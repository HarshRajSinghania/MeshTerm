"""Replace the NUL bytes in ANSI art files with spaces — and change nothing else.

A ``0x00`` in the *art body* of a ``.ans`` file is a cell the editor never painted. It is
not a character: terminals draw it as tofu, as a stray glyph, or swallow it and shift the
rest of the row left, so a logo that looked right in Moebius comes apart on a console. The
fix is one byte wide — write a space there — and this script makes exactly that
substitution and no other. Every other byte, including every escape sequence, stays where
it was, so the file's length never changes.

**The SAUCE record is not art.** An ANSI art file's last 128 bytes are usually a SAUCE
metadata record (title, author, group, date, and a NUL-padded font name), introduced by the
DOS end-of-file marker ``0x1A``. Its NULs are structure — padding and binary fields — and
blanking them corrupts the record. Nothing from that ``0x1A`` onward is touched; it is
metadata, and it never reaches the screen anyway. NULs found there are reported, not
replaced.

A space renders in whatever background the surrounding art last set. That is black in the
ordinary case — the default background, and what an unpainted cell should look like — but a
NUL inside a span that set a non-black background becomes a space in *that* colour, and
reads as a coloured block. Those cells are called out by row and column at the end of a
run: fixing one is an editing decision (repaint the cell, or close the colour span), which
is not a substitution this script will make on its own.

``--dry-run`` reports without writing; a real run copies each file it changes to
``<name>.bak`` first.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

#: The DOS end-of-file marker. In a ``.ans`` file it closes the art and introduces the
#: SAUCE record (and any COMNT block before it) — everything from here on is metadata.
EOF_MARKER = 0x1A

#: The SAUCE record's signature, at the head of its 128-byte block.
SAUCE_SIG = b"SAUCE00"

#: What a NUL becomes: a plain space. The one substitution this script makes.
NUL, SPACE = 0x00, 0x20

#: An ANSI escape sequence (CSI + params + a final letter). Only the ``m`` ones — Select
#: Graphic Rendition — carry the colour state the background check follows.
_ESC = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

#: Default art width in cells, for reporting a NUL's row/column. ANSI art wraps at the
#: file's own width rather than at newlines, so a wrong guess only mislabels a coordinate
#: in a warning — it never changes which bytes are replaced.
DEFAULT_WIDTH = 80


def art_end(data: bytes) -> int:
    """The index where the art stops and the trailing metadata begins.

    The SAUCE record is anchored on the ``0x1A`` that introduces it, not on the file's
    length: a file may also carry a COMNT block, and a file may carry an ``0x1A`` with no
    SAUCE at all (a plain DOS end-of-file). Either way, nothing from that marker on is
    drawn, so nothing from that marker on is ours to edit.

    Args:
        data: The whole file.

    Returns:
        The length of the art body — the whole file when it has no trailing marker.
    """
    sauce = data.rfind(SAUCE_SIG)
    if sauce > 0:
        # Walk back over the record (and any COMNT block) to the marker that opens it.
        marker = data.rfind(bytes([EOF_MARKER]), 0, sauce)
        return marker if marker >= 0 else sauce
    marker = data.rfind(bytes([EOF_MARKER]))
    return marker if marker >= 0 else len(data)


def _black_background(params: str) -> bool:
    """Whether an SGR parameter list leaves the background black (and un-reversed).

    Black is the default background, ``40`` (black), and ``49`` (reset to default); ``0``
    resets everything back to it. Reverse video (``7``) swings the foreground into the
    background, so a space under it is not black either.

    Args:
        params: The parameters of one ``…m`` sequence, e.g. ``"0;34"``.

    Returns:
        ``True`` if a space written under this state renders black.
    """
    black = True
    for part in (params or "0").split(";"):
        code = int(part) if part.isdigit() else 0
        if code in (0, 40, 49):
            black = True
        elif code == 7 or 41 <= code <= 47 or 100 <= code <= 107:
            black = False
    return black


def scan(data: bytes, width: int) -> tuple[list[tuple[int, int, int, bool]], int]:
    """Find every NUL in the art body, with where it lands and what colour it lands on.

    Walks the body a cell at a time, stepping over escape sequences (which occupy no cell)
    and tracking the background the SGR state leaves in force.

    Args:
        data: The whole file.
        width: Cells per row, for the reported row/column.

    Returns:
        ``(hits, metadata_nuls)`` — one ``(offset, row, col, on_black)`` per NUL in the
        art, and how many NULs sit in the trailing metadata (never touched).
    """
    end = art_end(data)
    hits: list[tuple[int, int, int, bool]] = []
    i, row, col, black = 0, 1, 1, True
    while i < end:
        esc = _ESC.match(data, i)
        if esc:
            if esc.group()[-1:] == b"m":
                black = _black_background(esc.group()[2:-1].decode("ascii", "replace"))
            i = esc.end()
            continue
        byte = data[i]
        if byte == 0x0A:  # newline: the next row starts here
            row, col, i = row + 1, 1, i + 1
            continue
        if byte == 0x0D:
            i += 1
            continue
        if byte == NUL:
            hits.append((i, row, col, black))
        col += 1
        if col > width:
            row, col = row + 1, 1
        i += 1
    return hits, data.count(NUL) - len(hits)


def fix(path: Path, width: int, dry_run: bool) -> int:
    """Replace the art body's NULs in one file; return how many went.

    Args:
        path: The ``.ans`` file.
        width: Cells per row, for the reported coordinates.
        dry_run: Report only — write nothing.

    Returns:
        The number of NULs replaced (or that would be).
    """
    data = bytearray(path.read_bytes())
    hits, in_metadata = scan(bytes(data), width)
    tail = f" ({in_metadata} in the SAUCE/metadata tail, left alone)" if in_metadata else ""
    if not hits:
        print(f"{path}: no NULs in the art{tail}")
        return 0

    for offset, _row, _col, _black in hits:
        data[offset] = SPACE
    verb = "would replace" if dry_run else "replaced"
    plural = "" if len(hits) == 1 else "s"
    print(f"{path}: {verb} {len(hits)} NUL{plural}{tail}")

    coloured = [(r, c) for _o, r, c, black in hits if not black]
    if coloured:
        where = ", ".join(f"row {r} col {c}" for r, c in coloured[:12])
        more = f", +{len(coloured) - 12} more" if len(coloured) > 12 else ""
        print(
            f"  ! {len(coloured)} of them sit on a non-black background, so the new space "
            f"takes that colour instead of reading black: {where}{more}. Repainting those "
            "cells is an editing call, not a substitution — left to you."
        )
    if not dry_run:
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        path.write_bytes(bytes(data))
    return len(hits)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="the .ans files to fix")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WIDTH,
        help=f"art width in cells, for reported coordinates (default {DEFAULT_WIDTH})",
    )
    args = parser.parse_args()

    total = 0
    for path in args.paths:
        if not path.is_file():
            print(f"{path}: not a file", file=sys.stderr)
            continue
        total += fix(path, args.width, args.dry_run)
    print(f"\n{'would replace' if args.dry_run else 'replaced'} {total} NUL(s) in total")


if __name__ == "__main__":
    main()
