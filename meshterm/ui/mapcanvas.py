"""A colored Unicode-braille canvas for the terminal map.

Each character cell holds a 2×4 grid of braille dots, so the drawable resolution is twice
the columns by four times the rows. Streets, rivers and water are rasterized as braille
*dots*; place names, street names and node labels are written as *text* over whole cells;
node markers are single glyphs on top.

A terminal cell can only show one colour, so each cell keeps the colour of the
highest-**priority** feature whose dots fall in it (a river drawn over water keeps the river's
blue). Text and markers form a separate overlay that always wins the cell, with greedy
collision avoidance so labels never overprint each other. The canvas renders straight to
truecolour ANSI lines, which is exactly what the TUI frame consumes.
"""

from __future__ import annotations

import unicodedata
from typing import Optional

#: Unicode braille pattern base; add a dot bitmask to get the glyph.
_BRAILLE_BASE = 0x2800


def single_cell(text: str) -> str:
    """Reduce ``text`` to characters that render in exactly one fixed-width cell.

    The map is a fixed-width grid drawn with whatever font the terminal happens to have, and
    the layout assumes every label character advances exactly one column. Three kinds of
    character break that: control/format codes, combining marks (which stack onto the previous
    cell), and East-Asian *wide*/*fullwidth* glyphs and emoji (which take two columns and so
    shove the rest of the row out of alignment). Those are also the characters least likely to
    exist in a typical monospace font, so they surface as tofu. Dropping them keeps labels
    legible in the Latin/Cyrillic/Greek range fonts reliably cover; a label that is *only*
    such characters (e.g. an all-emoji node name) collapses to empty and is simply not drawn.
    """
    out: list[str] = []
    for ch in text:
        if ch == " ":
            out.append(ch)
            continue
        if unicodedata.category(ch)[0] == "C":  # control, format, surrogate, unassigned
            continue
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            continue
        out.append(ch)
    return "".join(out).strip()

#: Dot bit for each (col, row) within a cell — the Unicode braille standard layout.
_DOT_BITS = (
    (0x01, 0x02, 0x04, 0x40),  # left column, rows 0..3
    (0x08, 0x10, 0x20, 0x80),  # right column, rows 0..3
)

RGB = tuple[int, int, int]


def parse_hex(color: str) -> RGB:
    """Convert ``"#rrggbb"`` (or ``"rrggbb"``) to an ``(r, g, b)`` tuple."""
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


