#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/util/log.h>

#include "server.h"
#include "view.h"

/* ---------------------------------------------------------------------------
 * XDG toplevel signal handlers
 * ---------------------------------------------------------------------------*/

static void handle_view_map(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, map);

    view->mapped = true;

    /* Force fullscreen configure — the client should already have received one
     * in view_attach_toplevel, but re-send in case it ignored it. */
    if (view->server->output_width > 0) {
        wlr_xdg_toplevel_set_fullscreen(view->xdg_toplevel, true);
        wlr_xdg_toplevel_set_size(view->xdg_toplevel,
            view->server->output_width,
            view->server->output_height);
    }

    wlr_log(WLR_INFO, "View '%s' mapped (id=%d app_id=%s)",
        view->name  ? view->name  : "(anon)",
        view->id,
        view->app_id ? view->app_id : "(none)");

    /* Show something as soon as anything is mappable, so the panel is never
     * needlessly black. */
    if (!view->server->active_view) {
        view_activate(view);
    }

    /* Clients race to map, so "first to map" is not a stable choice of default
     * view.  Until the controller switches explicitly, keep the first view in
     * config order visible — correcting here if a later-configured client
     * happened to map first.  Done without a fade: this is startup settling,
     * not a user-visible transition. */
    if (!view->server->view_selected_by_user) {
        struct view *first = view_first_mapped(view->server);
        if (first && first != view->server->active_view) {
            wlr_log(WLR_DEBUG,
                "Preferring first configured view '%s' over '%s'",
                first->name ? first->name : "(anon)",
                view->server->active_view && view->server->active_view->name
                    ? view->server->active_view->name : "(anon)");
            view_activate(first);
        }
    }
}

static void handle_view_unmap(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, unmap);
    view->mapped = false;

    if (view->active) {
        /* Switch to another mapped view if available */
        struct view *next = view_next(view->server, view);
        if (next && next != view) {
            view_activate(next);
        } else {
            view->active = false;
            view->server->active_view = NULL;
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

    if (view->xdg_toplevel &&
        view->xdg_toplevel->base->initial_commit &&
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

    wlr_log(WLR_INFO, "View '%s' destroyed (id=%d)",
        view->name ? view->name : "(anon)", view->id);

    /* wlroots asserts that a toplevel's signal listener lists are empty before
     * it frees the toplevel, so every listener must be unlinked here — not
     * merely forgotten.  Removing our own destroy listener mid-emission is
     * safe: wl_signal_emit iterates with a cursor.  view_free() skips its own
     * removal block because it guards on view->xdg_toplevel, cleared below. */
    wl_list_remove(&view->map.link);
    wl_list_remove(&view->unmap.link);
    wl_list_remove(&view->commit.link);
    wl_list_remove(&view->destroy.link);
    wl_list_remove(&view->request_fullscreen.link);
    wl_list_remove(&view->set_title.link);
    wl_list_remove(&view->set_app_id.link);

    /* Detach the XDG surface reference so view_free does not double-free */
    view->xdg_toplevel    = NULL;
    view->scene_tree      = NULL;   /* destroyed with the XDG surface */
    view->surface_tree = NULL;
    view->mapped = false;

    /* If managed and auto-restart is requested, re-launch.
     * The pid was already cleared by handle_sigchld when the process exited. */
    if (view->command && view->auto_restart && view->pid == 0) {
        wlr_log(WLR_INFO, "Auto-restarting view '%s'", view->name);
        view_launch(view);
        return; /* Keep the view slot */
    }

    /* For managed slots without auto-restart, keep the slot (pid=0, no surface).
     * For anonymous views, remove the slot entirely. */
    if (!view->command) {
        view_free(view);
    }
}

static void handle_view_request_fullscreen(struct wl_listener *listener,
                                            void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, request_fullscreen);
    /* Always grant fullscreen in a kiosk — client asked, we comply */
    wlr_xdg_toplevel_set_fullscreen(view->xdg_toplevel, true);
}

static void handle_view_set_title(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, set_title);
    free(view->title);
    const char *t = view->xdg_toplevel->title;
    view->title = t ? strdup(t) : NULL;
}

static void handle_view_set_app_id(struct wl_listener *listener, void *data) {
    (void)data;
    struct view *view = wl_container_of(listener, view, set_app_id);
    free(view->app_id);
    const char *a = view->xdg_toplevel->app_id;
    view->app_id = a ? strdup(a) : NULL;
}

/* ---------------------------------------------------------------------------
 * Internal helpers
 * ---------------------------------------------------------------------------*/

/* Allocate a bare view slot with no surface attached */
static struct view *view_alloc(struct server *server) {
    struct view *view = calloc(1, sizeof(struct view));
    if (!view) {
        wlr_log(WLR_ERROR, "OOM allocating view");
        return NULL;
    }
    view->server = server;
    view->id     = server->next_view_id++;
    wl_list_insert(server->views.prev, &view->link);
    server->view_count++;
    return view;
}

/* ---------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------------*/

