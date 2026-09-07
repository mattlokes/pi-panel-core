#ifndef SERVER_H
#define SERVER_H

#include <stdbool.h>
#include <sys/types.h>

#include <wayland-server-core.h>
#include <wlr/backend.h>
#include <wlr/render/allocator.h>
#include <wlr/render/wlr_renderer.h>
#include <wlr/types/wlr_compositor.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/types/wlr_subcompositor.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/util/log.h>

#include "input.h"
#include "ipc.h"
#include "transition.h"

struct view; /* forward declaration — full definition in view.h */

/* Per-output wrapper holding listeners tied to the output's lifetime */
struct output {
    struct server          *server;
    struct wlr_output      *wlr_output;
    struct wl_listener      frame;
    struct wl_listener      request_state;
    struct wl_listener      destroy;
    struct wl_list          link;   /* link in server->outputs */
};

struct server {
    /* Wayland core */
    struct wl_display              *display;
    struct wl_event_loop           *event_loop;
    char                           *wayland_socket; /* e.g. "wayland-1" */

    /* wlroots backend stack */
    struct wlr_backend             *backend;
    struct wlr_renderer            *renderer;
    struct wlr_allocator           *allocator;

    /* Wayland protocol globals */
    struct wlr_compositor          *compositor;
    struct wlr_subcompositor       *subcompositor;
    struct wlr_data_device_manager *data_device_mgr;

    /* Output management */
    struct wl_list                  outputs;        /* list of struct output */
    struct wlr_output_layout       *output_layout;
    struct wlr_output              *primary_output; /* first connected output */
    int                             output_width;
    int                             output_height;

    /* Scene graph */
    struct wlr_scene               *scene;
    struct wlr_scene_output_layout *scene_output_layout;
    struct wlr_scene_tree          *app_layer;  /* lower z-order: app surfaces */
    struct wlr_scene_rect          *fade_rect;  /* topmost: transition overlay */

    /* XDG shell */
    struct wlr_xdg_shell           *xdg_shell;

    /* Backend / shell signals */
    struct wl_listener              new_output;
    struct wl_listener              new_xdg_toplevel;

    /* View management */
    struct wl_list                  views;       /* list of struct view */
    int                             view_count;
    int                             next_view_id;
    struct view                    *active_view; /* NULL if none */
    /* False until the controller issues its first switch.  While false, the
     * first *configured* view is preferred over whichever client wins the
     * race to map first. */
    bool                            view_selected_by_user;

    /* Child process management */
    struct wl_event_source         *sigchld_source;

    /* Subsystems */
    struct input_manager            input;
    struct ipc_server               ipc;
    struct transition_state         transition;
};

struct server_config {
    const char *wayland_socket; /* NULL = auto-assign */
    const char *ipc_socket;     /* NULL = use default */
    bool        log_debug;
};

bool server_init(struct server *server, const struct server_config *cfg);
void server_finish(struct server *server);

/* Called by handle_new_xdg_surface to assign a toplevel to a view slot */
void server_assign_toplevel(struct server *server,
                            struct wlr_xdg_toplevel *toplevel,
                            pid_t client_pid);

#endif /* SERVER_H */
