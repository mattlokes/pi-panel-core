#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <wlr/util/log.h>

#include "cJSON.h"
#include "io.pipanel.Compositor.varlink.h"   /* generated: ipc_interface_description */
#include "clock.h"
#include "ipc.h"
#include "server.h"
#include "slot.h"
#include "view.h"

#define IFACE          "io.pipanel.Compositor"
#define SERVICE_IFACE  "org.varlink.service"

#ifndef PI_PANEL_VERSION
#define PI_PANEL_VERSION "unknown"
#endif

/* Served verbatim by GetInterfaceDescription("org.varlink.service"). */
static const char service_interface_description[] =
    "# The Varlink Service Interface is provided by every varlink service. It\n"
    "# describes the service and the interfaces it implements.\n"
    "interface org.varlink.service\n"
    "\n"
    "# Get a list of all the interfaces a service provides and information\n"
    "# about the implementation.\n"
    "method GetInfo() -> (\n"
    "  vendor: string,\n"
    "  product: string,\n"
    "  version: string,\n"
    "  url: string,\n"
    "  interfaces: []string\n"
    ")\n"
    "\n"
    "# Get the description of an interface that is implemented by this service.\n"
    "method GetInterfaceDescription(interface: string) -> (description: string)\n"
    "\n"
    "# The requested interface was not found.\n"
    "error InterfaceNotFound (interface: string)\n"
    "\n"
    "# The requested method was not found\n"
    "error MethodNotFound (method: string)\n"
    "\n"
    "# The interface defines the requested method, but the service does not\n"
    "# implement it.\n"
    "error MethodNotImplemented (method: string)\n"
    "\n"
    "# One of the passed parameters is invalid.\n"
    "error InvalidParameter (parameter: string)\n"
    "\n"
    "# Client is denied access\n"
    "error PermissionDenied ()\n"
    "\n"
    "# Method is expected to be called with 'more' set to true, but wasn't\n"
    "error ExpectedMore ()\n";

/* ---------------------------------------------------------------------------
 * Client lifetime
 *
 * A client is never freed from inside code that may still touch it (a method
 * handler, a broadcast loop).  It is marked doomed and reaped from an idle
 * callback on the next loop iteration.
 * ---------------------------------------------------------------------------*/

static void ipc_client_free(struct ipc_server *ipc, int slot) {
    struct ipc_client *client = ipc->clients[slot];
    wl_event_source_remove(client->source);
    close(client->fd);
    free(client->in);
    free(client->out);
    free(client);
    ipc->clients[slot] = NULL;
}

static void reap_doomed(void *data) {
    struct ipc_server *ipc = data;
    ipc->reap_scheduled = false;
    for (int i = 0; i < IPC_MAX_CLIENTS; i++) {
        if (ipc->clients[i] && ipc->clients[i]->doomed) {
            ipc_client_free(ipc, i);
        }
    }
}

static void client_doom(struct ipc_client *client) {
    struct ipc_server *ipc = &client->server->ipc;
    client->doomed = true;
    if (!ipc->reap_scheduled) {
        ipc->reap_scheduled = true;
        wl_event_loop_add_idle(client->server->event_loop, reap_doomed, ipc);
    }
}

/* ---------------------------------------------------------------------------
 * Output
 * ---------------------------------------------------------------------------*/

static bool buf_reserve(char **buf, size_t *cap, size_t need) {
    if (need <= *cap) {
        return true;
    }
    size_t cap2 = *cap ? *cap : 1024;
    while (cap2 < need) {
        cap2 *= 2;
    }
    char *p = realloc(*buf, cap2);
    if (!p) {
        return false;
    }
    *buf = p;
    *cap = cap2;
    return true;
}

static void client_update_mask(struct ipc_client *client) {
    uint32_t mask = WL_EVENT_READABLE;
    if (client->out_len > 0) {
        mask |= WL_EVENT_WRITABLE;
    }
    wl_event_source_fd_update(client->source, mask);
}

static void client_flush(struct ipc_client *client) {
    size_t off = 0;
    while (off < client->out_len) {
        ssize_t n = write(client->fd, client->out + off, client->out_len - off);
        if (n > 0) {
            off += (size_t)n;
        } else if (n < 0 && errno == EINTR) {
            continue;
        } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            break;   /* the rest goes out when the socket is writable again */
        } else {
            client_doom(client);
            return;
        }
    }
    if (off > 0) {
        memmove(client->out, client->out + off, client->out_len - off);
        client->out_len -= off;
    }
    client_update_mask(client);
}

