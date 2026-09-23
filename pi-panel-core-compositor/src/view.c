#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/util/log.h>

#include "ipc.h"
#include "server.h"
#include "slot.h"
#include "view.h"

/* ---------------------------------------------------------------------------
 * Slot matching
 * ---------------------------------------------------------------------------*/

char *unit_for_pid(pid_t pid) {
    if (pid <= 0) {
        return NULL;
    }
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/cgroup", (int)pid);
    FILE *f = fopen(path, "r");
    if (!f) {
        return NULL;
    }

    /* cgroup v2 is one line: "0::/system.slice/pi-panel-app@immich.service".
     * Walk the path from the innermost component outwards and take the first
     * unit-looking one, so an app that creates its own sub-cgroups still maps
     * to the unit systemd started it in.  This works identically for system
     * units and for `systemd-run --user` ones, which is what lets the headless
     * test setup run without root. */
    char line[1024];
    char *unit = NULL;
    while (!unit && fgets(line, sizeof(line), f)) {
        if (strncmp(line, "0::", 3) != 0) {
            continue;
        }
        line[strcspn(line, "\n")] = '\0';
        char *p = line + 3;
        while (!unit) {
            char *slash = strrchr(p, '/');
            char *component = slash ? slash + 1 : p;
            size_t n = strlen(component);
            if ((n > 8 && strcmp(component + n - 8, ".service") == 0) ||
                (n > 6 && strcmp(component + n - 6, ".scope") == 0)) {
                unit = strdup(component);
            }
            if (!slash) {
                break;
            }
            *slash = '\0';
        }
    }
    fclose(f);
    return unit;
}

/* Try to give an unregistered window a home, e.g. once it reports an app_id. */
static void view_try_adopt(struct view *view) {
    if (view->slot) {
        return;
    }
    struct slot *slot = slot_find_free_match(view->server, view->unit, view->app_id);
    if (slot) {
        slot_adopt_unregistered(slot);
    }
}

/* ---------------------------------------------------------------------------
 * XDG toplevel signal handlers
 * ---------------------------------------------------------------------------*/

static void handle_view_map(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, map);
    struct server *server = view->server;

    view->mapped = true;

    /* Force fullscreen configure — the client should already have received one
     * on its initial commit, but re-send in case it ignored it. */
    if (server->output_width > 0) {
        wlr_xdg_toplevel_set_fullscreen(view->xdg_toplevel, true);
        wlr_xdg_toplevel_set_size(view->xdg_toplevel,
            server->output_width, server->output_height);
    }

    char buf[32];
    wlr_log(WLR_INFO, "View '%s' mapped (id=%d app_id=%s unit=%s)",
        view_name(view, buf, sizeof(buf)), view->id,
        view->app_id ? view->app_id : "(none)",
        view->unit ? view->unit : "(none)");
    ipc_event_view(server, "slot_mapped", view);

    /* The controller decides what is shown.  The one exception keeps the panel
     * from sitting black when the controller is absent: if nothing is visible,
     * the first *registered* window to map is shown.  Unregistered windows
     * never show themselves. */
    if (!server->active_view && view->slot) {
        view_activate(view);
    }
}

static void handle_view_unmap(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, unmap);
    struct server *server = view->server;

    view->mapped = false;
    ipc_event_view(server, "slot_unmapped", view);

    if (view->active) {
        struct view *next = view_next_registered(server, view);
        if (next && next != view) {
            view_activate(next);
        } else {
            view_hide(view);
            ipc_event(server, "active_changed");
        }
    }
}

/* The xdg-shell protocol forbids sending a configure event before the client
 * has made its initial commit, and wlroots asserts on it
 * (wlr_xdg_surface_schedule_configure: `surface->initialized`).  The initial
 * commit is therefore the first legal moment to force the surface fullscreen
 * at output size. */
static void handle_view_commit(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, commit);

    if (view->xdg_toplevel->base->initial_commit &&
        view->server->output_width > 0) {
        wlr_xdg_toplevel_set_fullscreen(view->xdg_toplevel, true);
        wlr_xdg_toplevel_set_size(view->xdg_toplevel,
            view->server->output_width,
            view->server->output_height);
    }
}

