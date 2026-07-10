"""A reusable ring spinner: a thick braille circle with a comet of dots chasing round it.

Where :class:`~meshterm.ui.tui.spinner.Spinner` is one animated cell, this is a larger,
attention-grabbing *area* animation — a bold circle drawn in Braille dots with a bright arc
that orbits the rim, its dots fading from a near-white head back through the blues. It exists
because Bluetooth operations lag noticeably more than serial ones, and a menu navigation that
talks to a BLE companion can sit on a blank screen for a beat; a big centred spinner makes
that wait read as deliberate work rather than a hang.

Like :class:`Spinner`, it is deliberately screen-agnostic: it holds a rotation phase, a
caller advances it with :meth:`tick` (typically from an animation timer), and :meth:`render`
returns a Rich renderable.

Braille geometry: a Braille cell packs a 2×4 grid of dots, and a terminal cell is about twice
as tall as it is wide, so a single Braille dot is *very nearly square* on screen (½ a cell
each way). That means a mathematical circle plotted in Braille-dot space also *looks* round,
which is the whole trick behind drawing a clean ring in text. Stroking the circle over a small
band of radii thickens it into a bold arc that reads unmistakably as a circle.
"""

from __future__ import annotations

import math
from typing import Optional

from rich.text import Text

#: Braille bit for each sub-dot position, indexed ``[y][x]`` within a 2-wide × 4-tall cell.
#: Adding these to the Braille block base (``U+2800``) yields the glyph for a set of dots.
_DOT_BITS = (
    (0x01, 0x08),  # top row:    dots 1, 4
    (0x02, 0x10),  # second row: dots 2, 5
    (0x04, 0x20),  # third row:  dots 3, 6
    (0x40, 0x80),  # bottom row: dots 7, 8
)
_BRAILLE_BASE = 0x2800


