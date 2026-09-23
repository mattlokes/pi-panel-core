#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <signal.h>

#include <wayland-server-core.h>
#include <wlr/backend.h>
#include <wlr/render/allocator.h>
#include <wlr/render/wlr_renderer.h>
#include <wlr/types/wlr_compositor.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_subcompositor.h>
#include <wlr/types/wlr_viewporter.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>

#include "server.h"
#include "slot.h"
#include "view.h"

/* ---------------------------------------------------------------------------
 * Output event handlers
 * ---------------------------------------------------------------------------*/

static void handle_output_frame(struct wl_listener *listener, void *data) {
    (void)data;
    struct output *output = wl_container_of(listener, output, frame);
    struct wlr_scene_output *scene_output =
        wlr_scene_get_scene_output(output->server->scene, output->wlr_output);
    if (!scene_output) {
        return;
    }
    wlr_scene_output_commit(scene_output, NULL);

    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    wlr_scene_output_send_frame_done(scene_output, &now);
}

static void handle_output_request_state(struct wl_listener *listener,
                                         void *data) {
    struct output *output = wl_container_of(listener, output, request_state);
    const struct wlr_output_event_request_state *event = data;
    wlr_output_commit_state(output->wlr_output, event->state);
}

static void handle_output_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct output *output = wl_container_of(listener, output, destroy);

    wl_list_remove(&output->frame.link);
    wl_list_remove(&output->request_state.link);
    wl_list_remove(&output->destroy.link);
    wl_list_remove(&output->link);

    if (output->server->primary_output == output->wlr_output) {
        output->server->primary_output = NULL;
        output->server->output_width   = 0;
        output->server->output_height  = 0;
    }

    free(output);
}

static void handle_new_output(struct wl_listener *listener, void *data) {
    struct server     *server     = wl_container_of(listener, server, new_output);
    struct wlr_output *wlr_output = data;

    wlr_output_init_render(wlr_output, server->allocator, server->renderer);

    /* Set preferred mode and enable the output */
    struct wlr_output_state state;
    wlr_output_state_init(&state);
    wlr_output_state_set_enabled(&state, true);

    struct wlr_output_mode *mode = wlr_output_preferred_mode(wlr_output);
    if (mode) {
        wlr_output_state_set_mode(&state, mode);
    }

    if (!wlr_output_commit_state(wlr_output, &state)) {
        wlr_log(WLR_ERROR, "Failed to commit initial output state");
        wlr_output_state_finish(&state);
        return;
    }
    wlr_output_state_finish(&state);

    /* Integrate output with layout and scene */
    struct wlr_output_layout_output *l_output =
        wlr_output_layout_add_auto(server->output_layout, wlr_output);
    struct wlr_scene_output *scene_output =
        wlr_scene_output_create(server->scene, wlr_output);
    wlr_scene_output_layout_add_output(server->scene_output_layout,
                                        l_output, scene_output);

    /* Allocate per-output wrapper */
    struct output *out = calloc(1, sizeof(struct output));
    if (!out) {
        wlr_log(WLR_ERROR, "OOM allocating output wrapper");
        return;
    }
    out->server     = server;
    out->wlr_output = wlr_output;

    out->frame.notify          = handle_output_frame;
    out->request_state.notify  = handle_output_request_state;
    out->destroy.notify        = handle_output_destroy;

    wl_signal_add(&wlr_output->events.frame,         &out->frame);
    wl_signal_add(&wlr_output->events.request_state, &out->request_state);
    wl_signal_add(&wlr_output->events.destroy,       &out->destroy);
    wl_list_insert(&server->outputs, &out->link);

    /* First output becomes the primary output */
    if (!server->primary_output) {
        server->primary_output = wlr_output;
        server->output_width   = wlr_output->width;
        server->output_height  = wlr_output->height;

        wlr_log(WLR_INFO, "Primary output: %s (%dx%d)",
            wlr_output->name, wlr_output->width, wlr_output->height);

        /* Now that we have dimensions, create the full-screen fade overlay.
         * It sits at the root scene level, above app_layer, and starts
         * transparent and disabled. */
        float transparent[4] = {0.0f, 0.0f, 0.0f, 0.0f};
        server->fade_rect = wlr_scene_rect_create(
            &server->scene->tree,
            server->output_width,
            server->output_height,
            transparent);
        if (!server->fade_rect) {
            wlr_log(WLR_ERROR, "Failed to create fade_rect");
        } else {
            wlr_scene_node_set_enabled(&server->fade_rect->node, false);
            /* Overlays such as the clock stay visible through a fade. */
            wlr_scene_node_place_below(&server->fade_rect->node,
                                       &server->overlay_layer->node);
        }

        clock_output_changed(&server->clock);
    }
}

/* ---------------------------------------------------------------------------
 * XDG shell handler
 * ---------------------------------------------------------------------------*/

