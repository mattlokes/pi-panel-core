#ifndef VIEW_H
#define VIEW_H

#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/types/wlr_scene.h>

#include "server.h"

struct slot;

/* One client window.  Created when an XDG toplevel appears and freed when it
 * is destroyed; the process that owns it is managed by systemd, never by us. */
struct view {
    struct server                *server;
    struct wl_list                link;    /* link in server->views */

    struct wlr_xdg_toplevel      *xdg_toplevel;
    struct wlr_scene_tree        *scene_tree;
    struct wlr_scene_tree        *surface_tree;   /* from wlr_scene_xdg_surface_create */

    int     id;        /* unique, shares the counter with slots, never reused */
    struct slot *slot; /* NULL while unregistered ("anon-<id>") */

    char   *app_id;    /* reported by the client (may be NULL) */
    char   *title;     /* reported by the client (may be NULL) */
    char   *unit;      /* systemd unit of the client process (may be NULL) */
    pid_t   pid;

    bool    mapped;    /* client committed its first buffer */
    bool    active;    /* this view is currently visible */

    struct wl_listener  map;
    struct wl_listener  unmap;
    struct wl_listener  commit;
    struct wl_listener  destroy;
    struct wl_listener  request_fullscreen;
    struct wl_listener  set_title;
    struct wl_listener  set_app_id;
};

/* Wrap a new toplevel in a view and place it in a matching slot if one is
 * free.  |pid| is the client's pid, used to find its systemd unit. */
struct view *view_create(struct server *server,
                         struct wlr_xdg_toplevel *toplevel, pid_t pid);

/* Shutdown only: unhook and free every view without emitting events. */
void view_free_all(struct server *server);

/* Make this view the visible one; hide all others; move keyboard focus. */
void view_activate(struct view *view);

/* Hide this view's scene tree (does not destroy it). */
void view_hide(struct view *view);

/* The name a view is addressed by: its slot's name, or "anon-<id>". */
const char *view_name(const struct view *view, char *buf, size_t len);

struct view *view_for_id(struct server *server, int id);
struct view *view_for_app_id(struct server *server, const char *app_id);

/* Next mapped view *in a registered slot* after |current|, wrapping around;
 * |current| itself if there is no other. */
struct view *view_next_registered(struct server *server, struct view *current);

/* The systemd unit a process runs in, read from /proc/<pid>/cgroup: the
 * innermost ".service" or ".scope" component.  Caller frees; NULL if unknown. */
char *unit_for_pid(pid_t pid);

#endif /* VIEW_H */
