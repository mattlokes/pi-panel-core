#include <stdlib.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/backend.h>
#include <wlr/types/wlr_cursor.h>
#include <wlr/types/wlr_input_device.h>
#include <wlr/types/wlr_keyboard.h>
#include <wlr/types/wlr_pointer.h>
#include <wlr/types/wlr_touch.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_xcursor_manager.h>
#include <wlr/util/log.h>
#include <xkbcommon/xkbcommon.h>

#include "server.h"
#include "view.h"
#include "input.h"

/* ---------------------------------------------------------------------------
 * Keyboard handlers
 * ---------------------------------------------------------------------------*/

/* Per-keyboard wrapper */
struct keyboard_device {
    struct input_manager *im;
    struct wlr_keyboard  *keyboard;
    struct wl_listener    key;
    struct wl_listener    modifiers;
    struct wl_listener    destroy;
};

static void handle_kb_modifiers(struct wl_listener *listener, void *data) {
    (void)data;
    struct keyboard_device *kb = wl_container_of(listener, kb, modifiers);
    wlr_seat_set_keyboard(kb->im->seat, kb->keyboard);
    wlr_seat_keyboard_notify_modifiers(kb->im->seat, &kb->keyboard->modifiers);
}

/* Compositor keybindings, checked before a key reaches the focused client.
 * A kiosk forwards essentially everything, so this stays deliberately small —
 * but there must be at least one way out, or the only exit is over SSH.
 *
 * Ctrl+Alt+Backspace  quit the compositor
 *
 * Returns true if the key was consumed. */
static bool handle_keybinding(struct input_manager *im, xkb_keysym_t sym) {
    switch (sym) {
    case XKB_KEY_BackSpace:
        wlr_log(WLR_INFO, "Ctrl+Alt+Backspace — shutting down");
        wl_display_terminate(im->server->display);
        return true;
    default:
        return false;
    }
}

static void handle_kb_key(struct wl_listener *listener, void *data) {
    struct keyboard_device          *kb    = wl_container_of(listener, kb, key);
    struct wlr_keyboard_key_event   *event = data;
    struct input_manager            *im    = kb->im;

    bool handled = false;

    /* Only presses, and only while both Ctrl and Alt are held. */
    uint32_t mods = wlr_keyboard_get_modifiers(kb->keyboard);
    const uint32_t want = WLR_MODIFIER_CTRL | WLR_MODIFIER_ALT;

    if ((mods & want) == want &&
        event->state == WL_KEYBOARD_KEY_STATE_PRESSED) {
        /* libinput keycodes are offset by 8 from xkb's. */
        const xkb_keysym_t *syms;
        int nsyms = xkb_state_key_get_syms(kb->keyboard->xkb_state,
                                            event->keycode + 8, &syms);
        for (int i = 0; i < nsyms; i++) {
            if (handle_keybinding(im, syms[i])) {
                handled = true;
            }
        }
    }

    if (handled) {
        return;
    }

    wlr_seat_set_keyboard(im->seat, kb->keyboard);
    wlr_seat_keyboard_notify_key(im->seat,
        event->time_msec, event->keycode, event->state);
}

static void handle_kb_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct keyboard_device *kb = wl_container_of(listener, kb, destroy);
    wl_list_remove(&kb->key.link);
    wl_list_remove(&kb->modifiers.link);
    wl_list_remove(&kb->destroy.link);
    free(kb);
}