class MapCanvas:
    """A colored braille raster with a text/marker overlay and label collision tracking."""

    def __init__(self, cell_w: int, cell_h: int) -> None:
        """Create a blank canvas.

        Args:
            cell_w: Width in character cells.
            cell_h: Height in character cells.
        """
        self.cell_w = max(1, cell_w)
        self.cell_h = max(1, cell_h)
        self.dot_w = self.cell_w * 2
        self.dot_h = self.cell_h * 4
        self._bits = [[0] * self.cell_w for _ in range(self.cell_h)]
        self._color: list[list[Optional[RGB]]] = [
            [None] * self.cell_w for _ in range(self.cell_h)
        ]
        self._prio = [[-1] * self.cell_w for _ in range(self.cell_h)]
        # Overlay: (cx, cy) -> (char, rgb, bold). Occupied tracks cells claimed by labels /
        # markers so later labels can avoid them; label_cells is the labels alone, so the
        # anti-stacking margin can guard against text piling up without also forbidding a
        # label from sitting immediately above or below a (single-glyph) marker.
        self._overlay: dict[tuple[int, int], tuple[str, RGB, bool]] = {}
        self._occupied: set[tuple[int, int]] = set()
        self._label_cells: set[tuple[int, int]] = set()

    # -- primitives -------------------------------------------------------------

    def plot(self, x: int, y: int, color: RGB, priority: int) -> None:
        """Light the braille dot at dot ``(x, y)`` and colour its cell by ``priority``."""
        if not (0 <= x < self.dot_w and 0 <= y < self.dot_h):
            return
        cx, cy = x >> 1, y >> 2
        self._bits[cy][cx] |= _DOT_BITS[x & 1][y & 3]
        if priority >= self._prio[cy][cx]:
            self._prio[cy][cx] = priority
            self._color[cy][cx] = color

    def draw_line(
        self, points: list[tuple[float, float]], color: RGB, priority: int
    ) -> None:
        """Rasterize a polyline through ``points`` (dot coordinates) as braille dots."""
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            self._segment(x0, y0, x1, y1, color, priority)

    def _segment(
        self, x0: float, y0: float, x1: float, y1: float, color: RGB, priority: int
    ) -> None:
        """Bresenham a single segment, skipping ones wholly off the canvas."""
        # Quick reject: both endpoints beyond the same edge → nothing visible.
        if (x0 < 0 and x1 < 0) or (x0 >= self.dot_w and x1 >= self.dot_w):
            return
        if (y0 < 0 and y1 < 0) or (y0 >= self.dot_h and y1 >= self.dot_h):
            return
        xa, ya, xb, yb = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(xb - xa), -abs(yb - ya)
        sx = 1 if xa < xb else -1
        sy = 1 if ya < yb else -1
        err = dx + dy
        while True:
            self.plot(xa, ya, color, priority)
            if xa == xb and ya == yb:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                xa += sx
            if e2 <= dx:
                err += dx
                ya += sy

    def fill_polygon(
        self, rings: list[list[tuple[float, float]]], color: RGB, priority: int
    ) -> None:
        """Even-odd scanline fill of a polygon (with holes) in dot space."""
        edges: list[tuple[float, float, float, float]] = []
        ys: list[float] = []
        for ring in rings:
            for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
                if y0 != y1:
                    edges.append((x0, y0, x1, y1))
                    ys.extend((y0, y1))
        if not edges:
            return
        y_start = max(0, int(min(ys)))
        y_end = min(self.dot_h - 1, int(max(ys)))
        for y in range(y_start, y_end + 1):
            yc = y + 0.5
            xs: list[float] = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yc < y1) or (y1 <= yc < y0):
                    xs.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
            xs.sort()
            for i in range(0, len(xs) - 1, 2):
                x_from = max(0, int(round(xs[i])))
                x_to = min(self.dot_w - 1, int(round(xs[i + 1])))
                for x in range(x_from, x_to + 1):
                    self.plot(x, y, color, priority)

    # -- overlay (markers + labels) --------------------------------------------

    def marker(self, x: int, y: int, glyph: str, color: RGB) -> None:
        """Place a marker glyph at dot ``(x, y)``.

        Markers always draw (they are the point of the map) and reserve their cell so
        labels route around them. The label, if any, is placed separately via
        :meth:`marker_label` so it can be dropped (bare glyph) when the map is crowded.
        """
        cx, cy = x >> 1, y >> 2
        if not (0 <= cx < self.cell_w and 0 <= cy < self.cell_h):
            return
        self._overlay[(cx, cy)] = (glyph, color, True)
        self._occupied.add((cx, cy))

    def marker_label(
        self,
        x: int,
        y: int,
        text: str,
        color: RGB,
        *,
        label_color: Optional[RGB] = None,
        avoid_dots: bool = False,
    ) -> bool:
        """Place a label beside the marker at dot ``(x, y)``, only if it fits cleanly.

        The label goes to the right of the marker (one blank cell gap) when there's room,
        else to the left — but never over another marker or label. When neither side is
        free the label is dropped and just the marker glyph shows, so a crowded map stays
        legible. With ``avoid_dots`` a spot is also rejected when braille dots already sit
        under it, so a caller can first sweep for placements clear of the drawn lines and
        only then settle for one that overprints them. Returns whether the label was
        placed.
        """
        text = single_cell(text)
        if not text:
            return False
        cx, cy = x >> 1, y >> 2
        if not (0 <= cx < self.cell_w and 0 <= cy < self.cell_h):
            return False
        lc = label_color or color
        for start in (cx + 2, cx - 1 - len(text)):
            if avoid_dots and not self._dot_free(start, cy, len(text)):
                continue
            if self._place_run(start, cy, text, lc, bold=True, checked=True):
                return True
        return False

    def _dot_free(self, start_cx: int, cy: int, length: int) -> bool:
        """Whether the run of cells at ``(start_cx…, cy)`` holds no braille dots."""
        if not (0 <= cy < self.cell_h):
            return False
        if start_cx < 0 or start_cx + length > self.cell_w:
            return False
        return all(self._bits[cy][mx] == 0 for mx in range(start_cx, start_cx + length))

    def place_label(
        self,
        x: float,
        y: float,
        text: str,
        color: RGB,
        *,
        bold: bool = False,
        avoid_dots: bool = False,
    ) -> bool:
        """Write a centered basemap label at dot ``(x, y)`` if it fits without collision.

        Args:
            x: Anchor dot x.
            y: Anchor dot y.
            text: Label text.
            color: Text colour.
            bold: Whether to embolden (used for the most important places).
            avoid_dots: Also reject the spot when braille dots already sit under the
                run, so a caller can sweep for a placement clear of the drawn lines
                before settling for one that overprints them (as :meth:`marker_label`
                does for its side placements).

        Returns:
            ``True`` if placed, ``False`` if it fell off-canvas or overlapped existing text.
        """
        text = single_cell(text)
        if not text:
            return False
        cx = int(x) >> 1
        cy = int(y) >> 2
        start = cx - len(text) // 2
        if avoid_dots and not self._dot_free(start, cy, len(text)):
            return False
        return self._place_run(start, cy, text, color, bold=bold, checked=True)

    def _place_run(
        self,
        start_cx: int,
        cy: int,
        text: str,
        color: RGB,
        *,
        bold: bool,
        checked: bool = False,
    ) -> bool:
        """Write ``text`` starting at cell ``(start_cx, cy)``.

        With ``checked`` the run is skipped entirely if it would run off-canvas, overprint
        or touch any claimed cell on its own row, or stack flush against another label on
        the row directly above or below; otherwise it is forced and simply clipped to the
        canvas. The same-row margin keeps a label off its neighbours (markers included);
        the vertical margin guards only against *label* stacking — which is what otherwise
        lets dense areas silt up into a solid block of text — so a label may still sit
        immediately above or below a single-glyph marker. Returns whether anything was placed.
        """
        if not (0 <= cy < self.cell_h):
            return False
        cells = range(start_cx, start_cx + len(text))
        if checked:
            if start_cx < 0 or start_cx + len(text) > self.cell_w:
                return False
            margin = range(start_cx - 1, start_cx + len(text) + 1)
            if any((mx, cy) in self._occupied for mx in margin):
                return False
            if any(
                (mx, my) in self._label_cells
                for my in (cy - 1, cy + 1)
                for mx in margin
            ):
                return False
        placed = False
        for offset, ch in enumerate(text):
            mx = start_cx + offset
            if 0 <= mx < self.cell_w:
                self._overlay[(mx, cy)] = (ch, color, bold)
                self._occupied.add((mx, cy))
                self._label_cells.add((mx, cy))
                placed = True
        return placed

    # -- output -----------------------------------------------------------------

    def to_ansi_lines(self) -> list[str]:
        """Render the canvas to one truecolour ANSI string per row."""
        reset = "\x1b[0m"
        lines: list[str] = []
        for cy in range(self.cell_h):
            parts: list[str] = []
            cur: Optional[tuple[RGB, bool]] = None
            for cx in range(self.cell_w):
                overlay = self._overlay.get((cx, cy))
                if overlay is not None:
                    ch, color, bold = overlay
                elif self._bits[cy][cx]:
                    bits = self._bits[cy][cx]
                    ch = chr(_BRAILLE_BASE + bits)
                    color = self._color[cy][cx] or (128, 128, 128)
                    bold = False
                else:
                    if cur is not None:
                        parts.append(reset)
                        cur = None
                    parts.append(" ")
                    continue
                style = (color, bold)
                if style != cur:
                    r, g, b = color
                    parts.append(f"\x1b[0m\x1b[38;2;{r};{g};{b}m" + ("\x1b[1m" if bold else ""))
                    cur = style
                parts.append(ch)
            if cur is not None:
                parts.append(reset)
            lines.append("".join(parts))
        return lines