/* Note: we listen on xdg_shell.events.new_toplevel, NOT .new_surface.
 * new_surface fires when the xdg_surface is created, which is *before* the
 * client has assigned it a role — at that point role is still
 * WLR_XDG_SURFACE_ROLE_NONE and there is no toplevel to attach.  new_toplevel
 * fires once the role is bound, which is what we actually want.  Popups need
 * no handling here: wlr_scene_xdg_surface_create() manages them once their
 * parent surface is in the scene. */
static void handle_new_xdg_toplevel(struct wl_listener *listener, void *data) {
    struct server           *server   = wl_container_of(listener, server, new_xdg_toplevel);
    struct wlr_xdg_toplevel *toplevel = data;

    /* The client's pid leads to its systemd unit, which picks its slot. */
    pid_t pid = 0;
    struct wl_client *wl_client =
        wl_resource_get_client(toplevel->base->resource);
    wl_client_get_credentials(wl_client, &pid, NULL, NULL);

    view_create(server, toplevel, pid);
}

/* ---------------------------------------------------------------------------
 * SIGINT/SIGTERM — leave the event loop so server_finish() can run
 * ---------------------------------------------------------------------------*/

static int handle_terminate_signal(int sig, void *data) {
    struct server *server = data;
    wlr_log(WLR_INFO, "Caught signal %d — shutting down", sig);
    wl_display_terminate(server->display);
    return 0;
}

/* ---------------------------------------------------------------------------
 * Output power
 * ---------------------------------------------------------------------------*/

bool server_set_output_power(struct server *server, bool on) {
    bool ok = true;
    struct output *out;
    wl_list_for_each(out, &server->outputs, link) {
        struct wlr_output_state state;
        wlr_output_state_init(&state);
        wlr_output_state_set_enabled(&state, on);
        if (on) {
            /* A disabled output may have dropped its mode; ask for the
             * preferred one again, as at startup. */
            struct wlr_output_mode *mode = wlr_output_preferred_mode(out->wlr_output);
            if (mode) {
                wlr_output_state_set_mode(&state, mode);
            }
        }
        if (!wlr_output_commit_state(out->wlr_output, &state)) {
            wlr_log(WLR_ERROR, "Failed to turn output %s %s",
                out->wlr_output->name, on ? "on" : "off");
            ok = false;
        }
        wlr_output_state_finish(&state);
    }
    if (ok && server->output_power != on) {
        server->output_power = on;
        wlr_log(WLR_INFO, "Output power %s", on ? "on" : "off");
        ipc_event(server, "output_power_changed");
    }
    return ok;
}

/* ---------------------------------------------------------------------------
 * server_init / server_finish
 * ---------------------------------------------------------------------------*/

