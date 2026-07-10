"""A corner-light pass for framed boxes: brighten each frame's top-left, fading to normal.

Every panel the TUI draws — the full-screen base frame, floating dialogs, the startup
splash box, and the content-sized tool panels nested *inside* screen bodies (Status, Link
budget, Mesh, ...) — is bordered with Rich's box glyphs. This module post-processes
composed ANSI lines and gives each such frame a subtle lighting treatment: the top-left
corner is lifted toward white, and the highlight decays along the top edge (rightward) and
the left edge (downward) until it meets the border's own, untouched colour. The right and
bottom edges stay as drawn, so the box reads as lit from the top-left.

The pass is generic on purpose. It does not know which panels exist or what colour each
border uses: it *finds* frames by their corner glyphs and reads every border cell's current
colour from the ANSI stream itself, blending that toward white. A muted box therefore gets
a muted glow, a warn box a warm one — and nested panels are lit individually, each from its
own corner — with no per-callsite wiring. Composers apply it exactly once, after layout
(see :func:`~meshterm.ui.tui.frame.compose_base` and friends), so the effect never
compounds.

Because it runs on every repaint, the pass works on the ANSI strings directly rather than
round-tripping through Rich (parse → restyle → re-render costs ~10ms a frame; string
surgery is well under 1ms). The render console emits truecolor SGR sequences for every
themed colour, so a tiny state machine tracking ``38;2;r;g;b`` foregrounds is enough to
know each border cell's colour; the recolour splices in an override before the glyph and
restores the tracked colour after it, leaving bold/dim attributes and everything else in
the stream untouched. Title and subtitle text embedded in a border row is skipped (only
box-drawing glyphs are recoloured).
"""

from __future__ import annotations

import re
from itertools import accumulate

from rich.cells import cell_len

#: Glyphs that open a frame's top edge. Rich's panels use the ROUNDED box (``╭``) — or its
#: SQUARE substitute (``┌``) on legacy Windows consoles; the heavy/double corners are
#: accepted too so a restyled box would still glow.
_TOP_LEFT = frozenset("╭┌┏╔")

#: Glyphs that close a top edge. The nearest one right of a top-left corner on the same row
#: marks the frame's width, which normalises the horizontal fade.
_TOP_RIGHT = frozenset("╮┐┓╗")

#: Glyphs that close a left edge. Finding one directly below a top-left corner marks the
#: frame's height, which normalises the vertical fade.
_BOTTOM_LEFT = frozenset("╰└┗╚")

#: Border glyphs a top edge is drawn with. Anything else on the row (a title, its
#: surrounding spaces) is left untouched, so only the frame itself is recoloured.
_HORIZONTAL = frozenset("─━═")

#: Border glyphs a left edge is drawn with, walked downward from each top-left corner.
_VERTICAL = frozenset("│┃║")

#: How far the corner cell itself is lifted toward pure white (0 = untouched, 1 = white).
#: The fade multiplies this by a per-cell weight, so every other border cell gets less.
_CORNER_LIFT = 0.8

#: Weights below this leave the cell alone — the blend would be invisible, and skipping it
#: keeps far border cells byte-identical to their un-glowed rendering.
_MIN_WEIGHT = 0.02

#: One SGR escape sequence (the only kind the render console emits); group 1 is its
#: semicolon-separated parameter list.
_SGR = re.compile(r"\x1b\[([0-9;]*)m")

#: Finds top-left corner glyphs in a plain (escape-free) row at C speed.
_CORNER = re.compile(f"[{''.join(_TOP_LEFT)}]")


def _strip(line: str) -> str:
    """Return the line's plain text with all SGR escape sequences removed."""
    return _SGR.sub("", line)


def _weight(distance: int, extent: int) -> float:
    """Return the highlight weight for a border cell ``distance`` cells from the corner.

    The fade is quadratic — ``(1 - t)²`` over the edge's ``extent`` — so the glow
    concentrates near the corner and melts into the border's normal colour well before the
    far end, rather than tinting the whole edge half-bright.

    Args:
        distance: Cells travelled along the edge from the top-left corner (0 = the corner).
        extent: The edge's full length in cells; the weight reaches 0 here.

    Returns:
        A weight in [0, 1] to scale :data:`_CORNER_LIFT` by.
    """
    if extent <= 0:
        return 0.0
    t = min(1.0, distance / extent)
    return (1.0 - t) ** 2