class RingSpinner:
    """An orbiting-comet ring drawn in thick Braille dots.

    The full rim is drawn in a calm blue; a bright arc (the "comet") is drawn over it and
    rotates one step per :meth:`tick`, its dots fading along a cyan→blue ramp from a near-white
    head to a deep-blue tail, so a glowing swarm of dots appears to chase itself around the
    circle.
    """

    #: The resting rim colour: a muted blue the comet stands out against.
    RIM_STYLE = "#2f5c96"

    #: The comet's colour ramp, head (near-white cyan) → tail (deep blue). The bright head
    #: catches the eye; the fade gives the orbiting dots a sense of motion and depth.
    COMET_RAMP = (
        "bold #ecfeff",
        "bold #a5f3fc",
        "#67e8f9",
        "#38bdf8",
        "#0ea5e9",
        "#0284c7",
        "#0369a1",
    )

    def __init__(
        self,
        *,
        radius: float = 10.5,
        thickness: float = 2.5,
        comet: float = math.radians(130),
        step: float = math.radians(16),
    ) -> None:
        """Configure a ring spinner.

        Args:
            radius: Circle radius in Braille dots (½-cell units). ~10.5 gives a bold 13×7-cell
                ring that reads clearly as a circle.
            thickness: Stroke width in Braille dots; the rim is drawn over this band of radii,
                so ~2.5 gives a chunky, unmistakably-round arc rather than a thin outline.
            comet: Angular length of the bright orbiting arc, in radians.
            step: How far the comet advances each :meth:`tick`, in radians.
        """
        self._radius = radius
        self._thickness = max(1.0, thickness)
        self._comet = comet
        self._step = step
        self._phase = 0.0
        # Precompute the geometry once — it never changes between frames, only the phase does.
        self._cols, self._rows, self._centre, self._cells = self._plot_rim()

    def tick(self) -> None:
        """Advance the comet one step around the rim (wrapping at a full turn)."""
        self._phase = (self._phase + self._step) % (2 * math.pi)

    def reset(self) -> None:
        """Return the comet to its starting position."""
        self._phase = 0.0

    # --- geometry ------------------------------------------------------------

    def _plot_rim(self) -> tuple[int, int, float, dict[tuple[int, int], int]]:
        """Stroke the thick rim into a Braille-cell grid, once, with exact 4-fold symmetry.

        Only the first quadrant is plotted; each lit dot is then mirrored across both axes, so
        the four quarters are identical by construction and no rounding asymmetry can creep in.
        The canvas is rounded up to a multiple of 4 dots so the mirror axes land on Braille-cell
        boundaries — that keeps the *packed cell grid* symmetric, not merely the raw dots.

        Returns:
            ``(cols, rows, centre, cells)`` — the grid size in cells, the ring centre in the
            square dot-space, and a map from each ``(col, row)`` cell to its OR'd Braille bits.
        """
        half = (self._thickness - 1) / 2.0
        outer = self._radius + half
        pad = 1
        dim = int(math.ceil((2 * outer + 2 * pad) / 4.0)) * 4  # multiple-of-4 dot canvas
        centre = (dim - 1) / 2.0
        edge = dim - 1
        # Sample the quarter arc densely enough that neither the angular step nor the ½-dot
        # radial step leaves a gap once mirrored.
        quadrant = max(60, int((math.pi / 2) * (outer + 1) / 0.4))
        radii = [self._radius + off for off in _frange(-half, half, 0.5)]
        cells: dict[tuple[int, int], int] = {}
        for i in range(quadrant + 1):
            angle = (math.pi / 2) * i / quadrant
            cos, sin = math.cos(angle), math.sin(angle)
            for rr in radii:
                px = int(round(centre + rr * cos))
                py = int(round(centre - rr * sin))
                # Mirror the one quadrant into all four for a perfectly symmetric ring.
                for mx, my in ((px, py), (edge - px, py), (px, edge - py), (edge - px, edge - py)):
                    key = (mx // 2, my // 4)
                    cells[key] = cells.get(key, 0) | _DOT_BITS[my % 4][mx % 2]
        # Report the *full* canvas size (not the lit bounding box): trimming to the lit extent
        # would shave unequal padding off the sides and break the mirror symmetry.
        return dim // 2, dim // 4, centre, cells

    def _cell_angle(self, col: int, row: int) -> float:
        """The polar angle (from the ring centre) of a cell's centre, in ``[0, 2π)``.

        Derived from geometry rather than the plotting pass so the comet sweeps smoothly
        regardless of how a cell's dots were accumulated.
        """
        cx = col * 2 + 0.5  # centre of the cell's 2-wide dot column
        cy = row * 4 + 1.5  # centre of the cell's 4-tall dot row
        return math.atan2(self._centre - cy, cx - self._centre) % (2 * math.pi)

    def _comet_style(self, angle: float) -> Optional[str]:
        """The comet colour for a rim cell at ``angle``, or ``None`` if it is outside the arc.

        Cells within ``comet`` radians *behind* the rotating head are lit; their position along
        the arc picks a colour from :attr:`COMET_RAMP`, so the head glows near-white and the
        tail fades into deep blue just before the plain rim resumes.
        """
        behind = (self._phase - angle) % (2 * math.pi)
        if behind > self._comet:
            return None
        step = self._comet / len(self.COMET_RAMP)
        index = min(len(self.COMET_RAMP) - 1, int(behind / step))
        return self.COMET_RAMP[index]

    # --- rendering -----------------------------------------------------------

    def render(self, brightness: float = 1.0) -> Text:
        """Render the current frame as a multi-line Rich :class:`~rich.text.Text`.

        Each rim cell is drawn in the rim colour unless the comet is currently over it, in
        which case it takes the comet's colour for its position along the arc.

        Args:
            brightness: A 0→1 dimmer applied to every colour, scaling it toward black. Used to
                fade the ring in from black; ``1.0`` renders at full colour.

        Returns:
            The ring as one ``Text`` whose lines are the ring's rows.
        """
        rim = dim_color(self.RIM_STYLE, brightness)
        text = Text()
        for row in range(self._rows):
            if row:
                text.append("\n")
            for col in range(self._cols):
                bits = self._cells.get((col, row))
                if not bits:
                    text.append(" ")
                    continue
                comet = self._comet_style(self._cell_angle(col, row))
                style = dim_color(comet, brightness) if comet else rim
                text.append(chr(_BRAILLE_BASE + bits), style=style)
        return text

    @property
    def width(self) -> int:
        """The ring's width in terminal cells (its natural render width)."""
        return self._cols

    @property
    def height(self) -> int:
        """The ring's height in terminal cells (its number of rows)."""
        return self._rows


def _frange(start: float, stop: float, step: float) -> list[float]:
    """Return ``[start, start+step, …]`` up to and including ``stop`` (a float ``range``)."""
    out = [start]
    while out[-1] + step <= stop + 1e-9:
        out.append(out[-1] + step)
    return out


def dim_color(style: str, factor: float) -> str:
    """Scale a Rich hex-colour style toward black by ``factor`` (0 = black, 1 = unchanged).

    Only the ``#rrggbb`` token is scaled; any attribute words (e.g. ``bold``) are preserved.
    A style without a hex colour is returned untouched, as is any ``factor >= 1``.

    Args:
        style: A Rich style such as ``"#38bdf8"`` or ``"bold #ecfeff"``.
        factor: The brightness multiplier, clamped to ``[0, 1]``.

    Returns:
        The style with its colour dimmed toward black.
    """
    if factor >= 1.0:
        return style
    factor = max(0.0, factor)
    parts = []
    for token in style.split():
        if token.startswith("#") and len(token) == 7:
            r = round(int(token[1:3], 16) * factor)
            g = round(int(token[3:5], 16) * factor)
            b = round(int(token[5:7], 16) * factor)
            parts.append(f"#{r:02x}{g:02x}{b:02x}")
        else:
            parts.append(token)
    return " ".join(parts)