/* Queue one encoded message (without its NUL) and try to send it. */
static void client_queue(struct ipc_client *client, const char *msg, size_t len) {
    if (client->doomed) {
        return;
    }
    if (client->out_len + len + 1 > IPC_OUT_MAX) {
        /* A subscriber this far behind is not reading.  Waiting on it would
         * stall the compositor; dropping it lets it resubscribe for a fresh
         * snapshot. */
        wlr_log(WLR_ERROR, "IPC: client fell %zu bytes behind — disconnecting",
            client->out_len);
        client_doom(client);
        return;
    }
    if (!buf_reserve(&client->out, &client->out_cap, client->out_len + len + 1)) {
        client_doom(client);
        return;
    }
    memcpy(client->out + client->out_len, msg, len);
    client->out[client->out_len + len] = '\0';
    client->out_len += len + 1;
    client_flush(client);
}

static void client_send(struct ipc_client *client, cJSON *msg) {
    if (client->oneway) {
        cJSON_Delete(msg);
        return;
    }
    char *text = cJSON_PrintUnformatted(msg);
    cJSON_Delete(msg);
    if (!text) {
        client_doom(client);
        return;
    }
    client_queue(client, text, strlen(text));
    free(text);
}

/* Takes ownership of |params| (NULL means {}). */
static void reply(struct ipc_client *client, cJSON *params, bool continues) {
    cJSON *msg = cJSON_CreateObject();
    cJSON_AddItemToObject(msg, "parameters", params ? params : cJSON_CreateObject());
    if (continues) {
        cJSON_AddTrueToObject(msg, "continues");
    }
    client_send(client, msg);
}

static void reply_error(struct ipc_client *client, const char *error, cJSON *params) {
    cJSON *msg = cJSON_CreateObject();
    cJSON_AddStringToObject(msg, "error", error);
    cJSON_AddItemToObject(msg, "parameters", params ? params : cJSON_CreateObject());
    client_send(client, msg);
}

static void reply_error_str(struct ipc_client *client, const char *error,
                            const char *key, const char *value) {
    cJSON *params = cJSON_CreateObject();
    cJSON_AddStringToObject(params, key, value);
    reply_error(client, error, params);
}

static void invalid_parameter(struct ipc_client *client, const char *name) {
    reply_error_str(client, SERVICE_IFACE ".InvalidParameter", "parameter", name);
}

/* ---------------------------------------------------------------------------
 * JSON builders (the types of io.pipanel.Compositor)
 * ---------------------------------------------------------------------------*/

static void add_str_or_null(cJSON *obj, const char *key, const char *value) {
    if (value) {
        cJSON_AddStringToObject(obj, key, value);
    } else {
        cJSON_AddNullToObject(obj, key);
    }
}

static cJSON *output_json(struct server *server) {
    struct wlr_output *out = server->primary_output;
    if (!out) {
        return cJSON_CreateNull();
    }
    cJSON *o = cJSON_CreateObject();
    add_str_or_null(o, "name", out->name);
    cJSON_AddNumberToObject(o, "width", server->output_width);
    cJSON_AddNumberToObject(o, "height", server->output_height);
    /* refresh is in mHz; 0 when the backend (headless, nested) reports none */
    cJSON_AddNumberToObject(o, "refresh", out->refresh / 1000.0);
    return o;
}

static cJSON *clock_json(struct server *server) {
    struct clock_state *cs = &server->clock;
    cJSON *o = cJSON_CreateObject();
    cJSON_AddBoolToObject(o, "enabled", cs->enabled);
    cJSON_AddStringToObject(o, "format", cs->format);
    cJSON_AddStringToObject(o, "position", clock_position_name(cs->position));
    cJSON_AddNumberToObject(o, "size", cs->size);
    return o;
}

static cJSON *status_json(struct server *server) {
    cJSON *s = cJSON_CreateObject();
    char buf[32];
    add_str_or_null(s, "active",
        server->active_view ? view_name(server->active_view, buf, sizeof(buf)) : NULL);
    cJSON_AddBoolToObject(s, "transitioning", server->transition.active);
    cJSON_AddBoolToObject(s, "output_power", server->output_power);
    cJSON_AddItemToObject(s, "output", output_json(server));
    cJSON_AddItemToObject(s, "clock", clock_json(server));
    return s;
}

