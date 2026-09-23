#ifndef CLOCK_H
#define CLOCK_H

#include <stdbool.h>
#include <wayland-server-core.h>

#include "stb_truetype.h"

/* The clock overlay: the time, drawn by the compositor above every app.
 * Off until ctl turns it on with SetClock. */

#define CLOCK_FORMAT_MAX  64   /* bytes, including the NUL */
#define CLOCK_SIZE_MIN    8
#define CLOCK_SIZE_MAX    1000

#ifndef CLOCK_DEFAULT_FONT
#define CLOCK_DEFAULT_FONT "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
#endif

struct server;
struct wlr_scene_buffer;

/* The order matches the Clock.position enum in the interface. */
enum clock_position {
    CLOCK_TOP_LEFT,
    CLOCK_TOP_RIGHT,
    CLOCK_BOTTOM_LEFT,
    CLOCK_BOTTOM_RIGHT,
    CLOCK_CENTER,
};

struct clock_state {
    struct server           *server;

    bool                     enabled;
    char                     format[CLOCK_FORMAT_MAX];
    enum clock_position      position;
    int                      size;        /* text height in px; 0 = automatic */

    /* The font, read on the first enable */
    unsigned char           *font_data;
    stbtt_fontinfo           font;
    bool                     font_ready;

    struct wlr_scene_buffer *node;        /* under server->overlay_layer */
    char                     shown[256];  /* the text in node's buffer */
    int                      shown_px;
    bool                     draw_failed; /* already logged since SetClock */

    /* A CLOCK_REALTIME timerfd armed for the next minute (or second).  With
     * TFD_TIMER_CANCEL_ON_SET it also fires when the wall clock is stepped,
     * e.g. by NTP at boot. */
    int                      timer_fd;
    struct wl_event_source  *timer_source;
};

void clock_init(struct clock_state *cs, struct server *server);
void clock_finish(struct clock_state *cs);

/* Apply a SetClock call.  |format| NULL, |position| -1 and |size| -1 keep the
 * current value.  Returns false with *err set when the clock cannot be shown
 * (the font would not load); the settings are unchanged then. */
bool clock_configure(struct clock_state *cs, bool enabled, const char *format,
                     int position, int size, const char **err);

/* The primary output appeared or changed size: build or re-lay the overlay. */
void clock_output_changed(struct clock_state *cs);

const char *clock_position_name(enum clock_position position);
int clock_position_parse(const char *name);  /* -1 if unknown */

#endif /* CLOCK_H */
