#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/timerfd.h>
#include <time.h>
#include <unistd.h>

#include <drm_fourcc.h>
#include <wlr/interfaces/wlr_buffer.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/util/log.h>

#include "clock.h"
#include "clock_render.h"
#include "server.h"

#define FONT_MAX_BYTES (32 * 1024 * 1024)

static const char *const position_names[] = {
    [CLOCK_TOP_LEFT]     = "top_left",
    [CLOCK_TOP_RIGHT]    = "top_right",
    [CLOCK_BOTTOM_LEFT]  = "bottom_left",
    [CLOCK_BOTTOM_RIGHT] = "bottom_right",
    [CLOCK_CENTER]       = "center",
};

const char *clock_position_name(enum clock_position position) {
    return position_names[position];
}

int clock_position_parse(const char *name) {
    for (size_t i = 0; i < sizeof(position_names) / sizeof(position_names[0]); i++) {
        if (strcmp(name, position_names[i]) == 0) {
            return (int)i;
        }
    }
    return -1;
}

/* ---------------------------------------------------------------------------
 * A wlr_buffer over pixels in memory
 *
 * The renderer (gles2 or pixman) uploads it through data-pointer access.
 * ---------------------------------------------------------------------------*/

struct clock_buffer {
    struct wlr_buffer base;
    uint32_t         *pixels;
};

static void clock_buffer_destroy(struct wlr_buffer *wlr_buffer) {
    struct clock_buffer *buf = wl_container_of(wlr_buffer, buf, base);
    wlr_buffer_finish(wlr_buffer);
    free(buf->pixels);
    free(buf);
}

static bool clock_buffer_begin_access(struct wlr_buffer *wlr_buffer,
        uint32_t flags, void **data, uint32_t *format, size_t *stride) {
    struct clock_buffer *buf = wl_container_of(wlr_buffer, buf, base);
    if (flags & WLR_BUFFER_DATA_PTR_ACCESS_WRITE) {
        return false;
    }
    *data   = buf->pixels;
    *format = DRM_FORMAT_ARGB8888;
    *stride = (size_t)wlr_buffer->width * 4;
    return true;
}

static void clock_buffer_end_access(struct wlr_buffer *wlr_buffer) {
    (void)wlr_buffer;
}

static const struct wlr_buffer_impl clock_buffer_impl = {
    .destroy               = clock_buffer_destroy,
    .begin_data_ptr_access = clock_buffer_begin_access,
    .end_data_ptr_access   = clock_buffer_end_access,
};

/* ---------------------------------------------------------------------------
 * Font
 * ---------------------------------------------------------------------------*/

static bool load_font(struct clock_state *cs, const char **err) {
    if (cs->font_ready) {
        return true;
    }
    const char *path = CLOCK_DEFAULT_FONT;
    FILE *f = fopen(path, "rb");
    if (!f) {
        wlr_log(WLR_ERROR, "Clock: cannot open font %s: %s", path, strerror(errno));
        *err = "cannot open the clock font";
        return false;
    }
    unsigned char *data = NULL;
    long len = -1;
    if (fseek(f, 0, SEEK_END) == 0) {
        len = ftell(f);
        rewind(f);
    }
    if (len > 0 && len <= FONT_MAX_BYTES) {
        data = malloc((size_t)len);
        if (data && fread(data, 1, (size_t)len, f) != (size_t)len) {
            free(data);
            data = NULL;
        }
    }
    fclose(f);
    int offset = data ? stbtt_GetFontOffsetForIndex(data, 0) : -1;
    if (offset < 0 || !stbtt_InitFont(&cs->font, data, offset)) {
        wlr_log(WLR_ERROR, "Clock: %s is not a usable font", path);
        free(data);
        *err = "the clock font is not a usable TrueType font";
        return false;
    }
    cs->font_data  = data;
    cs->font_ready = true;
    wlr_log(WLR_INFO, "Clock: font %s", path);
    return true;
}