def _lift(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """Blend an RGB colour toward white by ``amount`` (0 = unchanged, 1 = pure white)."""
    r, g, b = rgb
    return (
        round(r + (255 - r) * amount),
        round(g + (255 - g) * amount),
        round(b + (255 - b) * amount),
    )


def _columns(plain: str) -> list[int]:
    """Return each character's display column (a cumulative cell-width prefix sum).

    Border glyphs are single-cell, but body text earlier in a row may hold double-width
    characters; walking columns rather than indices keeps the vertical edge walk aligned
    under its corner regardless.
    """
    return [0, *accumulate(cell_len(ch) for ch in plain)]


def _index_at_column(columns: list[int], column: int) -> int | None:
    """Return the index of the character at display ``column``, or ``None`` if none lands there.

    A wide character straddling the column (so nothing *starts* there) returns ``None``,
    which simply ends the edge walk — a frame's border cells always start on their column.
    """
    for idx in range(len(columns) - 1):
        if columns[idx] == column:
            return idx
        if columns[idx] > column:
            return None
    return None


def _advance_fg(fg: tuple[int, int, int] | None, params: str) -> tuple[int, int, int] | None:
    """Track the foreground colour across one SGR sequence's parameters.

    Only truecolor foregrounds (``38;2;r;g;b``) are resolved — the render console emits
    every themed colour that way. A reset (``0`` or empty), a default (``39``), or any
    other foreground form yields ``None``, and cells whose colour is unknown are simply not
    glowed rather than guessed at.

    Args:
        fg: The foreground in effect before this sequence, or ``None`` if unknown/default.
        params: The sequence's raw parameter list (the text between ``ESC[`` and ``m``).

    Returns:
        The foreground in effect after the sequence.
    """
    parts = params.split(";") if params else [""]
    i = 0
    while i < len(parts):
        code = parts[i] or "0"
        if code == "0" or code == "39":
            fg = None
        elif code == "38":
            if parts[i + 1 : i + 2] == ["2"] and i + 4 < len(parts):
                try:
                    fg = (int(parts[i + 2]), int(parts[i + 3]), int(parts[i + 4]))
                except ValueError:  # pragma: no cover - malformed sequence
                    fg = None
                i += 5
                continue
            fg = None  # an 8-bit (38;5;n) or malformed foreground: unknown, skip its cells
            i += 2 if parts[i + 1 : i + 2] == ["5"] else 1
        elif code == "48":
            # A background colour: skip its components so they aren't misread as codes.
            if parts[i + 1 : i + 2] == ["2"]:
                i += 5
            elif parts[i + 1 : i + 2] == ["5"]:
                i += 2
        elif code.isdigit() and (30 <= int(code) <= 37 or 90 <= int(code) <= 97):
            fg = None  # a named 16-colour foreground; the themed borders never use these
        i += 1
    return fg


def _recolor_line(line: str, targets: list[tuple[int, float]]) -> str:
    """Splice colour overrides into one ANSI line at the given plain-text indices.

    Walks the line's SGR sequences to track the active foreground, and wraps each target
    character in a lifted-colour override followed by a restore of the tracked colour.
    Targets falling where the foreground is unknown are left untouched.

    Args:
        line: The original ANSI line.
        targets: ``(plain_index, weight)`` pairs, the border cells to brighten.

    Returns:
        The line with the highlight spliced in.
    """
    wanted = dict(sorted(targets))
    out: list[str] = []
    fg: tuple[int, int, int] | None = None
    plain_pos = 0
    cursor = 0

    def emit(text: str) -> None:
        """Append a raw text run, recolouring any target characters inside it."""
        nonlocal plain_pos
        start = plain_pos
        plain_pos += len(text)
        if fg is None:
            out.append(text)
            return
        base = 0
        for idx, weight in wanted.items():
            rel = idx - start
            if rel < base or rel >= len(text):
                continue
            r, g, b = _lift(fg, _CORNER_LIFT * weight)
            out.append(text[base:rel])
            out.append(f"\x1b[38;2;{r};{g};{b}m{text[rel]}\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
            base = rel + 1
        out.append(text[base:])

    for match in _SGR.finditer(line):
        emit(line[cursor : match.start()])
        out.append(match.group(0))
        fg = _advance_fg(fg, match.group(1))
        cursor = match.end()
    emit(line[cursor:])
    return "".join(out)


class _Glower:
    """One glow pass over a block of composed lines (find frames → splice highlights)."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.plains = [_strip(line) for line in lines]
        #: Per-row display-column prefix sums, computed lazily — only walked rows need one.
        self._column_cache: dict[int, list[int]] = {}
        #: The border cells to brighten: row → ``(plain_index, weight)`` pairs.
        self.targets: dict[int, list[tuple[int, float]]] = {}

    def _cols(self, row: int) -> list[int]:
        """Return (and cache) the column prefix sums for ``row``."""
        cols = self._column_cache.get(row)
        if cols is None:
            cols = self._column_cache[row] = _columns(self.plains[row])
        return cols

    def apply(self) -> list[str]:
        """Find every top-left corner and light its frame; return the updated lines."""
        for row, plain in enumerate(self.plains):
            for match in _CORNER.finditer(plain):
                self._glow_frame(row, match.start())
        if not self.targets:
            return self.lines
        out = list(self.lines)
        for row, targets in self.targets.items():
            out[row] = _recolor_line(self.lines[row], targets)
        return out

    def _glow_frame(self, row: int, corner: int) -> None:
        """Light one frame from its top-left corner at ``(row, corner)``."""
        plain = self.plains[row]
        columns = self._cols(row)

        # The matching ╮ bounds the top edge; a clipped frame falls back to the row's end.
        closing = next((j for j in range(corner + 1, len(plain)) if plain[j] in _TOP_RIGHT), None)
        end = closing if closing is not None else len(plain)
        extent = max(1, columns[end] - columns[corner])
        for idx in range(corner, end):
            ch = plain[idx]
            if idx != corner and ch not in _HORIZONTAL:
                continue  # title text (and its padding) keeps its own style
            self._mark(row, idx, _weight(columns[idx] - columns[corner], extent))

        # Walk the left edge downward until the ╰ that closes it (or the block's clipped
        # end), then fade over that height. The bottom corner lands at weight 0 by design.
        column = columns[corner]
        edge: list[tuple[int, int]] = []
        closed = False
        for below in range(row + 1, len(self.plains)):
            idx = _index_at_column(self._cols(below), column)
            if idx is None:
                break
            ch = self.plains[below][idx]
            if ch in _BOTTOM_LEFT:
                closed = True
                break
            if ch not in _VERTICAL:
                break
            edge.append((below, idx))
        height = len(edge) + 1 if closed else len(edge) + 2
        for depth, (below, idx) in enumerate(edge, start=1):
            self._mark(below, idx, _weight(depth, height))

    def _mark(self, row: int, idx: int, weight: float) -> None:
        """Record one border cell to brighten (skipping invisibly small weights)."""
        if weight < _MIN_WEIGHT:
            return
        self.targets.setdefault(row, []).append((idx, weight))


def apply_corner_glow(lines: list[str]) -> list[str]:
    """Light every frame in ``lines`` from its top-left corner.

    Scans the composed ANSI lines for box top-left corners, then blends each frame's border
    colour toward white — strongest at the corner, fading quadratically along the top and
    left edges to the border's normal colour. Body text, titles, and the right/bottom edges
    are untouched. Rows without recoloured cells are returned byte-identical.

    Args:
        lines: Composed ANSI lines (one per terminal row, no newlines).

    Returns:
        The lines with the corner highlight applied.
    """
    return _Glower(lines).apply()
