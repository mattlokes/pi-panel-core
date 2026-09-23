#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "clock_render.h"

#define SHADOW_ALPHA 160   /* of 255: how dark the shadow is at full coverage */

/* Next codepoint from UTF-8 |*s|, advancing it.  Malformed input gives
 * U+FFFD rather than stopping: the text comes from strftime and a locale. */
static int utf8_next(const unsigned char **s) {
    const unsigned char *p = *s;
    int cp, extra;
    if (p[0] < 0x80)                { *s = p + 1; return p[0]; }
    else if ((p[0] & 0xe0) == 0xc0) { cp = p[0] & 0x1f; extra = 1; }
    else if ((p[0] & 0xf0) == 0xe0) { cp = p[0] & 0x0f; extra = 2; }
    else if ((p[0] & 0xf8) == 0xf0) { cp = p[0] & 0x07; extra = 3; }
    else                            { *s = p + 1; return 0xfffd; }
    for (int i = 1; i <= extra; i++) {
        if ((p[i] & 0xc0) != 0x80) {
            *s = p + i;
            return 0xfffd;
        }
        cp = (cp << 6) | (p[i] & 0x3f);
    }
    *s = p + extra + 1;
    return cp;
}

/* One pass of a box blur of radius |r| along rows (dx=1) or columns. */
static void box_blur(const uint8_t *src, uint8_t *dst, int w, int h, int r,
                     bool horizontal) {
    int len   = horizontal ? w : h;
    int lines = horizontal ? h : w;
    int step  = horizontal ? 1 : w;
    int span  = 2 * r + 1;
    for (int l = 0; l < lines; l++) {
        const uint8_t *in  = src + (horizontal ? l * w : l);
        uint8_t       *out = dst + (horizontal ? l * w : l);
        int sum = 0;
        for (int i = -r; i <= r; i++) {
            if (i >= 0 && i < len) {
                sum += in[i * step];
            }
        }
        for (int i = 0; i < len; i++) {
            out[i * step] = (uint8_t)(sum / span);
            int add = i + r + 1, sub = i - r;
            if (add < len) { sum += in[add * step]; }
            if (sub >= 0)  { sum -= in[sub * step]; }
        }
    }
}

bool clock_render_text(const stbtt_fontinfo *font, const char *text, int px,
                       struct clock_image *out) {
    memset(out, 0, sizeof(*out));
    if (!text || !*text || px <= 0) {
        return false;
    }

    float scale = stbtt_ScaleForPixelHeight(font, (float)px);
    int ascent, descent, line_gap;
    stbtt_GetFontVMetrics(font, &ascent, &descent, &line_gap);
    int baseline = (int)ceilf(ascent * scale);

    /* Measure: the line box is the advance width by ascent - descent. */
    float pen = 0.0f;
    int prev = 0;
    for (const unsigned char *s = (const unsigned char *)text; *s;) {
        int cp = utf8_next(&s);
        int advance, lsb;
        if (prev) {
            pen += scale * stbtt_GetCodepointKernAdvance(font, prev, cp);
        }
        stbtt_GetCodepointHMetrics(font, cp, &advance, &lsb);
        pen += scale * advance;
        prev = cp;
    }
    int text_w = (int)ceilf(pen);
    int text_h = baseline + (int)ceilf(-descent * scale);

    int offset = px / 24 > 1 ? px / 24 : 1;      /* shadow drop */
    int radius = px / 20 > 1 ? px / 20 : 1;      /* shadow softness */
    int pad    = offset + radius + 1;
    int w = text_w + 2 * pad;
    int h = text_h + 2 * pad;
    if (text_w <= 0 || w > CLOCK_IMAGE_MAX_W || h > CLOCK_IMAGE_MAX_H) {
        return false;
    }

    uint8_t  *cover  = calloc((size_t)w * h, 1);
    uint8_t  *tmp    = calloc((size_t)w * h, 1);
    uint8_t  *shadow = calloc((size_t)w * h, 1);
    uint32_t *pixels = calloc((size_t)w * h, sizeof(uint32_t));
    if (!cover || !tmp || !shadow || !pixels) {
        free(cover); free(tmp); free(shadow); free(pixels);
        return false;
    }

    /* Coverage: each glyph rendered on its own, at its subpixel position,
     * and combined by max so neighbours that overlap do not clip each other. */
    pen  = 0.0f;
    prev = 0;
    for (const unsigned char *s = (const unsigned char *)text; *s;) {
        int cp = utf8_next(&s);
        if (prev) {
            pen += scale * stbtt_GetCodepointKernAdvance(font, prev, cp);
        }
        int advance, lsb;
        stbtt_GetCodepointHMetrics(font, cp, &advance, &lsb);

        float shift = pen - floorf(pen);
        int gw, gh, gx, gy;
        unsigned char *glyph = stbtt_GetCodepointBitmapSubpixel(font,
            scale, scale, shift, 0.0f, cp, &gw, &gh, &gx, &gy);
        if (glyph) {
            int ox = pad + (int)floorf(pen) + gx;
            int oy = pad + baseline + gy;
            for (int y = 0; y < gh; y++) {
                int ty = oy + y;
                if (ty < 0 || ty >= h) { continue; }
                for (int x = 0; x < gw; x++) {
                    int tx = ox + x;
                    if (tx < 0 || tx >= w) { continue; }
                    uint8_t v = glyph[y * gw + x];
                    uint8_t *c = &cover[ty * w + tx];
                    if (v > *c) { *c = v; }
                }
            }
            stbtt_FreeBitmap(glyph, NULL);
        }
        pen += scale * advance;
        prev = cp;
    }

    /* Shadow: the coverage blurred (two box passes each way are close to a
     * gaussian), then dropped down and right by |offset|. */
    box_blur(cover, tmp, w, h, radius, true);
    box_blur(tmp, shadow, w, h, radius, false);
    box_blur(shadow, tmp, w, h, radius, true);
    box_blur(tmp, shadow, w, h, radius, false);

    /* White text over a black shadow, premultiplied: the colour channels are
     * just the text's own coverage. */
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            uint32_t a = cover[y * w + x];
            uint32_t s = 0;
            int sx = x - offset, sy = y - offset;
            if (sx >= 0 && sy >= 0) {
                s = shadow[sy * w + sx] * SHADOW_ALPHA / 255;
            }
            uint32_t alpha = a + s * (255 - a) / 255;
            pixels[y * w + x] = (alpha << 24) | (a << 16) | (a << 8) | a;
        }
    }
    free(cover);
    free(tmp);
    free(shadow);

    out->width  = w;
    out->height = h;
    out->text_x = pad;
    out->text_y = pad;
    out->text_w = text_w;
    out->text_h = text_h;
    out->pixels = pixels;
    return true;
}