static void setup_keyboard(struct input_manager *im,
                            struct wlr_input_device *device) {
    struct wlr_keyboard *keyboard = wlr_keyboard_from_input_device(device);

    /* Configure keymap */
    struct xkb_context *ctx = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    struct xkb_keymap  *map = xkb_keymap_new_from_names(ctx, NULL,
                                  XKB_KEYMAP_COMPILE_NO_FLAGS);
    wlr_keyboard_set_keymap(keyboard, map);
    xkb_keymap_unref(map);
    xkb_context_unref(ctx);

    wlr_keyboard_set_repeat_info(keyboard, 25, 600);

    /* Allocate per-keyboard listener wrapper */
    struct keyboard_device *kb = calloc(1, sizeof(struct keyboard_device));
    if (!kb) {
        wlr_log(WLR_ERROR, "OOM allocating keyboard_device");
        return;
    }
    kb->im       = im;
    kb->keyboard = keyboard;

    kb->key.notify       = handle_kb_key;
    kb->modifiers.notify = handle_kb_modifiers;
    kb->destroy.notify   = handle_kb_destroy;

    wl_signal_add(&keyboard->events.key,       &kb->key);
    wl_signal_add(&keyboard->events.modifiers, &kb->modifiers);
    wl_signal_add(&device->events.destroy,     &kb->destroy);

    wlr_seat_set_keyboard(im->seat, keyboard);
    wlr_log(WLR_DEBUG, "Keyboard device added");
}

/* ---------------------------------------------------------------------------
 * Pointer / cursor handlers
 * ---------------------------------------------------------------------------*/

static void handle_cursor_motion(struct wl_listener *listener, void *data) {
    struct input_manager            *im    = wl_container_of(listener, im, cursor_motion);
    struct wlr_pointer_motion_event *event = data;
    wlr_cursor_move(im->cursor, &event->pointer->base,
                    event->delta_x, event->delta_y);
    /* In a kiosk we don't need to route pointer focus — touch is primary */
    wlr_seat_pointer_notify_frame(im->seat);
}

static void handle_cursor_motion_absolute(struct wl_listener *listener,
                                           void *data) {
    struct input_manager                     *im    =
        wl_container_of(listener, im, cursor_motion_absolute);
    struct wlr_pointer_motion_absolute_event *event = data;
    wlr_cursor_warp_absolute(im->cursor, &event->pointer->base,
                              event->x, event->y);
    wlr_seat_pointer_notify_frame(im->seat);
}

static void handle_cursor_button(struct wl_listener *listener, void *data) {
    struct input_manager             *im    = wl_container_of(listener, im, cursor_button);
    struct wlr_pointer_button_event  *event = data;

    wlr_seat_pointer_notify_button(im->seat,
        event->time_msec, event->button, event->state);

    /* On button press, focus the active view if we have one */
    if (event->state == WL_POINTER_BUTTON_STATE_PRESSED) {
        struct server *server = im->server;
        if (server->active_view && server->active_view->mapped) {
            struct wlr_keyboard *kb = wlr_seat_get_keyboard(im->seat);
            if (kb) {
                wlr_seat_keyboard_notify_enter(im->seat,
                    server->active_view->xdg_toplevel->base->surface,
                    kb->keycodes, kb->num_keycodes, &kb->modifiers);
            }
        }
    }
    wlr_seat_pointer_notify_frame(im->seat);
}

static void handle_cursor_axis(struct wl_listener *listener, void *data) {
    struct input_manager          *im    = wl_container_of(listener, im, cursor_axis);
    struct wlr_pointer_axis_event *event = data;
    wlr_seat_pointer_notify_axis(im->seat,
        event->time_msec, event->orientation,
        event->delta, event->delta_discrete, event->source,
        event->relative_direction);
}

static void handle_cursor_frame(struct wl_listener *listener, void *data) {
    (void)data;
    struct input_manager *im = wl_container_of(listener, im, cursor_frame);
    wlr_seat_pointer_notify_frame(im->seat);
}

/* ---------------------------------------------------------------------------
 * Touch handlers
 * Per-touch-device wrapper (similar pattern to keyboard)
 * ---------------------------------------------------------------------------*/

struct touch_device {
    struct input_manager *im;
    struct wlr_touch     *touch;
    struct wl_listener    down;
    struct wl_listener    up;
    struct wl_listener    motion;
    struct wl_listener    frame;
    struct wl_listener    destroy;
};

static struct wlr_surface *active_surface(struct input_manager *im) {
    struct server *server = im->server;
    if (server->active_view &&
        server->active_view->mapped &&
        server->active_view->xdg_toplevel) {
        return server->active_view->xdg_toplevel->base->surface;
    }
    return NULL;
}

