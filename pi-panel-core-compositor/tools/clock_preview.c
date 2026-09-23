/* Render the clock overlay's text to a PAM image, to look at the rasterizer
 * without a compositor (the compositor has no screencopy, so grim cannot):
 *
 *   build/clock-preview FONT.ttf TEXT PX > clock.pam
 *
 * The image is composited over mid-grey so the shadow shows. */
#include <stdio.h>
#include <stdlib.h>

#include "clock_render.h"

int main(int argc, char *argv[]) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s FONT.ttf TEXT PX > out.pam\n", argv[0]);
        return 2;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) {
        perror(argv[1]);
        return 1;
    }
    static unsigned char data[32 * 1024 * 1024];
    size_t len = fread(data, 1, sizeof(data), f);
    fclose(f);
    stbtt_fontinfo font;
    if (len == 0 || !stbtt_InitFont(&font, data, stbtt_GetFontOffsetForIndex(data, 0))) {
        fprintf(stderr, "%s: not a usable font\n", argv[1]);
        return 1;
    }
    struct clock_image img;
    if (!clock_render_text(&font, argv[2], atoi(argv[3]), &img)) {
        fprintf(stderr, "nothing rendered\n");
        return 1;
    }
    printf("P7\nWIDTH %d\nHEIGHT %d\nDEPTH 3\nMAXVAL 255\nTUPLTYPE RGB\nENDHDR\n",
        img.width, img.height);
    for (int i = 0; i < img.width * img.height; i++) {
        unsigned p = img.pixels[i], a = p >> 24, bg = 128 * (255 - a) / 255;
        unsigned char rgb[3] = {
            (unsigned char)(((p >> 16) & 0xff) + bg),
            (unsigned char)(((p >> 8) & 0xff) + bg),
            (unsigned char)((p & 0xff) + bg),
        };
        fwrite(rgb, 1, 3, stdout);
    }
    free(img.pixels);
    return 0;
}
