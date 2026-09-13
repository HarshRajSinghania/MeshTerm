# SPDX-License-Identifier: Apache-2.0
"""The basemap's credit — OpenStreetMap's name, in the corner of the map's own frame.

The street map is rendered from OpenStreetMap data, served as vector tiles by
OpenFreeMap on the unmodified OpenMapTiles schema. All three want naming, and the
OpenStreetMap Foundation's *Attribution Guideline* (adopted 2021-06-25) says where and
how precisely enough to build against:

    "For a browsable map (e.g., embedded in a web page or application), the credit
    should typically appear in a corner of the map. While the lower right corner is
    traditional, any corner of the map is acceptable."

    "Attribution must be to 'OpenStreetMap'." … "The historical forms of attribution
    '© OpenStreetMap contributors' or '© OpenStreetMap' are acceptable."

    "The attribution format should not require individuals to interact with the map or
    produced work to see the attribution."

    "You may use a mechanism to fade/collapse the attribution under certain conditions:
    … automatically on map interaction such as panning, clicking, or zooming …"

    "If the attribution has been collapsed, the user must still be able to find the
    licence information if they look for it, for example from an '(i)' button in the
    corner of the map or an 'About' option in a menu."

OpenFreeMap asks for one line of its own — *"OpenFreeMap © OpenMapTiles Data from
OpenStreetMap"*, adding *"You do not need to display the OpenFreeMap part, but it is
nice if you do."* — so :data:`CREDIT_FULL` is that line less its optional half, and
:data:`CREDIT_SHORT` is the remnant it collapses to.

That is the whole design, and it is built to cost the map almost nothing:

* **The credit rides the map's own frame, never a line of its own.** No title atom, no
  footer character, no body row: the title is a status line already chaining four atoms
  and the footer hint spends its whole 72-cell budget, and a credit bolted to either
  would be paid for on every screen of every visit. *Where* it rides is the one thing
  that differs by platform, and only because the platforms' frames differ. A bordered
  frame has a **bottom border rule**, so the credit sits in it the way a title sits in
  the top rule — right-justified, muted, one rule cell before the corner — and the
  drawing itself is left alone (JP, 2026-09-13: *"that way it's not in the map"*). The
  PicoCalc's frame has no bottom rule at all — a borderless title bar above, the F-key
  lane below, and body rows in between — so there the credit is **stamped over the right
  end of the drawing's own bottom row**, which is still the corner the guideline calls
  traditional, and costs the map fifteen cells of ground a pan can move out from under.
  That form was read on the device's own panel and signed off as it stands (JP,
  2026-09-13), so its glyphs, its position and its collapse are settled: leave them alone.
  :func:`rule_caption` and :func:`stamp` are the two forms; :func:`map_body` picks.
* **It arrives whole and shrinks once used.** A map the reader has not touched yet shows
  :data:`CREDIT_FULL`, so the attribution is never something you have to interact to
  see; the first pan, zoom, reframe or find keystroke collapses it to
  :data:`CREDIT_SHORT`, which is itself one of the two forms OSMF names as acceptable.
  So the collapse permission is spent on OpenMapTiles' half of the line and the
  OpenStreetMap credit simply never goes away.
* **The licence lives on the About page**, which is the guideline's own worked example
  of where a collapsed credit's licence information may be found — an ``About`` option
  in a menu. ``meshterm/assets/pages/about.md`` carries the full line, the ODbL and the
  ``openstreetmap.org/copyright`` URL spelled out, because nothing on a framebuffer
  console is clickable. That page is reachable from the main menu at all times, so the
  map needs no reveal key of its own — which is worth more here than any key would be:
  the map binds no plain letters (they all feed the find filter), and on the PicoCalc an
  unadvertised key is an undiscoverable one, so a reveal would have had to buy a chip on
  a five-slot F-key lane that is already full.

Where it *is* stamped on the drawing — the PicoCalc's map, and the location preview on
both platforms — the mark **wins the cells it sits in**: it is spliced over the rendered
row, so a street, a braille dot or a node label under it is overprinted rather than
shifted. The guideline requires the attribution to be "legible and understandable", which
a credit a passing label could shred is not — and the map is pannable, so ground hidden
under fifteen cells of one row is a keypress away, while the credit is not allowed to be.

Its style is ``muted``: this is chrome drawn on the map, not content on it. It must not
be mistaken for a node, so it takes neither the ``you`` white nor any key-derived node
hue — the one lane in the app where grey is exactly the right claim.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from ..platforms import Platform, on_platform
from .tui.render import crop_cells, render_to_ansi

#: The whole credit, shown until the reader first touches the map. OpenFreeMap's required
#: line with its optional "OpenFreeMap" half dropped — 40 cells, which fits inside both
#: platforms' readable widths (72 regular, 53 PicoCalc) with room to spare.
CREDIT_FULL = "© OpenMapTiles · Data from OpenStreetMap"

#: What the credit collapses to once the map has been used, and the only form the small
#: static preview ever draws. 15 cells, and a complete OpenStreetMap attribution in its
#: own right ("The historical forms … '© OpenStreetMap' are acceptable"), so nothing about
#: the OSM credit depends on the collapse permission — only OpenMapTiles' half does.
CREDIT_SHORT = "© OpenStreetMap"

#: Blank cells kept between whatever the map drew and the start of the mark, so the credit
#: never reads as the tail of a street name it happens to land beside.
_GAP = 1

#: Whether this platform's frame has a bottom border rule for the credit to sit in, rather
#: than the map having to give up cells of its own bottom row. Bound once per platform
#: switch (:func:`_bind`) so no paint ever asks the platform this question itself.
_HAS_RULE = True


@on_platform
def _bind(platform: Platform) -> None:
    """Settle where the credit goes when the platform is chosen, not when a frame is drawn."""
    global _HAS_RULE
    _HAS_RULE = platform.frame_border


def credit(*, full: bool) -> Text:
    """The credit as a styled run — :data:`CREDIT_FULL` or :data:`CREDIT_SHORT`.

    Args:
        full: Whether to give the whole line (an untouched map) rather than the remnant.

    Returns:
        The mark, ``muted`` and ``no_wrap``.
    """
    return Text(CREDIT_FULL if full else CREDIT_SHORT, style="muted", no_wrap=True)


def stamp(lines: list[str], width: int, *, full: bool, left: Text | None = None) -> list[str]:
    """The map rows with the credit stamped into the right end of the last one.

    The rows arrive as raw ANSI straight from
    :meth:`~meshterm.ui.mapcanvas.MapCanvas.to_ansi_lines`, so the bottom one is parsed
    back (:meth:`rich.text.Text.from_ansi`), cut to the cells the mark leaves it
    (:func:`~meshterm.ui.tui.render.crop_cells`, which measures in display cells and so
    never halves a braille glyph), and re-rendered with the mark appended. That is the
    same parse the frame does to every body line on its way into the panel, so the ground
    survives it unchanged on both platforms — the PicoCalc's already-quantized colours
    included.

    ``left`` replaces the row's content rather than cropping it, for the one caller that
    has something else to put there: on a platform whose footer is the F-key lane the map
    echoes its live find query over this very row (see
    :meth:`~meshterm.ui.map_screen.MapScreen._query_echo`), and the two share it — query
    from the left, credit pinned right, the query cropped if it runs long. The credit is
    the one that may not be cut.

    A row too narrow to hold even :data:`CREDIT_SHORT` beside a blank cell takes no mark
    at all; a surface that small has no room to be legible in, and the About page is still
    carrying the full statement.

    Nothing is mutated: a caller may hand over a frame it is caching (the map serves one
    raster across many paints) and get a fresh list back, so the credit is never stamped
    into ground that the next paint will stamp over again.

    Args:
        lines: The rendered map rows.
        width: The row width in cells.
        full: Whether the credit is still in its whole form (see :func:`credit`).
        left: Content for the rest of the row, replacing what was drawn there.

    Returns:
        A new list of rows — the input's, with its last rewritten.
    """
    if not lines or width <= 0:
        return lines
    mark = credit(full=full)
    span = cell_len(mark.plain)
    if span + _GAP > width:  # the whole line won't sit here — try the remnant
        mark = credit(full=False)
        span = cell_len(mark.plain)
        if span + _GAP > width:
            return lines
    keep = width - span
    row = crop_cells(left if left is not None else Text.from_ansi(lines[-1]), 0, keep - _GAP)
    row.pad_right(max(0, keep - cell_len(row.plain)))
    row.append_text(mark)
    return [*lines[:-1], render_to_ansi(row, width, no_wrap=True)]


def rule_caption(*, full: bool) -> str:
    """The credit for the frame's bottom border rule — ``""`` where the frame draws none.

    A bordered frame already has a rule under the body doing nothing but closing the box,
    and a caption in it is the cheapest place the credit can possibly go: it takes no cell
    of the drawing and no cell of any line the app was otherwise using. The frame renders
    it right-justified through Rich's own subtitle machinery
    (:func:`~meshterm.ui.tui.frame._panel_box`), so it lands as ``──── © OpenStreetMap ─╯``
    — one rule cell before the corner, exactly as a title sits in the top rule.

    Empty on the PicoCalc, whose frame has no bottom rule to sit in: there the credit is
    stamped on the drawing instead (:func:`stamp`). Both callers ask unconditionally and
    one of them gets nothing, so neither has to know which platform it is on.

    Args:
        full: Whether the credit is still in its whole form (see :func:`credit`).

    Returns:
        The caption, or ``""`` where this platform's frame has no rule for it.
    """
    return (CREDIT_FULL if full else CREDIT_SHORT) if _HAS_RULE else ""


def map_body(lines: list[str], width: int, *, full: bool, left: Text | None = None) -> list[str]:
    """The full-screen map's rows, credited wherever this platform's frame cannot do it.

    The counterpart to :func:`rule_caption`, and the reason the map's ``render_body`` needs
    no platform test of its own: exactly one of the two marks the map, and this is the one
    that draws nothing where the frame's bottom rule is carrying the credit instead.

    ``left`` (the find query echo) is drawn either way, because it belongs to the map
    rather than to the credit — though in practice only the rule-less platform asks for it,
    that being the same platform whose footer is the F-key lane and so has nowhere else to
    put the query.

    Args:
        lines: The rendered map rows.
        width: The row width in cells.
        full: Whether the credit is still in its whole form (see :func:`credit`).
        left: The find query echo for the bottom row, or ``None``.

    Returns:
        A new list of rows (the input's own list where there is nothing to draw).
    """
    if not _HAS_RULE:
        return stamp(lines, width, full=full, left=left)
    if left is None or not lines or width <= 0:
        return lines
    return [*lines[:-1], render_to_ansi(crop_cells(left, 0, width), width, no_wrap=True)]