struct view *view_create_managed(struct server *server, const char *name,
                                  const char *command, bool auto_restart) {
    struct view *view = view_alloc(server);
    if (!view) {
        return NULL;
    }
    view->name         = name    ? strdup(name)    : NULL;
    view->command      = command ? strdup(command) : NULL;
    view->auto_restart = auto_restart;
    return view;
}

struct view *view_create_anonymous(struct server *server,
                                    struct wlr_xdg_toplevel *toplevel) {
    struct view *view = view_alloc(server);
    if (!view) {
        return NULL;
    }
    view_attach_toplevel(view, toplevel);
    return view;
}

void view_attach_toplevel(struct view *view, struct wlr_xdg_toplevel *toplevel) {
    struct server *server = view->server;

    view->xdg_toplevel = toplevel;
    /* Stash a back-pointer in the XDG surface so signal handlers can reach us */
    toplevel->base->data = view;

    /* Create a scene tree for this view's content, hidden initially */
    view->scene_tree = wlr_scene_tree_create(server->app_layer);
    wlr_scene_node_set_enabled(&view->scene_tree->node, false);

    /* Place the XDG surface into the scene tree */
    view->surface_tree =
        wlr_scene_xdg_surface_create(view->scene_tree, toplevel->base);

    /* Position at origin — a single fullscreen output means (0,0) is correct */
    wlr_scene_node_set_position(&view->scene_tree->node, 0, 0);

    /* Wire XDG signals */
    view->map.notify               = handle_view_map;
    view->unmap.notify             = handle_view_unmap;
    view->commit.notify            = handle_view_commit;
    view->destroy.notify           = handle_view_destroy;
    view->request_fullscreen.notify = handle_view_request_fullscreen;
    view->set_title.notify         = handle_view_set_title;
    view->set_app_id.notify        = handle_view_set_app_id;

    wl_signal_add(&toplevel->base->surface->events.map,       &view->map);
    wl_signal_add(&toplevel->base->surface->events.unmap,     &view->unmap);
    wl_signal_add(&toplevel->base->surface->events.commit,    &view->commit);
    wl_signal_add(&toplevel->events.destroy,                   &view->destroy);
    wl_signal_add(&toplevel->events.request_fullscreen,        &view->request_fullscreen);
    wl_signal_add(&toplevel->events.set_title,                 &view->set_title);
    wl_signal_add(&toplevel->events.set_app_id,                &view->set_app_id);

    /* Capture current app_id / title if already set */
    if (toplevel->app_id) {
        view->app_id = strdup(toplevel->app_id);
    }
    if (toplevel->title) {
        view->title = strdup(toplevel->title);
    }

    /* Do NOT configure the toplevel here — the client has not committed yet.
     * handle_view_commit() sends the fullscreen configure on initial commit. */

    wlr_log(WLR_DEBUG, "Toplevel attached to view id=%d name='%s'",
        view->id, view->name ? view->name : "(anon)");
}

bool view_launch(struct view *view) {
    if (!view->command) {
        wlr_log(WLR_ERROR, "view_launch: view '%s' has no command",
            view->name ? view->name : "(anon)");
        return false;
    }

    pid_t pid = fork();
    if (pid < 0) {
        wlr_log(WLR_ERROR, "fork() failed: %s", strerror(errno));
        return false;
    }

    if (pid == 0) {
        /* Child process.  Start a new session so this view's entire process
         * tree shares one process group: view_terminate() can then take down
         * a forking wrapper script and the app it spawned together. */
        setsid();

        if (view->server->wayland_socket) {
            setenv("WAYLAND_DISPLAY", view->server->wayland_socket, 1);
        }
        /* Unset DISPLAY so SDL/Qt don't fall back to X11 */
        unsetenv("DISPLAY");

        execl("/bin/sh", "sh", "-c", view->command, (char *)NULL);
        /* execl only returns on error */
        _exit(127);
    }

    /* Parent */
    view->pid = pid;
    wlr_log(WLR_DEBUG, "Launched view '%s' pid=%d command='%s'",
        view->name ? view->name : "(anon)", pid, view->command);
    return true;
}

bool view_terminate(struct view *view) {
    if (view->pid <= 0) {
        return false;
    }
    /* Negative pid signals the whole process group.  view_launch() made the
     * child a session leader, so its pgid equals its pid. */
    if (kill(-view->pid, SIGTERM) == 0) {
        return true;
    }
    if (errno == ESRCH) {
        /* Group already gone (or never formed) — try the process itself. */
        return kill(view->pid, SIGTERM) == 0;
    }
    wlr_log(WLR_ERROR, "Failed to terminate view '%s' (pid %d): %s",
        view->name ? view->name : "(anon)", (int)view->pid, strerror(errno));
    return false;
}

