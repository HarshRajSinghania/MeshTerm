"""The MeshTerm wordmark for the startup splash, loaded from the assets folder.

The art itself lives in ``meshterm/assets`` as pre-coloured ``.ans`` files — the classic
ANSI-art extension, kept short because the editors that draw this stuff are old and
particular about 8.3 names. A ``.txt`` beside one is its hand-drawn source for marks we
colour ourselves (``scripts/colour-logo.py`` paints one into the other); a mark drawn in
an ANSI-art editor arrives already finished and has no source but itself. Files are read
fresh on every call, so the wordmark can be re-styled by editing the art alone: no code
change and no restart of the design loop.

Two things separate a real ``.ans`` from a text file with colour in it, and :func:`_rows`
handles both so the art stays authorable in the tools that drew it:

* **Codepage 437.** The blocks and box-drawing (``█▓▒░`` and ``╔═╗``) are single bytes in
  DOS's codepage, not UTF-8. We try UTF-8 first and fall back, so a mark we generated
  ourselves and a mark exported from an art editor both read.
* **Auto-wrap.** An art editor omits the line break on a row that fills the canvas and
  lets the terminal wrap it, so the file's newlines are *not* the picture's rows. The
  canvas width comes from the SAUCE record on the end of the file — which has to be
  trimmed off in the same breath, being metadata rather than art.

There is more than one mark, at different widths, and the *screen* picks — not the
platform. A 53-column PicoCalc console and a desktop terminal dragged narrow have the same
problem, so :func:`load_logo` answers the only question that matters: which is the widest
mark that fits the columns I have?
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from rich.cells import cell_len
from rich.text import Text

#: Where the art lives — a sibling of the code, not mixed into it, and inside the package
#: so it ships with an installed wheel.
_ASSETS = Path(__file__).resolve().parent.parent / "assets"

#: Every wordmark, widest first. :func:`load_logo` walks this and takes the first that
#: fits, so adding a size is dropping a file in and naming it here.
_VARIANTS: tuple[str, ...] = ("logo.ans", "logo_53.ans")

#: A colour change and nothing else. Every escape these marks use is an SGR, so re-breaking
#: a row only ever has to carry a *colour* across the seam — never a cursor move.
_SGR = re.compile(r"\x1b\[[0-9;]*m")

#: SAUCE rides on the end of an art file behind DOS's end-of-file mark: 128 bytes naming
#: the author, the canvas and the font. None of it is meant to reach the screen.
_EOF_MARK = b"\x1a"
_SAUCE_LEN = 128


def _split_sauce(raw: bytes) -> tuple[bytes, Optional[int]]:
    """The art alone, and the canvas width SAUCE claims for it (``None`` when unsigned).

    A file with no record is returned whole and unmeasured: its newlines are its rows, and
    re-breaking one would be inventing a canvas the author never declared.
    """
    cut = raw.rfind(_EOF_MARK)
    if cut < 0:
        return raw, None
    trailer = raw[cut + 1 :]
    if not trailer.startswith(b"SAUCE") or len(trailer) < _SAUCE_LEN:
        return raw, None
    width = int.from_bytes(trailer[96:98], "little")  # TInfo1 — characters per row
    return raw[:cut], width or None


def _decode(raw: bytes) -> str:
    """An art file's text, in whichever of the two codepages it was written."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp437")


def _rewrap(line: str, width: int) -> list[str]:
    """Re-break one auto-wrapping line into rows of ``width`` printable cells.

    The rows are drawn independently — each becomes its own ``Text.from_ansi`` — so a
    colour set before the seam has to be restated after it. A real terminal needs no such
    help: it never stopped reading the one stream, so the state simply persisted.
    """
    rows: list[str] = []
    buf: list[str] = []
    state = ""
    cells = 0
    at = 0
    while at < len(line):
        found = _SGR.match(line, at)
        if found:
            seq = found.group()
            buf.append(seq)
            params = seq[2:-1]
            if params in ("", "0"):
                state = ""  # a plain reset leaves nothing to restate
            elif params.split(";")[0] in ("", "0"):
                state = seq  # reset-and-set: this sequence *is* the whole state
            else:
                state += seq
            at = found.end()
            continue
        buf.append(line[at])
        at += 1
        cells += 1
        if cells == width:
            rows.append("".join(buf))
            buf = [state] if state else []
            cells = 0
    if cells or not rows:  # the short last row, or a blank line that is still a row
        rows.append("".join(buf))
    return rows


def _rows(name: str) -> list[str]:
    """The named mark's rows, or ``[]`` when it can't be read."""
    try:
        raw = (_ASSETS / name).read_bytes()
    except OSError:
        return []
    art, width = _split_sauce(raw)
    lines = [line.rstrip("\r") for line in _decode(art).split("\n")]
    if lines and lines[-1] == "":  # drop the trailing newline's empty row
        lines.pop()
    if width:
        return [row for line in lines for row in _rewrap(line, width)]
    return lines


def logo_width(rows: list[str]) -> int:
    """The display width of a mark's widest row, escape sequences discounted."""
    return max((cell_len(Text.from_ansi(row).plain) for row in rows), default=0)


def load_logo(max_cols: Optional[int] = None) -> list[str]:
    """Return the widest wordmark that fits ``max_cols``, as pre-coloured ANSI rows.

    Args:
        max_cols: The columns available to draw in. ``None`` asks for the full-size mark
            without fitting — for a caller that will fit it later (see
            :func:`~meshterm.ui.tui.frame.compose_startup`, which knows the real width
            only at compose time).

    Returns:
        The mark's rows, or ``[]`` when none fits (or none could be read) — the splash
        draws no banner at all rather than a torn one, and a missing file degrades the
        same way rather than raising.
    """
    for name in _VARIANTS:
        rows = _rows(name)
        if rows and (max_cols is None or logo_width(rows) <= max_cols):
            return rows
    return []
