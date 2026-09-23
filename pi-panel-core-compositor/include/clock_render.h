#ifndef CLOCK_RENDER_H
#define CLOCK_RENDER_H

#include <stdbool.h>
#include <stdint.h>

#include "stb_truetype.h"

/* Rasterizing the clock text, kept free of wlroots so that
 * tools/clock_preview.c can render it to a file for a look without a
 * compositor. */

struct clock_image {
    int       width, height;   /* the whole image, shadow padding included */
    int       text_x, text_y;  /* where the text's line box starts in it */
    int       text_w, text_h;  /* the line box: advance width x (ascent - descent) */
    uint32_t *pixels;          /* premultiplied ARGB8888, stride = width * 4 */
};

/* Refuse to draw anything bigger than this: a hostile format could otherwise
 * ask for a gigabyte of pixels. */
#define CLOCK_IMAGE_MAX_W  8192
#define CLOCK_IMAGE_MAX_H  2048

/* Render UTF-8 |text| in white, |px| pixels high, over a soft dark shadow.
 * Returns false (and leaves |out| empty) for empty text, an image over the
 * limits, or no memory.  Free out->pixels with free(). */
bool clock_render_text(const stbtt_fontinfo *font, const char *text, int px,
                       struct clock_image *out);

#endif /* CLOCK_RENDER_H */