static void add_window_fields(cJSON *o, struct view *view) {
    cJSON_AddBoolToObject(o, "mapped", view && view->mapped);
    cJSON_AddBoolToObject(o, "active", view && view->active);
    add_str_or_null(o, "app_id", view ? view->app_id : NULL);
    add_str_or_null(o, "title", view ? view->title : NULL);
    add_str_or_null(o, "unit", view ? view->unit : NULL);
}

static cJSON *slot_json(struct slot *slot) {
    cJSON *o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "id", slot->id);
    cJSON_AddStringToObject(o, "name", slot->name);
    cJSON_AddTrueToObject(o, "registered");
    add_str_or_null(o, "match_unit", slot->match_unit);
    add_str_or_null(o, "match_app_id", slot->match_app_id);
    add_window_fields(o, slot->view);
    return o;
}

static cJSON *view_json(struct view *view) {
    if (view->slot) {
        return slot_json(view->slot);
    }
    cJSON *o = cJSON_CreateObject();
    char buf[32];
    cJSON_AddNumberToObject(o, "id", view->id);
    cJSON_AddStringToObject(o, "name", view_name(view, buf, sizeof(buf)));
    cJSON_AddFalseToObject(o, "registered");
    cJSON_AddNullToObject(o, "match_unit");
    cJSON_AddNullToObject(o, "match_app_id");
    add_window_fields(o, view);
    return o;
}

/* Registered slots in registration order, then unregistered windows. */
static cJSON *all_slots_json(struct server *server) {
    cJSON *arr = cJSON_CreateArray();
    struct slot *slot;
    wl_list_for_each(slot, &server->slots, link) {
        cJSON_AddItemToArray(arr, slot_json(slot));
    }
    struct view *view;
    wl_list_for_each(view, &server->views, link) {
        if (!view->slot) {
            cJSON_AddItemToArray(arr, view_json(view));
        }
    }
    return arr;
}

static cJSON *event_json(struct server *server, const char *kind, cJSON *slot) {
    cJSON *ev = cJSON_CreateObject();
    cJSON_AddStringToObject(ev, "kind", kind);
    cJSON_AddItemToObject(ev, "status", status_json(server));
    if (slot) {
        cJSON_AddItemToObject(ev, "slot", slot);
    }
    return ev;
}

/* ---------------------------------------------------------------------------
 * Events
 * ---------------------------------------------------------------------------*/

/* Takes ownership of |slot|.  Encodes once, queues to every subscriber. */
static void broadcast(struct server *server, const char *kind, cJSON *slot) {
    struct ipc_server *ipc = &server->ipc;
    bool any = false;
    for (int i = 0; i < IPC_MAX_CLIENTS; i++) {
        if (ipc->clients[i] && ipc->clients[i]->subscribed && !ipc->clients[i]->doomed) {
            any = true;
            break;
        }
    }
    if (!any) {
        cJSON_Delete(slot);
        return;
    }

    cJSON *params = cJSON_CreateObject();
    cJSON_AddItemToObject(params, "event", event_json(server, kind, slot));
    cJSON *msg = cJSON_CreateObject();
    cJSON_AddItemToObject(msg, "parameters", params);
    cJSON_AddTrueToObject(msg, "continues");
    char *text = cJSON_PrintUnformatted(msg);
    cJSON_Delete(msg);
    if (!text) {
        return;
    }
    size_t len = strlen(text);
    for (int i = 0; i < IPC_MAX_CLIENTS; i++) {
        struct ipc_client *c = ipc->clients[i];
        if (c && c->subscribed) {
            client_queue(c, text, len);
        }
    }
    free(text);
}

void ipc_event(struct server *server, const char *kind) {
    broadcast(server, kind, NULL);
}

void ipc_event_slot(struct server *server, const char *kind, struct slot *slot) {
    broadcast(server, kind, slot_json(slot));
}

void ipc_event_view(struct server *server, const char *kind, struct view *view) {
    broadcast(server, kind, view_json(view));
}

/* ---------------------------------------------------------------------------
 * Parameter helpers
 * ---------------------------------------------------------------------------*/

/* Optional string: *out is NULL when absent or null.  False if mistyped. */
static bool opt_string(cJSON *params, const char *key, const char **out) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(params, key);
    *out = NULL;
    if (!item || cJSON_IsNull(item)) {
        return true;
    }
    if (!cJSON_IsString(item)) {
        return false;
    }
    *out = item->valuestring;
    return true;
}

