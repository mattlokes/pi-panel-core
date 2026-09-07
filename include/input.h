#ifndef INPUT_H
#define INPUT_H

#include <stdbool.h>
#include <wayland-server-core.h>
#include <wlr/types/wlr_cursor.h>
#include <wlr/types/wlr_xcursor_manager.h>
#include <wlr/types/wlr_seat.h>

struct server; /* forward declaration */

struct input_manager {
    struct server              *server;

    struct wlr_seat            *seat;
    struct wlr_cursor          *cursor;
    struct wlr_xcursor_manager *xcursor_mgr;

    /* backend new_input signal */
    struct wl_listener          new_input;

    /* seat signals */
    struct wl_listener          request_set_cursor;
    struct wl_listener          request_set_selection;

    /* cursor/pointer signals */
    struct wl_listener          cursor_motion;
    struct wl_listener          cursor_motion_absolute;
    struct wl_listener          cursor_button;
    struct wl_listener          cursor_axis;
    struct wl_listener          cursor_frame;
};

bool input_init(struct input_manager *im, struct server *server);
void input_finish(struct input_manager *im);

#endif /* INPUT_H */