static void handle_touch_down(struct wl_listener *listener, void *data) {
    struct touch_device         *td    = wl_container_of(listener, td, down);
    struct wlr_touch_down_event *event = data;
    struct input_manager        *im    = td->im;
    struct server               *server = im->server;

    struct wlr_surface *surface = active_surface(im);
    if (!surface) return;

    /* Touch coords are normalised [0,1] relative to output */
    double sx = event->x * server->output_width;
    double sy = event->y * server->output_height;

    wlr_seat_touch_notify_down(im->seat, surface,
        event->time_msec, event->touch_id, sx, sy);
}

static void handle_touch_up(struct wl_listener *listener, void *data) {
    struct touch_device       *td    = wl_container_of(listener, td, up);
    struct wlr_touch_up_event *event = data;
    wlr_seat_touch_notify_up(td->im->seat,
        event->time_msec, event->touch_id);
}

static void handle_touch_motion(struct wl_listener *listener, void *data) {
    struct touch_device            *td    = wl_container_of(listener, td, motion);
    struct wlr_touch_motion_event  *event = data;
    struct input_manager           *im    = td->im;
    struct server                  *server = im->server;

    double sx = event->x * server->output_width;
    double sy = event->y * server->output_height;

    wlr_seat_touch_notify_motion(im->seat,
        event->time_msec, event->touch_id, sx, sy);
}

static void handle_touch_frame(struct wl_listener *listener, void *data) {
    (void)data;
    struct touch_device *td = wl_container_of(listener, td, frame);
    wlr_seat_touch_notify_frame(td->im->seat);
}

static void handle_touch_destroy(struct wl_listener *listener, void *data) {
    (void)data;
    struct touch_device *td = wl_container_of(listener, td, destroy);
    wl_list_remove(&td->down.link);
    wl_list_remove(&td->up.link);
    wl_list_remove(&td->motion.link);
    wl_list_remove(&td->frame.link);
    wl_list_remove(&td->destroy.link);
    free(td);
}

static void setup_touch(struct input_manager *im,
                         struct wlr_input_device *device) {
    struct touch_device *td = calloc(1, sizeof(struct touch_device));
    if (!td) {
        wlr_log(WLR_ERROR, "OOM allocating touch_device");
        return;
    }
    td->im    = im;
    td->touch = wlr_touch_from_input_device(device);

    td->down.notify    = handle_touch_down;
    td->up.notify      = handle_touch_up;
    td->motion.notify  = handle_touch_motion;
    td->frame.notify   = handle_touch_frame;
    td->destroy.notify = handle_touch_destroy;

    wl_signal_add(&td->touch->events.down,    &td->down);
    wl_signal_add(&td->touch->events.up,      &td->up);
    wl_signal_add(&td->touch->events.motion,  &td->motion);
    wl_signal_add(&td->touch->events.frame,   &td->frame);
    wl_signal_add(&device->events.destroy,    &td->destroy);

    /* Update seat capabilities to advertise touch */
    uint32_t caps = WL_SEAT_CAPABILITY_TOUCH;
    if (im->seat->capabilities & WL_SEAT_CAPABILITY_POINTER) {
        caps |= WL_SEAT_CAPABILITY_POINTER;
    }
    if (im->seat->capabilities & WL_SEAT_CAPABILITY_KEYBOARD) {
        caps |= WL_SEAT_CAPABILITY_KEYBOARD;
    }
    wlr_seat_set_capabilities(im->seat, caps);

    wlr_log(WLR_DEBUG, "Touch device added");
}

/* ---------------------------------------------------------------------------
 * Seat signal handlers
 * ---------------------------------------------------------------------------*/

static void handle_request_set_cursor(struct wl_listener *listener,
                                       void *data) {
    (void)listener;
    (void)data;
    /* Kiosk: ignore cursor image requests — no visible cursor */
}

static void handle_request_set_selection(struct wl_listener *listener,
                                          void *data) {
    struct input_manager                      *im    =
        wl_container_of(listener, im, request_set_selection);
    struct wlr_seat_request_set_selection_event *event = data;
    wlr_seat_set_selection(im->seat, event->source, event->serial);
}