static const char *req_string(struct ipc_client *client, cJSON *params, const char *key) {
    const char *value;
    if (!opt_string(params, key, &value) || !value) {
        invalid_parameter(client, key);
        return NULL;
    }
    return value;
}

/* ---------------------------------------------------------------------------
 * io.pipanel.Compositor methods
 * ---------------------------------------------------------------------------*/

static void m_register_slot(struct ipc_client *client, cJSON *params) {
    const char *name = req_string(client, params, "name");
    if (!name) {
        return;
    }
    const char *match_unit, *match_app_id;
    if (!opt_string(params, "match_unit", &match_unit)) {
        invalid_parameter(client, "match_unit");
        return;
    }
    if (!opt_string(params, "match_app_id", &match_app_id)) {
        invalid_parameter(client, "match_app_id");
        return;
    }
    if (!slot_name_valid(name)) {
        reply_error_str(client, IFACE ".InvalidName", "name", name);
        return;
    }
    bool created;
    struct slot *slot = slot_register(client->server, name, match_unit,
                                      match_app_id, &created);
    if (!slot) {
        reply_error_str(client, "io.pipanel.InternalError", "message", "out of memory");
        return;
    }
    cJSON *out = cJSON_CreateObject();
    cJSON_AddItemToObject(out, "slot", slot_json(slot));
    reply(client, out, false);
}

static void m_unregister_slot(struct ipc_client *client, cJSON *params) {
    const char *name = req_string(client, params, "name");
    if (!name) {
        return;
    }
    struct slot *slot = slot_for_name(client->server, name);
    if (!slot) {
        reply_error_str(client, IFACE ".NoSuchSlot", "slot", name);
        return;
    }
    slot_unregister(slot);
    reply(client, NULL, false);
}

static void m_list_slots(struct ipc_client *client, cJSON *params) {
    (void)params;
    cJSON *out = cJSON_CreateObject();
    cJSON_AddItemToObject(out, "slots", all_slots_json(client->server));
    reply(client, out, false);
}

static void m_get_status(struct ipc_client *client, cJSON *params) {
    (void)params;
    cJSON *out = cJSON_CreateObject();
    cJSON_AddItemToObject(out, "status", status_json(client->server));
    reply(client, out, false);
}

/* Name, "anon-<id>", numeric id, then app_id.  Sets *found to whether the
 * name refers to anything at all (a registered slot may have no window). */
static struct view *resolve_view(struct server *server, const char *name, bool *found) {
    *found = true;
    struct slot *slot = slot_for_name(server, name);
    if (slot) {
        return slot->view;
    }
    char *end;
    const char *digits = strncmp(name, "anon-", 5) == 0 ? name + 5 : name;
    long id = strtol(digits, &end, 10);
    if (*digits && *end == '\0') {
        struct view *view = view_for_id(server, (int)id);
        if (view) {
            return view;
        }
        slot = slot_for_id(server, (int)id);
        if (slot) {
            return slot->view;
        }
    }
    struct view *view = view_for_app_id(server, name);
    if (view) {
        return view;
    }
    *found = false;
    return NULL;
}

static void m_switch(struct ipc_client *client, cJSON *params) {
    struct server *server = client->server;
    const char *name = req_string(client, params, "slot");
    if (!name) {
        return;
    }
    const char *transition;
    if (!opt_string(params, "transition", &transition) ||
        (transition && strcmp(transition, "fade") != 0 && strcmp(transition, "cut") != 0)) {
        invalid_parameter(client, "transition");
        return;
    }

    bool found;
    struct view *view = resolve_view(server, name, &found);
    if (!found) {
        reply_error_str(client, IFACE ".NoSuchSlot", "slot", name);
        return;
    }
    if (!view || !view->mapped) {
        reply_error_str(client, IFACE ".SlotNotMapped", "slot", name);
        return;
    }
    if (transition && strcmp(transition, "cut") == 0) {
        transition_cut(&server->transition, view);
    } else {
        transition_begin(&server->transition, view);
    }
    reply(client, NULL, false);
}

static void m_set_output_power(struct ipc_client *client, cJSON *params) {
    cJSON *on = cJSON_GetObjectItemCaseSensitive(params, "on");
    if (!cJSON_IsBool(on)) {
        invalid_parameter(client, "on");
        return;
    }
    server_set_output_power(client->server, cJSON_IsTrue(on));
    reply(client, NULL, false);
}