/* ---------------------------------------------------------------------------
 * Drawing
 * ---------------------------------------------------------------------------*/

static int effective_px(struct clock_state *cs) {
    if (cs->size > 0) {
        return cs->size;
    }
    int px = cs->server->output_height / 16;
    return px < CLOCK_SIZE_MIN ? CLOCK_SIZE_MIN : px;
}

/* Does |fmt| show seconds?  Then the clock ticks every second. */
static bool format_has_seconds(const char *fmt) {
    for (const char *p = fmt; *p; p++) {
        if (*p != '%') {
            continue;
        }
        p++;
        while (*p && strchr("-_0^#", *p)) { p++; }
        while (*p >= '0' && *p <= '9')    { p++; }
        if (*p == 'E' || *p == 'O')       { p++; }
        if (!*p) {
            break;
        }
        if (strchr("STrsXc+", *p)) {
            return true;
        }
    }
    return false;
}

static void place_node(struct clock_state *cs, const struct clock_image *img) {
    struct server *server = cs->server;
    int ow = server->output_width, oh = server->output_height;
    int text_w = img->text_w, text_h = img->text_h;
    int margin = effective_px(cs) / 2;
    int x, y;  /* where the text's line box goes on the output */

    switch (cs->position) {
    case CLOCK_TOP_LEFT:
        x = margin;                  y = margin;                  break;
    case CLOCK_TOP_RIGHT:
        x = ow - margin - text_w;    y = margin;                  break;
    case CLOCK_BOTTOM_LEFT:
        x = margin;                  y = oh - margin - text_h;    break;
    case CLOCK_CENTER:
        x = (ow - text_w) / 2;       y = (oh - text_h) / 2;       break;
    case CLOCK_BOTTOM_RIGHT:
    default:
        x = ow - margin - text_w;    y = oh - margin - text_h;    break;
    }
    wlr_scene_node_set_position(&cs->node->node, x - img->text_x, y - img->text_y);
}

static void arm_timer(struct clock_state *cs) {
    if (cs->timer_fd < 0) {
        return;
    }
    struct itimerspec its = {0};
    if (cs->enabled) {
        struct timespec now;
        clock_gettime(CLOCK_REALTIME, &now);
        time_t period = format_has_seconds(cs->format) ? 1 : 60;
        its.it_value.tv_sec = (now.tv_sec / period + 1) * period;
    }
    /* An all-zero it_value disarms it. */
    if (timerfd_settime(cs->timer_fd,
            TFD_TIMER_ABSTIME | TFD_TIMER_CANCEL_ON_SET, &its, NULL) < 0) {
        wlr_log(WLR_ERROR, "Clock: timerfd_settime: %s", strerror(errno));
    }
}

/* Bring the overlay up to date: build it, redraw it if the text changed,
 * place it.  |force| redraws even when the text is the same. */
