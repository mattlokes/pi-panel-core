#ifndef VIEW_H
#define VIEW_H

#include <stdbool.h>
#include <sys/types.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/types/wlr_scene.h>

#include "server.h"

struct view {
    struct server                *server;
    struct wl_list                link;    /* link in server->views */

    /* Wayland surface — NULL until the client's XDG toplevel is created */
    struct wlr_xdg_toplevel      *xdg_toplevel;
    struct wlr_scene_tree        *scene_tree;
    struct wlr_scene_tree        *surface_tree;   /* subtree created by wlr_scene_xdg_surface_create */

    /* Identity */
    int     id;        /* unique, monotonically increasing, never reused */
    char   *name;      /* human label set at creation (may be NULL for anon) */
    char   *app_id;    /* reported by client via set_app_id (may be NULL) */
    char   *title;     /* reported by client via set_title (may be NULL) */

    /* App launcher */
    char   *command;      /* shell command to exec (NULL if externally launched) */
    pid_t   pid;          /* child PID; 0 = not launched by us / already exited */
    bool    auto_restart; /* re-exec command when process exits */

    /* State */
    bool    mapped;  /* client committed its first buffer */
    bool    active;  /* this view is currently visible */

    /* XDG signal listeners (registered when xdg_toplevel is attached) */
    struct wl_listener  map;
    struct wl_listener  unmap;
    struct wl_listener  commit;
    struct wl_listener  destroy;
    struct wl_listener  request_fullscreen;
    struct wl_listener  set_title;
    struct wl_listener  set_app_id;
};

/* Create a managed view slot and optionally launch its command.
 * Does NOT call view_launch() — caller must do that. */
struct view *view_create_managed(struct server *server, const char *name,
                                  const char *command, bool auto_restart);

/* Create an anonymous view directly from an incoming XDG toplevel. */
struct view *view_create_anonymous(struct server *server,
                                    struct wlr_xdg_toplevel *toplevel);

/* Attach an XDG toplevel to an existing (managed) view slot that has no
 * surface yet.  Sets up the scene tree and wires XDG signals. */
void view_attach_toplevel(struct view *view, struct wlr_xdg_toplevel *toplevel);

/* Fork/exec the view's command.  Sets view->pid on success.
 * The child is placed in its own session (setsid), so its whole process tree
 * shares a process group and can be signalled as a unit. */
bool view_launch(struct view *view);

/* SIGTERM the view's process group, so an app behind a forking wrapper script
 * dies along with its wrapper instead of being orphaned.  Returns false if the
 * view has no running process. */
bool view_terminate(struct view *view);

/* Make this view the visible one; hide all others; move keyboard/pointer focus. */
void view_activate(struct view *view);

/* Hide this view's scene tree (does not destroy it). */
void view_hide(struct view *view);

/* Remove the view from the server list and free it.
 * Usually called from handle_view_destroy; don't call directly unless the
 * toplevel has already been detached. */
void view_free(struct view *view);

/* Lookup functions */
struct view *view_for_id(struct server *server, int id);
struct view *view_for_name(struct server *server, const char *name);
struct view *view_for_app_id(struct server *server, const char *app_id);
struct view *view_for_pid(struct server *server, pid_t pid);

/* Find the view whose launched process is |pid| or any ancestor of |pid|.
 * A client is not always the process we exec'd: a wrapper script, or a shell
 * that forks rather than execs, leaves the client several generations down.
 * Walks up /proc until it finds a match, reaches init, or hits a depth cap.
 * Returns NULL if no view owns any process in that chain. */
struct view *view_for_pid_or_ancestor(struct server *server, pid_t pid);

/* First view in creation (config) order that is currently mapped, or NULL. */
struct view *view_first_mapped(struct server *server);

/* Return next/previous mapped view (wraps around), or NULL if only one view */
struct view *view_next(struct server *server, struct view *current);
struct view *view_prev(struct server *server, struct view *current);

#endif /* VIEW_H */