static void m_set_clock(struct ipc_client *client, cJSON *params) {
    struct server *server = client->server;

    cJSON *enabled = cJSON_GetObjectItemCaseSensitive(params, "enabled");
    if (!cJSON_IsBool(enabled)) {
        invalid_parameter(client, "enabled");
        return;
    }
    const char *format;
    if (!opt_string(params, "format", &format) ||
            (format && (!*format || strlen(format) >= CLOCK_FORMAT_MAX))) {
        invalid_parameter(client, "format");
        return;
    }
    const char *pos_name;
    int position = -1;
    if (!opt_string(params, "position", &pos_name) ||
            (pos_name && (position = clock_position_parse(pos_name)) < 0)) {
        invalid_parameter(client, "position");
        return;
    }
    int size = -1;
    cJSON *size_item = cJSON_GetObjectItemCaseSensitive(params, "size");
    if (size_item && !cJSON_IsNull(size_item)) {
        double d = cJSON_IsNumber(size_item) ? size_item->valuedouble : -1.0;
        if (!(d == 0.0 || (d >= CLOCK_SIZE_MIN && d <= CLOCK_SIZE_MAX)) || d != (int)d) {
            invalid_parameter(client, "size");
            return;
        }
        size = (int)d;
    }

    const char *err = NULL;
    if (!clock_configure(&server->clock, cJSON_IsTrue(enabled), format,
                         position, size, &err)) {
        reply_error_str(client, IFACE ".ClockUnavailable", "reason", err);
        return;
    }
    ipc_event(server, "clock_changed");
    cJSON *out = cJSON_CreateObject();
    cJSON_AddItemToObject(out, "clock", clock_json(server));
    reply(client, out, false);
}

static void m_quit(struct ipc_client *client, cJSON *params) {
    (void)params;
    reply(client, NULL, false);
    wlr_log(WLR_INFO, "Shutdown requested over IPC");
    wl_display_terminate(client->server->display);
}

static void m_subscribe(struct ipc_client *client, cJSON *params) {
    (void)params;
    if (!client->more) {
        reply_error(client, SERVICE_IFACE ".ExpectedMore", NULL);
        return;
    }
    /* Subscribed first, snapshot second, both on this same loop iteration:
     * no event can fall between them. */
    client->subscribed = true;
    cJSON *ev = event_json(client->server, "snapshot", NULL);
    cJSON_AddItemToObject(ev, "slots", all_slots_json(client->server));
    cJSON *out = cJSON_CreateObject();
    cJSON_AddItemToObject(out, "event", ev);
    reply(client, out, true);
}

/* ---------------------------------------------------------------------------
 * org.varlink.service methods
 * ---------------------------------------------------------------------------*/

static void m_get_info(struct ipc_client *client, cJSON *params) {
    (void)params;
    cJSON *out = cJSON_CreateObject();
    cJSON_AddStringToObject(out, "vendor", "pi-panel");
    cJSON_AddStringToObject(out, "product", "pi-panel-core-compositor");
    cJSON_AddStringToObject(out, "version", PI_PANEL_VERSION);
    cJSON_AddStringToObject(out, "url", "https://github.com/mattlokes/pi-panel-core");
    cJSON *ifaces = cJSON_AddArrayToObject(out, "interfaces");
    cJSON_AddItemToArray(ifaces, cJSON_CreateString(IFACE));
    cJSON_AddItemToArray(ifaces, cJSON_CreateString(SERVICE_IFACE));
    reply(client, out, false);
}

static void m_get_interface_description(struct ipc_client *client, cJSON *params) {
    const char *name = req_string(client, params, "interface");
    if (!name) {
        return;
    }
    const char *text = NULL;
    if (strcmp(name, IFACE) == 0) {
        text = ipc_interface_description;
    } else if (strcmp(name, SERVICE_IFACE) == 0) {
        text = service_interface_description;
    }
    if (!text) {
        reply_error_str(client, SERVICE_IFACE ".InterfaceNotFound", "interface", name);
        return;
    }
    cJSON *out = cJSON_CreateObject();
    cJSON_AddStringToObject(out, "description", text);
    reply(client, out, false);
}

/* ---------------------------------------------------------------------------
 * Dispatch
 * ---------------------------------------------------------------------------*/

