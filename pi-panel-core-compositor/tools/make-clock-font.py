#!/usr/bin/env python3
"""Make the clock's font from Inter Display: fixed-width digits and a colon
centred on them, as the plain characters.

    uv run --with fonttools tools/make-clock-font.py \\
        InterDisplay-SemiBold.ttf fonts/InterDisplayClock-SemiBold.ttf

The source is from the Inter 4.1 release (extras/ttf/ in Inter-4.1.zip).
stb_truetype maps characters straight to glyphs and applies no OpenType
features, so Inter's `tnum` and `case` alternates can't be asked for. Instead
"0"-"9" and ":" are pointed at those glyphs in the font's character map.
Proportional digits would make the clock change width every minute.

Inter's licence (OFL 1.1) reserves no font name; the copy is still renamed
so it can't be taken for stock Inter.
"""

import sys

from fontTools.ttLib import TTFont

DIGITS = ["zero", "one", "two", "three", "four",
          "five", "six", "seven", "eight", "nine"]
FAMILY = "Inter Display Clock"


def main(source: str, output: str) -> None:
    font = TTFont(source)
    glyphs = set(font.getGlyphOrder())
    mapping = {0x30 + i: f"{name}.tf" for i, name in enumerate(DIGITS)}
    mapping[ord(":")] = "colon.case.tf"
    missing = sorted(set(mapping.values()) - glyphs)
    if missing:
        sys.exit(f"{source}: no glyphs {', '.join(missing)}: not Inter 4?")

    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap.update(mapping)

    # Family, unique id, full name, PostScript name, typographic and WWS family:
    # "Inter Display SemiBold" -> "Inter Display Clock SemiBold".
    for record in font["name"].names:
        if record.nameID in (1, 3, 4, 6, 16, 21):
            text = record.toUnicode()
            record.string = (text.replace("Inter Display", FAMILY)
                                 .replace("InterDisplay", FAMILY.replace(" ", "")))
    font.save(output)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} SOURCE.ttf OUTPUT.ttf")
    main(sys.argv[1], sys.argv[2])