static void handle_view_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, destroy);
    struct server *server = view->server;

    char buf[32];
    wlr_log(WLR_INFO, "View '%s' destroyed (id=%d)",
        view_name(view, buf, sizeof(buf)), view->id);

    /* wlroots asserts that a toplevel's signal listener lists are empty before
     * it frees the toplevel, so every listener must be unlinked here — not
     * merely forgotten.  Removing our own destroy listener mid-emission is
     * safe: wl_signal_emit iterates with a cursor. */
    wl_list_remove(&view->map.link);
    wl_list_remove(&view->unmap.link);
    wl_list_remove(&view->commit.link);
    wl_list_remove(&view->destroy.link);
    wl_list_remove(&view->request_fullscreen.link);
    wl_list_remove(&view->set_title.link);
    wl_list_remove(&view->set_app_id.link);

    bool was_active = view->active;
    struct slot *slot = view->slot;
    if (slot) {
        slot->view = NULL;
        view->slot = NULL;
        /* The slot stays, empty, until its app comes back (systemd restarts
         * it) or the controller unregisters it. */
        ipc_event_slot(server, "slot_updated", slot);
    } else {
        ipc_event_view(server, "slot_removed", view);
    }

    if (server->active_view == view) {
        server->active_view = NULL;
    }
    if (server->transition.target == view) {
        server->transition.target = NULL;
    }
    if (server->transition.pending == view) {
        server->transition.pending = NULL;
    }
    if (was_active) {
        ipc_event(server, "active_changed");
    }

    wl_list_remove(&view->link);
    free(view->app_id);
    free(view->title);
    free(view->unit);
    free(view);
    /* scene_tree is destroyed along with the XDG surface. */
}

static void handle_view_request_fullscreen(struct wl_listener *listener,
                                            void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, request_fullscreen);
    /* Clients may ask before their initial commit (GStreamer's waylandsink
     * does).  Configuring then violates xdg-shell and wlroots asserts
     * (`surface->initialized`), taking the whole compositor down.  Nothing is
     * lost by ignoring it: handle_view_commit() makes every window fullscreen
     * on its initial commit anyway. */
    if (!view->xdg_toplevel->base->initialized) {
        return;
    }
    wlr_xdg_toplevel_set_fullscreen(view->xdg_toplevel, true);
}

static void handle_view_set_title(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, set_title);
    free(view->title);
    const char *t = view->xdg_toplevel->title;
    view->title = t ? strdup(t) : NULL;
    ipc_event_view(view->server, "slot_updated", view);
}

static void handle_view_set_app_id(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, set_app_id);
    free(view->app_id);
    const char *a = view->xdg_toplevel->app_id;
    view->app_id = a ? strdup(a) : NULL;
    ipc_event_view(view->server, "slot_updated", view);
    /* The app_id usually arrives just after the toplevel is created, so this
     * is where an app_id-matched window finds its slot. */
    view_try_adopt(view);
}

/* ---------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------------*/