struct method {
    const char *name;
    void      (*fn)(struct ipc_client *client, cJSON *params);
};

static const struct method methods[] = {
    { IFACE ".RegisterSlot",                    m_register_slot },
    { IFACE ".UnregisterSlot",                  m_unregister_slot },
    { IFACE ".ListSlots",                       m_list_slots },
    { IFACE ".GetStatus",                       m_get_status },
    { IFACE ".Switch",                          m_switch },
    { IFACE ".SetOutputPower",                  m_set_output_power },
    { IFACE ".SetClock",                        m_set_clock },
    { IFACE ".Quit",                            m_quit },
    { IFACE ".Subscribe",                       m_subscribe },
    { SERVICE_IFACE ".GetInfo",                 m_get_info },
    { SERVICE_IFACE ".GetInterfaceDescription", m_get_interface_description },
};

/* Returns false if the client broke the protocol and must be dropped. */
static bool dispatch(struct ipc_client *client, const char *text) {
    cJSON *msg = cJSON_Parse(text);
    if (!cJSON_IsObject(msg)) {
        wlr_log(WLR_ERROR, "IPC: message is not a JSON object — dropping client");
        cJSON_Delete(msg);
        return false;
    }
    cJSON *method = cJSON_GetObjectItemCaseSensitive(msg, "method");
    cJSON *params = cJSON_GetObjectItemCaseSensitive(msg, "parameters");
    if (!cJSON_IsString(method) || (params && !cJSON_IsObject(params) && !cJSON_IsNull(params))) {
        wlr_log(WLR_ERROR, "IPC: malformed call — dropping client");
        cJSON_Delete(msg);
        return false;
    }
    cJSON *empty = NULL;
    if (!cJSON_IsObject(params)) {
        params = empty = cJSON_CreateObject();
    }
    client->more   = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(msg, "more"));
    client->oneway = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(msg, "oneway"));

    wlr_log(WLR_DEBUG, "IPC call: %s", method->valuestring);

    const struct method *m = NULL;
    for (size_t i = 0; i < sizeof(methods) / sizeof(methods[0]); i++) {
        if (strcmp(methods[i].name, method->valuestring) == 0) {
            m = &methods[i];
            break;
        }
    }
    if (m) {
        m->fn(client, params);
    } else {
        const char *dot = strrchr(method->valuestring, '.');
        size_t iface_len = dot ? (size_t)(dot - method->valuestring) : 0;
        if ((iface_len == strlen(IFACE) && strncmp(method->valuestring, IFACE, iface_len) == 0) ||
            (iface_len == strlen(SERVICE_IFACE) && strncmp(method->valuestring, SERVICE_IFACE, iface_len) == 0)) {
            reply_error_str(client, SERVICE_IFACE ".MethodNotFound", "method", method->valuestring);
        } else {
            char iface[256];
            snprintf(iface, sizeof(iface), "%.*s", (int)iface_len, method->valuestring);
            reply_error_str(client, SERVICE_IFACE ".InterfaceNotFound", "interface", iface);
        }
    }
    client->oneway = false;
    cJSON_Delete(empty);
    cJSON_Delete(msg);
    return true;
}

/* ---------------------------------------------------------------------------
 * Client I/O
 * ---------------------------------------------------------------------------*/

static void client_read(struct ipc_client *client) {
    if (!buf_reserve(&client->in, &client->in_cap, client->in_len + 4096)) {
        client_doom(client);
        return;
    }
    ssize_t n = read(client->fd, client->in + client->in_len,
                     client->in_cap - client->in_len);
    if (n == 0) {
        client_doom(client);   /* EOF: the peer hung up (e.g. a subscriber went away) */
        return;
    }
    if (n < 0) {
        if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
            client_doom(client);
        }
        return;
    }
    client->in_len += (size_t)n;

    size_t start = 0;
    char *nul;
    while (!client->doomed &&
           (nul = memchr(client->in + start, '\0', client->in_len - start))) {
        if (client->subscribed) {
            /* A Subscribe call never ends, so a further call on this
             * connection could never be answered.  That is a client bug. */
            wlr_log(WLR_ERROR, "IPC: call on a subscribed connection — dropping client");
            client_doom(client);
            return;
        }
        if (!dispatch(client, client->in + start)) {
            client_doom(client);
            return;
        }
        start = (size_t)(nul - client->in) + 1;
    }

    if (start > 0) {
        memmove(client->in, client->in + start, client->in_len - start);
        client->in_len -= start;
    }
    if (client->in_len > IPC_IN_MAX) {
        wlr_log(WLR_ERROR, "IPC: message exceeds %d bytes — dropping client", IPC_IN_MAX);
        client_doom(client);
    }
}

