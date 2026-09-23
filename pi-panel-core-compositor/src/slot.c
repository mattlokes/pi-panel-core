#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <wlr/util/log.h>

#include "ipc.h"
#include "server.h"
#include "slot.h"
#include "view.h"

#define SLOT_NAME_MAX 63

bool slot_name_valid(const char *name) {
    if (!name || !*name) {
        return false;
    }
    size_t len = strlen(name);
    if (len > SLOT_NAME_MAX) {
        return false;
    }
    /* "anon-<id>" names unregistered windows; a slot must never shadow one. */
    if (strncmp(name, "anon-", 5) == 0) {
        return false;
    }
    /* Lowercase, so the name is also a valid systemd instance name and a safe
     * MQTT topic level without escaping. */
    for (size_t i = 0; i < len; i++) {
        char c = name[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                  (i > 0 && (c == '-' || c == '_'));
        if (!ok) {
            return false;
        }
    }
    return true;
}

static char *dup_or_null(const char *s) {
    return (s && *s) ? strdup(s) : NULL;
}

static bool str_eq(const char *a, const char *b) {
    return a && b && strcmp(a, b) == 0;
}

struct slot *slot_for_name(struct server *server, const char *name) {
    struct slot *slot;
    wl_list_for_each(slot, &server->slots, link) {
        if (str_eq(slot->name, name)) {
            return slot;
        }
    }
    return NULL;
}

struct slot *slot_for_id(struct server *server, int id) {
    struct slot *slot;
    wl_list_for_each(slot, &server->slots, link) {
        if (slot->id == id) {
            return slot;
        }
    }
    return NULL;
}

struct slot *slot_find_free_match(struct server *server,
                                  const char *unit, const char *app_id) {
    struct slot *slot;
    if (unit) {
        wl_list_for_each(slot, &server->slots, link) {
            if (!slot->view && str_eq(slot->match_unit, unit)) {
                return slot;
            }
        }
    }
    if (app_id) {
        wl_list_for_each(slot, &server->slots, link) {
            if (!slot->view && str_eq(slot->match_app_id, app_id)) {
                return slot;
            }
        }
    }
    return NULL;
}

void slot_adopt_unregistered(struct slot *slot) {
    struct server *server = slot->server;
    if (slot->view) {
        return;
    }
    /* Prefer a unit match over an app_id match, as slot_find_free_match does. */
    struct view *candidate = NULL, *view;
    wl_list_for_each(view, &server->views, link) {
        if (view->slot) {
            continue;
        }
        if (str_eq(slot->match_unit, view->unit)) {
            candidate = view;
            break;
        }
        if (!candidate && str_eq(slot->match_app_id, view->app_id)) {
            candidate = view;
        }
    }
    if (!candidate) {
        return;
    }

    char buf[32];
    wlr_log(WLR_INFO, "Slot '%s' adopts window %s", slot->name,
        view_name(candidate, buf, sizeof(buf)));
    ipc_event_view(server, "slot_removed", candidate);   /* the anon entry goes */
    candidate->slot = slot;
    slot->view = candidate;
    ipc_event_slot(server, "slot_updated", slot);

    /* Same rule as a fresh map: never leave the panel black if something
     * registered is ready to show. */
    if (!server->active_view && candidate->mapped) {
        view_activate(candidate);
    }
}

struct slot *slot_register(struct server *server, const char *name,
                           const char *match_unit, const char *match_app_id,
                           bool *created) {
    char default_unit[SLOT_NAME_MAX + 32];
    if (!match_unit || !*match_unit) {
        snprintf(default_unit, sizeof(default_unit),
            "pi-panel-app@%s.service", name);
        match_unit = default_unit;
    }

    struct slot *slot = slot_for_name(server, name);
    *created = (slot == NULL);
    if (slot) {
        bool changed = !str_eq(slot->match_unit, match_unit) ||
            !((slot->match_app_id == NULL && (!match_app_id || !*match_app_id)) ||
              str_eq(slot->match_app_id, match_app_id));
        if (changed) {
            free(slot->match_unit);
            free(slot->match_app_id);
            slot->match_unit   = strdup(match_unit);
            slot->match_app_id = dup_or_null(match_app_id);
            ipc_event_slot(server, "slot_updated", slot);
        }
    } else {
        slot = calloc(1, sizeof(*slot));
        if (!slot) {
            wlr_log(WLR_ERROR, "OOM allocating slot");
            return NULL;
        }
        slot->server       = server;
        slot->id           = server->next_id++;
        slot->name         = strdup(name);
        slot->match_unit   = strdup(match_unit);
        slot->match_app_id = dup_or_null(match_app_id);
        wl_list_insert(server->slots.prev, &slot->link);
        wlr_log(WLR_INFO, "Registered slot '%s' (unit=%s app_id=%s)",
            slot->name, slot->match_unit,
            slot->match_app_id ? slot->match_app_id : "-");
        ipc_event_slot(server, "slot_added", slot);
    }

    slot_adopt_unregistered(slot);
    return slot;
}

static void slot_free(struct slot *slot) {
    wl_list_remove(&slot->link);
    free(slot->name);
    free(slot->match_unit);
    free(slot->match_app_id);
    free(slot);
}

void slot_unregister(struct slot *slot) {
    struct server *server = slot->server;
    wlr_log(WLR_INFO, "Unregistered slot '%s'", slot->name);

    ipc_event_slot(server, "slot_removed", slot);
    struct view *view = slot->view;
    if (view) {
        view->slot = NULL;
        ipc_event_view(server, "slot_added", view);   /* back as anon-<id> */
    }
    slot_free(slot);
}

void slot_free_all(struct server *server) {
    struct slot *slot, *tmp;
    wl_list_for_each_safe(slot, tmp, &server->slots, link) {
        if (slot->view) {
            slot->view->slot = NULL;
        }
        slot_free(slot);
    }
}
