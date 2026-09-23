#!/usr/bin/env python3
"""Turn a text file into a C header holding it as one string constant.

    embed.py INPUT OUTPUT SYMBOL

Used to compile the Varlink interface description into the binary, so what
`varlinkctl introspect` shows can never drift from the .varlink file.
"""

import sys


def c_literal(line: str) -> str:
    out = []
    for ch in line:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\{ord(ch):03o}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def main() -> int:
    src, dst, symbol = sys.argv[1:4]
    with open(src, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
    body = "\n".join("    " + c_literal(line) for line in lines) or '    ""'
    guard = symbol.upper() + "_H"
    with open(dst, "w", encoding="utf-8") as f:
        f.write(f"/* Generated from {src.rsplit('/', 1)[-1]} by tools/embed.py. Do not edit. */\n")
        f.write(f"#ifndef {guard}\n#define {guard}\n\n")
        f.write(f"static const char {symbol}[] =\n{body};\n\n#endif\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