static int client_event(int fd, uint32_t mask, void *data) {
    (void)fd;
    struct ipc_client *client = data;
    if (client->doomed) {
        return 0;
    }
    if (mask & WL_EVENT_WRITABLE) {
        client_flush(client);
    }
    if (!client->doomed && (mask & WL_EVENT_READABLE)) {
        client_read(client);
    }
    if (!client->doomed && (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR))) {
        client_doom(client);
    }
    return 0;
}

static int ipc_accept(int fd, uint32_t mask, void *data) {
    (void)mask;
    struct ipc_server *ipc = data;

    int client_fd = accept4(fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
    if (client_fd < 0) {
        wlr_log(WLR_ERROR, "accept4() failed: %s", strerror(errno));
        return 0;
    }

    int slot = -1;
    for (int i = 0; i < IPC_MAX_CLIENTS; i++) {
        if (!ipc->clients[i]) { slot = i; break; }
    }
    if (slot < 0) {
        wlr_log(WLR_ERROR, "IPC: too many clients, rejecting connection");
        close(client_fd);
        return 0;
    }

    struct ipc_client *client = calloc(1, sizeof(struct ipc_client));
    if (!client) {
        close(client_fd);
        return 0;
    }
    client->fd     = client_fd;
    client->server = ipc->server;
    client->source = wl_event_loop_add_fd(ipc->server->event_loop, client_fd,
        WL_EVENT_READABLE, client_event, client);
    ipc->clients[slot] = client;
    wlr_log(WLR_DEBUG, "IPC: client connected (slot %d)", slot);
    return 0;
}

/* ---------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------------*/

bool ipc_init(struct ipc_server *ipc, struct server *server, const char *path) {
    ipc->server  = server;
    ipc->sock_fd = -1;

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof(addr.sun_path)) {
        wlr_log(WLR_ERROR, "IPC socket path too long: %s", path);
        return false;
    }
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    ipc->sock_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (ipc->sock_fd < 0) {
        wlr_log(WLR_ERROR, "socket() failed: %s", strerror(errno));
        return false;
    }

    /* A leftover socket file from a crash would make bind() fail.  But only
     * remove it if nobody answers on it: never steal a live compositor's. */
    int probe = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (probe >= 0) {
        if (connect(probe, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
            close(probe);
            wlr_log(WLR_ERROR, "IPC socket %s is in use by another compositor", path);
            close(ipc->sock_fd);
            ipc->sock_fd = -1;
            return false;
        }
        close(probe);
    }
    unlink(path);

    if (bind(ipc->sock_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        wlr_log(WLR_ERROR, "bind() failed on '%s': %s", path, strerror(errno));
        close(ipc->sock_fd);
        ipc->sock_fd = -1;
        return false;
    }
    /* Owner and group only: control of the panel is not for every local user. */
    chmod(path, 0660);

    if (listen(ipc->sock_fd, IPC_MAX_CLIENTS) < 0) {
        wlr_log(WLR_ERROR, "listen() failed: %s", strerror(errno));
        close(ipc->sock_fd);
        ipc->sock_fd = -1;
        return false;
    }

    ipc->socket_path = strdup(path);
    ipc->source = wl_event_loop_add_fd(server->event_loop, ipc->sock_fd,
        WL_EVENT_READABLE, ipc_accept, ipc);

    wlr_log(WLR_INFO, "Varlink IPC listening on %s", path);
    return true;
}

void ipc_finish(struct ipc_server *ipc) {
    if (ipc->source) {
        wl_event_source_remove(ipc->source);
        ipc->source = NULL;
    }
    for (int i = 0; i < IPC_MAX_CLIENTS; i++) {
        if (ipc->clients[i]) {
            ipc_client_free(ipc, i);
        }
    }
    if (ipc->sock_fd >= 0) {
        close(ipc->sock_fd);
        ipc->sock_fd = -1;
    }
    if (ipc->socket_path) {
        unlink(ipc->socket_path);
        free(ipc->socket_path);
        ipc->socket_path = NULL;
    }
}