struct view *view_create(struct server *server,
                         struct wlr_xdg_toplevel *toplevel, pid_t pid) {
    struct view *view = calloc(1, sizeof(struct view));
    if (!view) {
        wlr_log(WLR_ERROR, "OOM allocating view");
        return NULL;
    }
    view->server       = server;
    view->id           = server->next_id++;
    view->xdg_toplevel = toplevel;
    view->pid          = pid;
    view->unit         = unit_for_pid(pid);
    if (toplevel->app_id) {
        view->app_id = strdup(toplevel->app_id);
    }
    if (toplevel->title) {
        view->title = strdup(toplevel->title);
    }
    toplevel->base->data = view;
    wl_list_insert(server->views.prev, &view->link);

    /* Hidden until activated */
    view->scene_tree = wlr_scene_tree_create(server->app_layer);
    wlr_scene_node_set_enabled(&view->scene_tree->node, false);
    view->surface_tree =
        wlr_scene_xdg_surface_create(view->scene_tree, toplevel->base);
    /* A single fullscreen output means (0,0) is always correct */
    wlr_scene_node_set_position(&view->scene_tree->node, 0, 0);

    view->map.notify                = handle_view_map;
    view->unmap.notify              = handle_view_unmap;
    view->commit.notify             = handle_view_commit;
    view->destroy.notify            = handle_view_destroy;
    view->request_fullscreen.notify = handle_view_request_fullscreen;
    view->set_title.notify          = handle_view_set_title;
    view->set_app_id.notify         = handle_view_set_app_id;

    wl_signal_add(&toplevel->base->surface->events.map,    &view->map);
    wl_signal_add(&toplevel->base->surface->events.unmap,  &view->unmap);
    wl_signal_add(&toplevel->base->surface->events.commit, &view->commit);
    wl_signal_add(&toplevel->events.destroy,               &view->destroy);
    wl_signal_add(&toplevel->events.request_fullscreen,    &view->request_fullscreen);
    wl_signal_add(&toplevel->events.set_title,             &view->set_title);
    wl_signal_add(&toplevel->events.set_app_id,            &view->set_app_id);

    /* Do NOT configure the toplevel here — the client has not committed yet.
     * handle_view_commit() sends the fullscreen configure on initial commit. */

    struct slot *slot = slot_find_free_match(server, view->unit, view->app_id);
    if (slot) {
        view->slot = slot;
        slot->view = view;
        wlr_log(WLR_INFO, "Window id=%d (pid %d, %s) -> slot '%s'",
            view->id, (int)pid, view->unit ? view->unit : "no unit", slot->name);
        ipc_event_slot(server, "slot_updated", slot);
    } else {
        wlr_log(WLR_INFO, "Window id=%d (pid %d, %s) is unregistered: anon-%d",
            view->id, (int)pid, view->unit ? view->unit : "no unit", view->id);
        ipc_event_view(server, "slot_added", view);
    }
    return view;
}

void view_free_all(struct server *server) {
    struct view *view, *tmp;
    wl_list_for_each_safe(view, tmp, &server->views, link) {
        wl_list_remove(&view->map.link);
        wl_list_remove(&view->unmap.link);
        wl_list_remove(&view->commit.link);
        wl_list_remove(&view->destroy.link);
        wl_list_remove(&view->request_fullscreen.link);
        wl_list_remove(&view->set_title.link);
        wl_list_remove(&view->set_app_id.link);
        wl_list_remove(&view->link);
        free(view->app_id);
        free(view->title);
        free(view->unit);
        free(view);
    }
    server->active_view = NULL;
}

void view_activate(struct view *view) {
    struct server *server = view->server;

    if (view->active) {
        return;
    }

    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        if (v != view && v->active) {
            view_hide(v);
        }
    }

    wlr_scene_node_set_enabled(&view->scene_tree->node, true);
    view->active        = true;
    server->active_view = view;

    if (view->mapped) {
        struct wlr_keyboard *kb = wlr_seat_get_keyboard(server->input.seat);
        if (kb) {
            wlr_seat_keyboard_notify_enter(server->input.seat,
                view->xdg_toplevel->base->surface,
                kb->keycodes, kb->num_keycodes, &kb->modifiers);
        } else {
            wlr_seat_keyboard_notify_enter(server->input.seat,
                view->xdg_toplevel->base->surface, NULL, 0, NULL);
        }
    }

    char buf[32];
    wlr_log(WLR_DEBUG, "Activated view id=%d name='%s'",
        view->id, view_name(view, buf, sizeof(buf)));
    ipc_event(server, "active_changed");
}

void view_hide(struct view *view) {
    if (!view->active) {
        return;
    }
    wlr_scene_node_set_enabled(&view->scene_tree->node, false);
    view->active = false;
    if (view->server->active_view == view) {
        view->server->active_view = NULL;
    }
}

const char *view_name(const struct view *view, char *buf, size_t len) {
    if (view->slot) {
        return view->slot->name;
    }
    snprintf(buf, len, "anon-%d", view->id);
    return buf;
}

struct view *view_for_id(struct server *server, int id) {
    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        if (v->id == id) return v;
    }
    return NULL;
}

struct view *view_for_app_id(struct server *server, const char *app_id) {
    if (!app_id) return NULL;
    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        if (v->app_id && strcmp(v->app_id, app_id) == 0) return v;
    }
    return NULL;
}

struct view *view_next_registered(struct server *server, struct view *current) {
    struct view *v = current;
    do {
        struct wl_list *next_link = v->link.next;
        if (next_link == &server->views) {
            next_link = server->views.next;
        }
        v = wl_container_of(next_link, v, link);
        if (v != current && v->mapped && v->slot) return v;
    } while (v != current);
    return current;
}
