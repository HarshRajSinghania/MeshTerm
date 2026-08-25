#!/usr/bin/env python3
"""Colour a plain-text wordmark into the pre-coloured ANSI the splash draws.

The art itself is hand-drawn and lives in ``meshterm/assets`` as a ``.txt``; this paints
it and writes the matching ``.ans`` beside it, so the colour scheme is stated once here
rather than re-typed into every escape sequence by hand.

    python scripts/colour-logo.py                 # repaint every asset/*.txt
    python scripts/colour-logo.py logo_53         # just that one

The scheme is a vertical ramp over the letter rows — white, yellow, light red, red, light
black, hottest at the top — with every box-drawing character (the drop shadow and the
letters' own bevels) in the light black regardless of its row. Each colour is chosen so the
PicoCalc console's 16-slot quantizer lands it on exactly one palette slot (15, 11, 9, 1, 8)
while it stays a warm fade in truecolour on the desktop; see ``theme._VT_SLOTS``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "meshterm" / "assets"

#: Letter-row colours, hottest first. A mark with more rows than this reuses the last one.
ROW_RGB = ("255;255;255", "251;217;80", "248;113;113", "185;28;28", "80;93;111")

#: Every non-block character — the drop shadow and the bevels — takes this.
SHADOW_RGB = "80;93;111"

#: The letter body. Everything else in the art is shadow.
BLOCK = "█"


def colour(art: str) -> str:
    """Paint one plain-text wordmark, returning the pre-coloured ANSI."""
    lines = art.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out_lines = []
    for row, line in enumerate(lines):
        hue = ROW_RGB[min(row, len(ROW_RGB) - 1)]
        out, current = [], None
        for ch in line:
            if ch == " ":
                if current is not None:
                    out.append("\x1b[0m")
                    current = None
                out.append(" ")
                continue
            want = hue if ch == BLOCK else SHADOW_RGB
            if want != current:
                out.append(f"\x1b[38;2;{want}m")
                current = want
            out.append(ch)
        if current is not None:
            out.append("\x1b[0m")
        out_lines.append("".join(out))
    return "\n".join(out_lines) + "\n"


def main() -> None:
    """Repaint the named marks, or every ``.txt`` in the assets folder."""
    names = sys.argv[1:] or sorted(p.stem for p in ASSETS.glob("*.txt"))
    for name in names:
        source = ASSETS / f"{name}.txt"
        target = ASSETS / f"{name}.ans"
        target.write_text(colour(source.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"{source.name} -> {target.name}")


if __name__ == "__main__":
    main()