static void clock_update(struct clock_state *cs, bool force) {
    struct server *server = cs->server;

    if (!cs->enabled) {
        if (cs->node) {
            wlr_scene_node_set_enabled(&cs->node->node, false);
        }
        return;
    }
    if (!server->primary_output || !cs->font_ready) {
        return;  /* clock_output_changed() calls again once there is one */
    }
    if (!cs->node) {
        cs->node = wlr_scene_buffer_create(server->overlay_layer, NULL);
        if (!cs->node) {
            wlr_log(WLR_ERROR, "Clock: cannot create the scene node");
            return;
        }
    }

    /* Pick up a changed /etc/localtime (timedatectl set-timezone). */
    tzset();
    time_t t = time(NULL);
    struct tm tm;
    localtime_r(&t, &tm);
    char text[sizeof(cs->shown)];
    size_t n = strftime(text, sizeof(text), cs->format, &tm);
    if (n == 0) {
        text[0] = '\0';  /* empty, or too long for the buffer */
    }

    int px = effective_px(cs);
    if (force || px != cs->shown_px || strcmp(text, cs->shown) != 0) {
        struct clock_image img;
        if (!clock_render_text(&cs->font, text, px, &img)) {
            /* Say so once per SetClock, not on every tick. */
            if (text[0] && !cs->draw_failed) {
                wlr_log(WLR_ERROR, "Clock: cannot draw '%s' at %dpx", text, px);
                cs->draw_failed = true;
            }
            wlr_scene_buffer_set_buffer(cs->node, NULL);
        } else {
            struct clock_buffer *buf = calloc(1, sizeof(*buf));
            if (!buf) {
                free(img.pixels);
                return;
            }
            wlr_buffer_init(&buf->base, &clock_buffer_impl, img.width, img.height);
            buf->pixels = img.pixels;
            wlr_scene_buffer_set_buffer(cs->node, &buf->base);
            wlr_buffer_drop(&buf->base);  /* the scene holds its own lock */
            place_node(cs, &img);
            wlr_log(WLR_DEBUG, "Clock: drew '%s' at %dpx", text, px);
        }
        snprintf(cs->shown, sizeof(cs->shown), "%s", text);
        cs->shown_px = px;
    }
    wlr_scene_node_set_enabled(&cs->node->node, true);
}

static int handle_timer(int fd, uint32_t mask, void *data) {
    (void)mask;
    struct clock_state *cs = data;
    uint64_t expirations;
    /* ECANCELED means the wall clock was set: redraw and re-arm all the same. */
    if (read(fd, &expirations, sizeof(expirations)) < 0 &&
            errno != ECANCELED && errno != EAGAIN) {
        wlr_log(WLR_ERROR, "Clock: timerfd read: %s", strerror(errno));
    }
    clock_update(cs, false);
    arm_timer(cs);
    return 0;
}

/* ---------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------------*/

void clock_init(struct clock_state *cs, struct server *server) {
    memset(cs, 0, sizeof(*cs));
    cs->server   = server;
    cs->position = CLOCK_BOTTOM_RIGHT;
    snprintf(cs->format, sizeof(cs->format), "%s", "%H:%M");
    cs->timer_fd = timerfd_create(CLOCK_REALTIME, TFD_NONBLOCK | TFD_CLOEXEC);
    if (cs->timer_fd < 0) {
        wlr_log(WLR_ERROR, "Clock: timerfd_create: %s", strerror(errno));
        return;
    }
    cs->timer_source = wl_event_loop_add_fd(server->event_loop, cs->timer_fd,
        WL_EVENT_READABLE, handle_timer, cs);
}

void clock_finish(struct clock_state *cs) {
    if (cs->timer_source) {
        wl_event_source_remove(cs->timer_source);
        cs->timer_source = NULL;
    }
    if (cs->timer_fd >= 0) {
        close(cs->timer_fd);
        cs->timer_fd = -1;
    }
    if (cs->node) {
        wlr_scene_node_destroy(&cs->node->node);
        cs->node = NULL;
    }
    free(cs->font_data);
    cs->font_data  = NULL;
    cs->font_ready = false;
}

bool clock_configure(struct clock_state *cs, bool enabled, const char *format,
                     int position, int size, const char **err) {
    if (enabled && !load_font(cs, err)) {
        return false;
    }
    cs->enabled     = enabled;
    cs->draw_failed = false;
    if (format) {
        snprintf(cs->format, sizeof(cs->format), "%s", format);
    }
    if (position >= 0) {
        cs->position = (enum clock_position)position;
    }
    if (size >= 0) {
        cs->size = size;
    }
    wlr_log(WLR_INFO, "Clock %s: '%s' %s %s", enabled ? "on" : "off",
        cs->format, clock_position_name(cs->position),
        cs->size ? "fixed size" : "auto size");
    clock_update(cs, true);
    arm_timer(cs);
    return true;
}

void clock_output_changed(struct clock_state *cs) {
    clock_update(cs, true);
}