bool server_init(struct server *server, const struct server_config *cfg) {
    wl_list_init(&server->outputs);
    wl_list_init(&server->slots);
    wl_list_init(&server->views);
    server->next_id      = 1;
    server->active_view  = NULL;
    server->output_power = true;

    /* 1. Create Wayland display and event loop */
    server->display    = wl_display_create();
    server->event_loop = wl_display_get_event_loop(server->display);
    if (!server->display || !server->event_loop) {
        wlr_log(WLR_ERROR, "Failed to create Wayland display");
        return false;
    }

    /* 2. Backend — auto-selects X11/Wayland/DRM based on environment */
    server->backend = wlr_backend_autocreate(server->event_loop, NULL);
    if (!server->backend) {
        wlr_log(WLR_ERROR, "Failed to create backend");
        return false;
    }

    /* 3. Renderer */
    server->renderer = wlr_renderer_autocreate(server->backend);
    if (!server->renderer) {
        wlr_log(WLR_ERROR, "Failed to create renderer");
        return false;
    }
    wlr_renderer_init_wl_display(server->renderer, server->display);

    /* 4. Allocator */
    server->allocator = wlr_allocator_autocreate(server->backend,
                                                   server->renderer);
    if (!server->allocator) {
        wlr_log(WLR_ERROR, "Failed to create allocator");
        return false;
    }

    /* 5. Wayland protocol globals */
    server->compositor =
        wlr_compositor_create(server->display, 5, server->renderer);
    server->subcompositor   = wlr_subcompositor_create(server->display);
    server->data_device_mgr = wlr_data_device_manager_create(server->display);
    /* wp_viewporter lets a client present a buffer at a different size than
     * it was rendered.  Video clients depend on it: GStreamer's waylandsink
     * scales frames to the window this way, and without it a 640x360 stream
     * is drawn unscaled in a corner of the screen. */
    wlr_viewporter_create(server->display);

    /* 6. Output layout + scene graph */
    server->output_layout = wlr_output_layout_create(server->display);

    server->scene = wlr_scene_create();
    if (!server->scene) {
        wlr_log(WLR_ERROR, "Failed to create scene");
        return false;
    }
    server->scene_output_layout =
        wlr_scene_attach_output_layout(server->scene, server->output_layout);

    /* Create the application layer (scene tree for view surfaces) */
    server->app_layer =
        wlr_scene_tree_create(&server->scene->tree);
    if (!server->app_layer) {
        wlr_log(WLR_ERROR, "Failed to create app_layer scene tree");
        return false;
    }
    /* Overlays (the clock) go above everything, the fade included */
    server->overlay_layer =
        wlr_scene_tree_create(&server->scene->tree);
    if (!server->overlay_layer) {
        wlr_log(WLR_ERROR, "Failed to create overlay_layer scene tree");
        return false;
    }
    /* fade_rect is created in handle_new_output once dimensions are known,
     * and placed between the two layers */

    /* 7. XDG shell (protocol version 3) */
    server->xdg_shell = wlr_xdg_shell_create(server->display, 3);
    if (!server->xdg_shell) {
        wlr_log(WLR_ERROR, "Failed to create XDG shell");
        return false;
    }

    /* 8. Wire signals */
    server->new_output.notify       = handle_new_output;
    server->new_xdg_toplevel.notify = handle_new_xdg_toplevel;
    wl_signal_add(&server->backend->events.new_output,
                  &server->new_output);
    wl_signal_add(&server->xdg_shell->events.new_toplevel,
                  &server->new_xdg_toplevel);

    /* 9. Input subsystem */
    if (!input_init(&server->input, server)) {
        wlr_log(WLR_ERROR, "Failed to initialise input");
        return false;
    }

    /* 10. IPC server */
    const char *ipc_path = cfg->ipc_socket ? cfg->ipc_socket : IPC_DEFAULT_PATH;
    if (!ipc_init(&server->ipc, server, ipc_path)) {
        wlr_log(WLR_ERROR, "Failed to initialise IPC server");
        return false;
    }

    /* 11. Transition engine and the clock overlay (off until SetClock) */
    transition_init(&server->transition, server);
    clock_init(&server->clock, server);

    /* 12. Exit cleanly on INT/TERM so server_finish() runs and the IPC socket
     *     is unlinked.  (systemd stops us with SIGTERM.) */
    server->sigint_source = wl_event_loop_add_signal(
        server->event_loop, SIGINT, handle_terminate_signal, server);
    server->sigterm_source = wl_event_loop_add_signal(
        server->event_loop, SIGTERM, handle_terminate_signal, server);

    /* 13. Start backend — fires new_output and new_input for existing devices */
    if (!wlr_backend_start(server->backend)) {
        wlr_log(WLR_ERROR, "Failed to start backend");
        return false;
    }

    /* 14. Bind a Wayland display socket.
     *
     * Note the two libwayland entry points differ: wl_display_add_socket()
     * returns an int (0 on success) and takes the name, while
     * wl_display_add_socket_auto() returns the name it picked, or NULL. */
    const char *socket_name;
    if (cfg->wayland_socket) {
        if (wl_display_add_socket(server->display, cfg->wayland_socket) != 0) {
            wlr_log(WLR_ERROR, "Failed to bind Wayland socket '%s'",
                cfg->wayland_socket);
            return false;
        }
        socket_name = cfg->wayland_socket;
    } else {
        socket_name = wl_display_add_socket_auto(server->display);
        if (!socket_name) {
            wlr_log(WLR_ERROR, "Failed to auto-bind a Wayland socket");
            return false;
        }
    }
    server->wayland_socket = strdup(socket_name);

    return true;
}

void server_finish(struct server *server) {
    transition_finish(&server->transition);
    clock_finish(&server->clock);
    ipc_finish(&server->ipc);
    input_finish(&server->input);

    if (server->sigint_source) {
        wl_event_source_remove(server->sigint_source);
        server->sigint_source = NULL;
    }
    if (server->sigterm_source) {
        wl_event_source_remove(server->sigterm_source);
        server->sigterm_source = NULL;
    }

    /* Unhook the backend and shell listeners before anything is destroyed:
     * wlroots asserts a signal's listener list is empty before freeing the
     * object that owns it (wlr_backend_finish).  input_finish() above has
     * already removed its own new_input listener. */
    wl_list_remove(&server->new_output.link);
    wl_list_remove(&server->new_xdg_toplevel.link);

    /* Destroy all views and slots */
    view_free_all(server);
    slot_free_all(server);

    /* Destroy outputs.  Unhook each output's listeners first — both for the
     * assertion above, and so tearing down the backend cannot re-enter
     * handle_output_destroy() on wrappers we have already freed. */
    struct output *out, *otmp;
    wl_list_for_each_safe(out, otmp, &server->outputs, link) {
        wl_list_remove(&out->frame.link);
        wl_list_remove(&out->request_state.link);
        wl_list_remove(&out->destroy.link);
        wl_list_remove(&out->link);
        free(out);
    }

    if (server->backend)  { wlr_backend_destroy(server->backend); }
    if (server->display)  { wl_display_destroy_clients(server->display);
                             wl_display_destroy(server->display); }

    free(server->wayland_socket);
    server->wayland_socket = NULL;
}