void view_activate(struct view *view) {
    struct server *server = view->server;

    if (view->active) {
        return;
    }

    /* Hide all other views */
    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        if (v != view && v->active) {
            view_hide(v);
        }
    }

    /* Show this view */
    if (view->scene_tree) {
        wlr_scene_node_set_enabled(&view->scene_tree->node, true);
    }
    view->active       = true;
    server->active_view = view;

    /* Transfer keyboard focus */
    if (view->xdg_toplevel && view->mapped) {
        struct wlr_keyboard *kb = wlr_seat_get_keyboard(server->input.seat);
        if (kb) {
            wlr_seat_keyboard_notify_enter(
                server->input.seat,
                view->xdg_toplevel->base->surface,
                kb->keycodes,
                kb->num_keycodes,
                &kb->modifiers);
        } else {
            wlr_seat_keyboard_notify_enter(
                server->input.seat,
                view->xdg_toplevel->base->surface,
                NULL, 0, NULL);
        }
    }

    wlr_log(WLR_DEBUG, "Activated view id=%d name='%s'",
        view->id, view->name ? view->name : "(anon)");
}

void view_hide(struct view *view) {
    if (!view->active) {
        return;
    }
    if (view->scene_tree) {
        wlr_scene_node_set_enabled(&view->scene_tree->node, false);
    }
    view->active = false;
    if (view->server->active_view == view) {
        view->server->active_view = NULL;
    }
}

void view_free(struct view *view) {
    /* Remove signal listeners if surface is still alive */
    if (view->xdg_toplevel) {
        wl_list_remove(&view->map.link);
        wl_list_remove(&view->unmap.link);
        wl_list_remove(&view->commit.link);
        wl_list_remove(&view->destroy.link);
        wl_list_remove(&view->request_fullscreen.link);
        wl_list_remove(&view->set_title.link);
        wl_list_remove(&view->set_app_id.link);
    }

    if (view->server->active_view == view) {
        view->server->active_view = NULL;
    }
    if (view->server->transition.target  == view) {
        view->server->transition.target  = NULL;
    }
    if (view->server->transition.pending == view) {
        view->server->transition.pending = NULL;
    }

    wl_list_remove(&view->link);
    view->server->view_count--;

    free(view->name);
    free(view->app_id);
    free(view->title);
    free(view->command);
    free(view);
}

/* ---------------------------------------------------------------------------
 * Lookup functions
 * ---------------------------------------------------------------------------*/

struct view *view_for_id(struct server *server, int id) {
    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        if (v->id == id) return v;
    }
    return NULL;
}

struct view *view_for_name(struct server *server, const char *name) {
    if (!name) return NULL;
    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        if (v->name && strcmp(v->name, name) == 0) return v;
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

struct view *view_for_pid(struct server *server, pid_t pid) {
    if (pid <= 0) return NULL;
    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        if (v->pid == pid) return v;
    }
    return NULL;
}

/* Read the parent PID of |pid| from /proc/<pid>/stat, or 0 if unavailable. */
static pid_t parent_pid(pid_t pid) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/stat", (int)pid);

    FILE *f = fopen(path, "r");
    if (!f) {
        return 0;
    }
    char   buf[512];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    if (n == 0) {
        return 0;
    }
    buf[n] = '\0';

    /* Field 2 (comm) is parenthesised and may itself contain spaces or a
     * ')', so the only safe anchor is the *last* ')' in the line. */
    const char *p = strrchr(buf, ')');
    if (!p) {
        return 0;
    }
    char state;
    int  ppid;
    if (sscanf(p + 1, " %c %d", &state, &ppid) != 2) {
        return 0;
    }
    return (pid_t)ppid;
}

/* How far up the process tree to look for an owning view. Wrapper scripts are
 * typically 1-2 levels; the cap just bounds the walk. */
#define VIEW_PID_MAX_DEPTH 8

struct view *view_for_pid_or_ancestor(struct server *server, pid_t pid) {
    for (int depth = 0; pid > 1 && depth < VIEW_PID_MAX_DEPTH; depth++) {
        struct view *view = view_for_pid(server, pid);
        if (view) {
            if (depth > 0) {
                wlr_log(WLR_DEBUG,
                    "Matched client to view '%s' via ancestor pid %d (%d level%s up)",
                    view->name ? view->name : "(anon)", (int)pid,
                    depth, depth == 1 ? "" : "s");
            }
            return view;
        }
        pid = parent_pid(pid);
    }
    return NULL;
}

struct view *view_first_mapped(struct server *server) {
    /* Views are appended on creation, so list order is config/id order. */
    struct view *v;
    wl_list_for_each(v, &server->views, link) {
        if (v->mapped) {
            return v;
        }
    }
    return NULL;
}

/* Return the next *mapped* view after |current|, wrapping around.
 * Returns NULL (or |current| itself) if there are fewer than two mapped views. */
struct view *view_next(struct server *server, struct view *current) {
    struct view *v = current;
    do {
        struct wl_list *next_link = v->link.next;
        if (next_link == &server->views) {
            next_link = server->views.next;
        }
        v = wl_container_of(next_link, v, link);
        if (v->mapped) return v;
    } while (v != current);
    return current;
}

struct view *view_prev(struct server *server, struct view *current) {
    struct view *v = current;
    do {
        struct wl_list *prev_link = v->link.prev;
        if (prev_link == &server->views) {
            prev_link = server->views.prev;
        }
        v = wl_container_of(prev_link, v, link);
        if (v->mapped) return v;
    } while (v != current);
    return current;
}