/* ---------------------------------------------------------------------------
 * new_input handler — routes devices to setup functions
 * ---------------------------------------------------------------------------*/

static void handle_new_input(struct wl_listener *listener, void *data) {
    struct input_manager    *im     = wl_container_of(listener, im, new_input);
    struct wlr_input_device *device = data;

    switch (device->type) {
    case WLR_INPUT_DEVICE_KEYBOARD:
        setup_keyboard(im, device);
        wlr_seat_set_capabilities(im->seat,
            im->seat->capabilities | WL_SEAT_CAPABILITY_KEYBOARD);
        break;

    case WLR_INPUT_DEVICE_POINTER:
        wlr_cursor_attach_input_device(im->cursor, device);
        wlr_seat_set_capabilities(im->seat,
            im->seat->capabilities | WL_SEAT_CAPABILITY_POINTER);
        break;

    case WLR_INPUT_DEVICE_TOUCH:
        setup_touch(im, device);
        break;

    default:
        wlr_log(WLR_DEBUG, "Ignoring input device type %d", device->type);
        break;
    }
}

/* ---------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------------*/

bool input_init(struct input_manager *im, struct server *server) {
    im->server = server;

    im->cursor = wlr_cursor_create();
    if (!im->cursor) {
        wlr_log(WLR_ERROR, "Failed to create cursor");
        return false;
    }
    wlr_cursor_attach_output_layout(im->cursor, server->output_layout);

    /* Load a default cursor theme (24 px, scale 1).
     * On a touch-only kiosk this just means we have a fallback if needed. */
    im->xcursor_mgr = wlr_xcursor_manager_create(NULL, 24);
    wlr_xcursor_manager_load(im->xcursor_mgr, 1.0f);

    im->seat = wlr_seat_create(server->display, "seat0");
    if (!im->seat) {
        wlr_log(WLR_ERROR, "Failed to create seat");
        return false;
    }

    /* Wire seat signals */
    im->request_set_cursor.notify    = handle_request_set_cursor;
    im->request_set_selection.notify = handle_request_set_selection;
    wl_signal_add(&im->seat->events.request_set_cursor,    &im->request_set_cursor);
    wl_signal_add(&im->seat->events.request_set_selection, &im->request_set_selection);

    /* Wire cursor signals */
    im->cursor_motion.notify           = handle_cursor_motion;
    im->cursor_motion_absolute.notify  = handle_cursor_motion_absolute;
    im->cursor_button.notify           = handle_cursor_button;
    im->cursor_axis.notify             = handle_cursor_axis;
    im->cursor_frame.notify            = handle_cursor_frame;

    wl_signal_add(&im->cursor->events.motion,          &im->cursor_motion);
    wl_signal_add(&im->cursor->events.motion_absolute, &im->cursor_motion_absolute);
    wl_signal_add(&im->cursor->events.button,          &im->cursor_button);
    wl_signal_add(&im->cursor->events.axis,            &im->cursor_axis);
    wl_signal_add(&im->cursor->events.frame,           &im->cursor_frame);

    /* Wire backend new_input signal */
    im->new_input.notify = handle_new_input;
    wl_signal_add(&server->backend->events.new_input, &im->new_input);

    return true;
}

void input_finish(struct input_manager *im) {
    wl_list_remove(&im->new_input.link);
    wl_list_remove(&im->request_set_cursor.link);
    wl_list_remove(&im->request_set_selection.link);
    wl_list_remove(&im->cursor_motion.link);
    wl_list_remove(&im->cursor_motion_absolute.link);
    wl_list_remove(&im->cursor_button.link);
    wl_list_remove(&im->cursor_axis.link);
    wl_list_remove(&im->cursor_frame.link);

    if (im->xcursor_mgr) {
        wlr_xcursor_manager_destroy(im->xcursor_mgr);
        im->xcursor_mgr = NULL;
    }
    if (im->cursor) {
        wlr_cursor_destroy(im->cursor);
        im->cursor = NULL;
    }
}
