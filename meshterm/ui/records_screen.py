"""The Trophy case: the record-setting walks the trace tools have turned up.

A read-only board over the ``discovered_paths`` table (:meth:`Repository.discoveries`),
opened from the main menu. Every successful trace — a *Trace target* boomerang or a
*Trace path* walk — is scored against every discipline and offered to their boards
(see :mod:`~meshterm.services.records`); this screen is where the survivors live.

* the browser groups the disciplines, each under its heading with a one-line
  description of the game (word-wrapped when it must) — the two pin overhead together as
  one block while that board scrolls, so a row deep in a discipline still says which game
  it is winning and what that game scores — then the discipline's records ranked
  best-first: the day, the score in the discipline's own unit, and the walk itself on THE
  path widget with both ends bare (every record is a boomerang, so the cells go to the
  hops). A discipline holding records at more than one hash width tags each row with its
  width, since the widths are genuinely different games;
* opening a record floats :class:`RecordDialog` — every stat the walk was measured by
  (the far point named with the node it reached, the longest leg with the link it spanned),
  the walk drawn two ways: on THE route graph (the Message paths dialog's shape, us at both
  ends, drawn no taller than one lane
  needs) and, beside the stats, as the enclosed area it swept on a braille mini-map (us and
  every positioned hop, coloured node pins, no labels); then the full route — the path
  widget again, unlabelled across the card's whole width, wrapping at hop boundaries — and
  the spec, and when/by which app version it was
  set. The card scrolls (PgUp/PgDn/Home/End) when it outgrows the terminal. From there
  *Trace this path* reopens Trace path with the record's route prefilled, so a claim
  worth re-testing is one Enter from the air again — and when that screen closes, the
  flow unwinds to the main menu rather than re-entering the browser, so the user is
  never left many Escs deep;
* deletion comes in three grains, each behind a red data-loss dialog: one record
  behind a Cancel/Delete confirm, and — the bulk grains — one discipline (every width)
  or everything, each gated behind typing ``delete``.

Nothing here transmits: it reads the boards the trace tools filled.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.geo import haversine_km
from ..persistence.repository import DiscoveredPath
from ..services import trace_runner
from ..services.records import CATEGORIES, CATEGORY_BY_ID, Category, _local_xy
from .mapcanvas import RGB, MapCanvas
from .menus import fit_cells, icon_lane, marked_label, section_heading
from .pathgraph import PathLayer, render_path_graph
from .pathline import path_line
from .theme import glyph, name_style, snr_style
from .tui.render import render_hanging, render_to_ansi
from .tui.screen import Screen
from .widgets import (
    NodeResolver,
    TypeOf,
    name_rgb,
    node_marker,
    node_type_legend,
    route_graph_style,
    self_marker,
)

if TYPE_CHECKING:
    from ..context import AppContext

#: The width the discipline descriptions wrap at — comfortably inside 72 columns so the
#: prose reads the same however wide the terminal is.
_DESC_WRAP = 64

#: The colour a record's single walk draws in on the route graph. It is the only path
#: on the canvas, so it needs no colour to set it apart — white just reads as "the walk".
_WALK_EDGE = (255, 255, 255)

#: Canvas rows a record's route graph draws in. One walk is one lane, so the graph is flat
#: and needs only its marker row with a row either side for the labels — the shared widget's
#: own default (5) leaves a scored walk sitting in two rows of blank canvas. A fan would grow
#: past this on its own; nothing here ever fans.
_GRAPH_ROWS = 3

#: The area drawing's one body tone — a plain muted slate the enclosed region and its loop
#: both draw in. Deliberately a single shade (no dimmer interior wash under a brighter rim):
#: an even-odd fill shades only the enclosed side, and the coloured node pins carry the
#: colour, so the shape reads as one flat silhouette rather than two brightnesses.
_AREA_TONE: RGB = (148, 163, 184)

#: The area drawing's cell box, to the right of the stats: a width that scales with the
#: dialog but stays inside these bounds, extra rows spent below the stats so the shape has
#: room, and a floor of columns below which it is dropped (the stats reclaim the full
#: width) rather than squeezed into illegibility.
_POLY_MIN_W = 16
_POLY_MAX_W = 28
_POLY_EXTRA_ROWS = 2
_SIDE_BY_SIDE_MIN = 44

#: Cells the record card's label lane spans. Every labelled row on the card — the stat
#: lanes, the spec row's hanging indent, the recorded line — shares this one column, so
#: the values align whatever the label says.
_LABEL_W = 12


def discipline_label(category: Category, lane: int) -> str:
    """One discipline's mark padded to ``lane`` cells, then its title.

    THE way a board's name is written, so the seven headings — and the seven rows of the
    delete picker — start their titles in one column instead of two. The marks are not all
    the same width: ``🛣`` and ``🕸`` sit outside Emoji_Presentation and are one cell where
    the other five are two, which is what left ``🛣 Longest distance`` a column adrift of
    ``🎯 Farthest node``, and its gap looking closed on a terminal that paints the road
    wider than it advances (JP, 2026-09-09).

    The same rule as a command row's icon column (:func:`~meshterm.ui.menus.icon_lane`) and
    deliberately not that function: a command row *drops* its icon where the platform draws
    no icon lane, and a board's mark is part of its name on both platforms. So the lane is
    measured over what :func:`~meshterm.ui.theme.glyph` will actually draw here, and the
    marks stay.

    Args:
        category: The discipline being named.
        lane: The mark column's width, from :func:`discipline_lane`.

    Returns:
        ``"<mark><pad><title>"``, its trailing separator space included in the pad.
    """
    mark = glyph(category.icon)
    return f"{mark}{' ' * max(1, lane - cell_len(mark) + 1)}{category.title}"


def discipline_lane() -> int:
    """Cells the widest discipline mark claims on this platform (see :func:`discipline_label`).

    Measured, never written down: the marks change with the platform, and a substitution
    that later narrows simply tightens every heading by a cell without anything else moving.
    """
    return max(cell_len(glyph(category.icon)) for category in CATEGORIES)


def _drawn_rows(lines: list[str]) -> list[str]:
    """``lines`` with the rows nothing actually landed on stripped from either end.

    Both blocks the card stacks are drawn on a canvas sized for the worst case and then
    filled: the stats' area drawing keeps :data:`_POLY_EXTRA_ROWS` in hand for a tall shape,
    and the route graph pads a row either side of its markers for the labels. A shape that
    comes out flat, or labels that all seat on one side, leave those rows empty — and an
    empty row still costs a line of card, stacking up as a gap the layout never intended
    (three blank lines between the stats and the graph where one was meant). Trimming happens
    *after* the drawing, so nothing placed is ever lost: only rows that stayed blank go.
    """
    kept = list(lines)
    while kept and not Text.from_ansi(kept[-1]).plain.strip():
        kept.pop()
    while kept and not Text.from_ansi(kept[0]).plain.strip():
        kept.pop(0)
    return kept


@dataclass(frozen=True, slots=True)
class WalkVertex:
    """One positioned point of a walk's circuit, projected onto the local km plane.

    Our own node sits at the origin and every hop is placed relative to it (east/north
    kilometres), so the vertices draw the same shape the area was scored over. Each
    carries its own map marker so the drawing pins nodes in the shared palette.

    Attributes:
        x: Kilometres east of our node.
        y: Kilometres north of our node.
        glyph: The node-type marker to pin at this point.
        color: The marker's truecolour.
        is_self: Whether this vertex is our own node (the yellow star at the origin).
    """

    x: float
    y: float
    glyph: str
    color: RGB
    is_self: bool


class RecordDialog(Screen):
    """One record's full story, floating over the screen beneath.

    Every stat the walk was measured by — a reliability read from the far node's trace
    history, the far point named with the node it reached, the longest leg drawn as the
    link it spanned (our own end on the app-wide ★) — with the walk's enclosed area
    drawn beside them on a braille mini-map (us at the origin, every positioned hop pinned
    in its own name hue, no labels) when the terminal has the room; below, the walk on THE
    route graph (us at both ends, relays wearing their map marker over a node-type key),
    the route in full on THE path widget — no label lane, the card's whole width, wrapped
    at hop boundaries and never truncated — and the
    record's provenance — when it was set and by which app version. The card scrolls
    (PgUp/PgDn/Home/End) when it outgrows the frame, while the arrows drive the actions.
    Two actions besides Back: *Trace this path* reopens Trace path with the record's route
    prefilled (propagation shifts; a record is a claim worth re-testing), and *Delete…*
    removes this one record behind a red Cancel/Delete data-loss confirm.
    """

    def __init__(
        self,
        record: DiscoveredPath,
        category: Category,
        rank: int,
        *,
        resolve: NodeResolver,
        device_label: str,
        device_hash: str | None,
        far_label: str | None = None,
        far_id: str | None = None,
        shape: Sequence[WalkVertex] | None = None,
        reliability: tuple[float, int, int] | None = None,
        type_of: TypeOf | None = None,
    ) -> None:
        """Build the dialog for one stored record.

        Args:
            record: The record to show.
            category: Its category (for the score's unit and title).
            rank: Its standing on the board (1 = the record holder).
            resolve: Maps node ids to friendly names.
            device_label: Our node's name, bracketing the route.
            device_hash: Our public key, annotated at the record's width.
            far_label: The farthest node's name, shown beside its distance; ``None`` when
                no positioned hop was named.
            far_id: The farthest node's id, seeding the name's hash-derived hue; ``None``
                leaves the label on the name-derived fallback.
            shape: The walk's positioned circuit, projected for the area drawing; ``None``
                below the three points a polygon needs.
            reliability: ``(rate, successes, total)`` of traces to the walk's far node, or
                ``None`` when that node was never a trace target.
            type_of: Maps a route hash to its node type, so relays draw their map marker
                (``▲`` repeater, …) on the graph; ``None`` falls back to generic dots.
        """
        super().__init__()
        self.title = f"Record — {category.title} #{rank}"
        self.footer_hint = "↑↓ move · PgUp/PgDn scroll · Enter select · Esc back"
        self._record = record
        self._category = category
        self._resolve = resolve
        self._device_label = device_label
        self._device_hash = device_hash
        self._far_label = far_label
        self._far_id = far_id
        self._shape = list(shape) if shape else None
        self._reliability = reliability
        self._type_of = type_of
        self._actions = ("trace", "delete")
        self._index = 0
        self._cursor: int | None = None
        # The composed card above the action rows, per width (see render_body).
        self._upper_cache: tuple[int, list[str]] | None = None
        # The card opens at the top, reading down; the arrows drive (and follow) the action
        # cursor, while PgUp/PgDn/Home/End scroll the body free of it (see cursor_line).
        self._follow = False

    @property
    def dialog_width(self) -> int:
        """A comfortable reading width; the compositor still caps it to the frame."""
        return 62

    def handle(self, action: str, data: str = "") -> None:
        """Move the action cursor, scroll the card, commit the selection, or dismiss."""
        if action == "up":
            self._follow = True
            # Both ends clamp rather than wrap — the app-wide rule for a row cursor:
            # a highlight that leaps end to end takes the results window with it.
            self._index = max(0, self._index - 1)
        elif action == "down":
            self._follow = True
            self._index = min(len(self._actions) - 1, self._index + 1)
        elif action in ("pageup", "ctrl_pageup"):
            self._follow = False
            self.scroll_pages(-1)
        elif action in ("pagedown", "space", "ctrl_pagedown"):
            self._follow = False
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self._follow = False
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self._follow = False
            self.scroll_to_bottom()
        elif action == "enter":
            self.resolve(self._actions[self._index])
        elif action == "escape":
            self.resolve(None)

    def cursor_line(self) -> int | None:
        """Keep the selected action visible while arrowing; scroll free once paging."""
        return self._cursor if self._follow else None

    @staticmethod
    def _label(label: str) -> Text:
        """The card's muted label lane, padded to :data:`_LABEL_W` cells."""
        return Text(f"{label:<{_LABEL_W}}", style="muted")

    def _lane(self, label: str, value: Text) -> Text:
        """One label/value stat lane (label lane fixed so values align)."""
        row = self._label(label)
        row.append_text(value)
        return row

    def _graph_lines(self, width: int) -> list[str]:
        """Draw the walked route as one path on THE route graph — us at both ends.

        A scored walk leaves home and comes back, so it draws us (left) → its relays →
        us (right) as a single white path through the shared route-graph widget
        (:func:`~meshterm.ui.pathgraph.render_path_graph`), the same shape the Message
        paths dialog draws a delivery over. Relays wear their own map marker (``▲``
        repeater, …) where the type is known. Nodes the walk passed through more than once
        — a boomerang's mirrored return leg — collapse to their first appearance, and the
        route line below still carries every hop, revisits and all.

        That collapse is a *deliberate* choice here, not the widget's limit: a graph can draw
        each visit its own marker (``allow_duplicate_nodes``, which the observed-packet surfaces
        turn on), and this one declines. A trophy record scores a path **we composed**, so
        re-walking one node must never make the shape look bigger than the ground it covered —
        one node, one marker, whatever the spec asked for. An overheard via chain has the
        opposite duty: nothing there is ours to inflate, and a repeat is evidence.

        One walk is one lane, so the graph draws in :data:`_GRAPH_ROWS` — the marker row and
        a row either side for the labels — rather than the widget's default, which reserves
        room for a fan this canvas never carries and spends it on blank rows. Whichever of
        those two label rows stays empty is handed back (:func:`_drawn_rows`), so the graph
        sits exactly one blank line under the stats and the caption follows it directly.
        """
        seen: set[str] = set()
        hops: list[str] = []
        for node in self._record.route:
            if node not in seen:
                seen.add(node)
                hops.append(node)
        glyph_of, label_of, label_rgb_of = route_graph_style(
            resolve=self._resolve,
            self_name=self._device_label,
            source=self._device_label,
            type_of=self._type_of,
        )
        return _drawn_rows(
            render_path_graph(
                [PathLayer(hops=tuple(hops), color=_WALK_EDGE, priority=3)],
                width,
                glyph_of=glyph_of,
                label_of=label_of,
                label_rgb_of=label_rgb_of,
                min_rows=_GRAPH_ROWS,
            )
        )

    def _stat_lanes(self) -> list[Text]:
        """Every stat the walk was measured by, as label/value lanes (score → round trip).

        Which lanes appear varies by discipline — a walk with no positions has no distance,
        far point, or area — so callers size to the returned list rather than a fixed count.
        The far-point lane carries the reached node's name when one is known, and the
        longest-leg lane the link it spanned; a record set before a stat existed simply has
        no lane for it.
        """
        record, category = self._record, self._category
        stats = record.stats
        lanes: list[Text] = []

        score = category.format_score(record.score)
        if category.id == "long_haul" and not stats.get("km_complete", True):
            score = "≥ " + score
        lanes.append(self._lane("score", Text(score, style="accent bold")))

        if self._reliability is not None:
            rate, ok, total = self._reliability
            value = Text(f"{rate:.0%}", style=snr_style(20 * rate - 10))
            value.append(f"  · {ok}/{total} traces to far node", style="muted")
            lanes.append(self._lane("reliability", value))

        hops = stats.get("hop_count", len(record.route))
        distinct = stats.get("distinct_nodes", len(set(record.route)))
        walk = Text(f"{hops} hop{'s' if hops != 1 else ''}")
        walk.append(f" · {distinct} distinct node{'s' if distinct != 1 else ''}", style="muted")
        if stats.get("repeats"):
            walk.append(" · revisits", style="muted")
        lanes.append(self._lane("walk", walk))

        km = stats.get("km_travelled")
        if km:
            value = Text(f"{'≥ ' if not stats.get('km_complete', True) else ''}{km:.1f} km")
            lanes.append(self._lane("distance", value))
        far = stats.get("far_km")
        if far is not None:
            value = Text(f"{far:.1f} km")
            if self._far_label:
                value.append("  ")
                value.append(self._far_label, style=name_style(self._far_label, self._far_id))
            lanes.append(self._lane("far point", value))
        leg = stats.get("leg_km")
        if leg is not None:
            value = Text(f"{leg:.1f} km")
            link = stats.get("leg_link")
            if link and len(link) == 2:
                # The link itself on THE path widget, both ends named the way the walk
                # below names them — our own end on the app-wide ★, since a leg that
                # starts or ends at home is the commonest kind there is.
                value.append("  ")
                value.append_text(
                    path_line(
                        list(link),
                        self._resolve,
                        self_name=self._device_label,
                        bare_self=True,
                        dim_self=False,
                        show_hash=False,
                    ).text()
                )
            lanes.append(self._lane("longest leg", value))
        area = stats.get("area_km2")
        if area is not None:
            lanes.append(self._lane("area", Text(f"{area:.1f} km²")))
        snr = stats.get("min_snr")
        if snr is not None:
            lanes.append(self._lane("weakest", Text(f"{snr:+.1f} dB", style=snr_style(snr))))
        rtt = stats.get("rtt_ms")
        if rtt is not None:
            lanes.append(self._lane("round trip", Text(f"{rtt:.0f} ms")))
        return lanes

    def _compose_stats(self, lanes: list[Text], width: int) -> list[str]:
        """Lay the stat lanes out, the area drawing pinned to their right when it fits.

        With a drawable walk and room to spare, the polygon takes a fixed cell box on the
        right and the lanes are cropped to the column beside it; too narrow, or no shape,
        and the lanes reclaim the whole width and the drawing is dropped. The block is
        trimmed to the rows something landed on (:func:`_drawn_rows`), so a shape that comes
        out flat hands its unused rows back instead of leaving the card gaping under the
        last stat.
        """
        if not self._shape or width < _SIDE_BY_SIDE_MIN:
            return [render_to_ansi(lane, width, no_wrap=True) for lane in lanes]
        poly_w = min(_POLY_MAX_W, max(_POLY_MIN_W, width // 3))
        left_w = width - poly_w - 2
        # Spend a couple of rows beyond the stat lanes so the shape has room; the extra
        # rows hang just below the last stat, in the air above the route graph.
        poly = self._shape_lines(poly_w, len(lanes) + _POLY_EXTRA_ROWS)
        out: list[str] = []
        for i in range(len(poly)):
            row = lanes[i].copy() if i < len(lanes) else Text()
            row.no_wrap = True
            row.truncate(left_w, overflow="ellipsis", pad=True)
            row.append("  ")
            row.append_text(Text.from_ansi(poly[i]))
            out.append(render_to_ansi(row, width, no_wrap=True))
        return _drawn_rows(out)

    def _shape_lines(self, cell_w: int, cell_h: int) -> list[str]:
        """Draw the walk's enclosed area on a braille canvas: fill, loop, coloured pins.

        The projected circuit (us at the origin, every positioned hop around it) is scaled
        to the cell box preserving true proportions — braille dots are square, so equal x/y
        scaling keeps the geography honest — then filled as a shaded region, outlined as the
        walked loop, and pinned with each node's map marker. No labels: the pins carry the
        node types and the stats beside them carry the numbers.
        """
        verts = self._shape or []
        canvas = MapCanvas(cell_w, cell_h)
        xs = [v.x for v in verts]
        ys = [v.y for v in verts]
        span_x = (max(xs) - min(xs)) or 1e-6
        span_y = (max(ys) - min(ys)) or 1e-6
        pad = 2.0
        avail_w = max(1.0, canvas.dot_w - 1 - 2 * pad)
        avail_h = max(1.0, canvas.dot_h - 1 - 2 * pad)
        scale = min(avail_w / span_x, avail_h / span_y)
        origin_x = pad + (avail_w - span_x * scale) / 2
        origin_y = pad + (avail_h - span_y * scale) / 2
        min_x, max_y = min(xs), max(ys)
        # Flip y so north points up: the northernmost point lands at the top dot row.
        ring = [(origin_x + (v.x - min_x) * scale, origin_y + (max_y - v.y) * scale) for v in verts]
        closed = ring + ring[:1]
        # One flat tone for the enclosed side and its loop: even-odd shades only the odd
        # (enclosed) region — a self-crossing walk's crossing stays unshaded — and nothing
        # is drawn darker than anything else, so there is no dimmer sub-region.
        canvas.fill_polygon([closed], _AREA_TONE, priority=0)
        canvas.draw_line(closed, _AREA_TONE, priority=0)
        # Hops first, then our own node last, so the star always wins its cell — a hop that
        # projects onto the same cell can never hide us.
        for v, (dx, dy) in zip(verts, ring, strict=True):
            if not v.is_self:
                canvas.marker(int(round(dx)), int(round(dy)), v.glyph, v.color)
        for v, (dx, dy) in zip(verts, ring, strict=True):
            if v.is_self:
                canvas.marker(int(round(dx)), int(round(dy)), v.glyph, v.color)
        return canvas.to_ansi_lines()

    def render_body(self, width: int) -> list[str]:
        """Stats lanes, the route graph, the full route, provenance, then the actions."""
        # Everything above the action rows — the stat lanes, the area drawing, the
        # route graph and the provenance — is a pure function of the frozen record
        # and the width, so it is composed once per width; only the cursor-bearing
        # action rows below re-render per repaint.
        cached = self._upper_cache
        if cached is not None and cached[0] == width:
            lines = list(cached[1])
        else:
            lines = self._upper_lines(width)
            self._upper_cache = (width, list(lines))

        lines.append("")
        self._cursor = None
        # One measured icon column for both action rows: 🗑 is one cell where 👣 is two, so
        # each mark measured on its own started "Delete record…" a column left of "Trace this
        # path". Empty where the platform draws no icons, and the labels take the cells back.
        lane = icon_lane(("👣", "🗑"))
        for i, key in enumerate(self._actions):
            selected = i == self._index
            row = Text("❯ " if selected else "  ", style="cursor" if selected else "")
            if key == "trace":
                row.append_text(
                    marked_label(
                        "👣", "Trace this path — reopen in Trace path", "accent", lane=lane
                    )
                )
            else:
                row.append_text(marked_label("🗑", "Delete record…", "err", lane=lane))
            if selected:
                row.style = "cursor"
                self._cursor = len(lines)
            row.no_wrap = True
            row.truncate(width, overflow="ellipsis")
            lines.append(render_to_ansi(row, width))
        self._scroll_total = max(1, len(lines))
        return lines

    def _upper_lines(self, width: int) -> list[str]:
        """Compose the record card above the action rows (memoized per width)."""
        record = self._record
        lines: list[str] = []

        lines.extend(self._compose_stats(self._stat_lanes(), width))

        lines.append("")
        lines.extend(self._graph_lines(width))
        caption = Text("you → … → you · labels = hash byte", style="faint")
        lines.append(render_to_ansi(caption, width, no_wrap=True))
        lines.append(render_to_ansi(node_type_legend(), width, no_wrap=True))

        lines.append("")
        # The walk itself, on THE path widget, across the card's whole width. No ``route``
        # label lane: the line under a route graph captioned "you → … → you" is the route,
        # and the twelve cells a label would take are hops the reader came here for. It wraps
        # at hop boundaries (never mid-name, never mid-chip) rather than hanging under a lane.
        # Our two ends stand on the app-wide ★ rather than repeating our name and key — the
        # graph above already marks us with the same star at both ends, and the legend under
        # it reads that star back as "you" — kept in the ``you`` white, since nothing about a
        # walk already made is ours to compose.
        route = path_line(
            [None, *record.route, None],
            self._resolve,
            prefix_bytes=record.width_bytes,
            self_name=self._device_label,
            show_hash=True,
            hash_bytes=record.width_bytes,
            device_hash=self._device_hash,
            bare_self=True,
            dim_self=False,
        )
        lines.extend(render_to_ansi(line, width, no_wrap=True) for line in route.wrapped(width))
        spec = Text(record.spec, style="brand")
        spec.append(f"  ({record.width_bytes}-byte hops)", style="muted")
        lines.extend(render_hanging(self._label("spec"), spec, width, indent=_LABEL_W))

        stamp = record.discovered_at.astimezone().strftime("%b %d %Y %H:%M")
        when = Text(stamp)
        when.append(f" · MeshTerm {record.app_version}", style="muted")
        lines.append(render_to_ansi(self._lane("recorded", when), width, no_wrap=True))
        return lines


async def open_records(ctx: AppContext) -> dict:
    """Open the Trophy case browser and run it until dismissed.

    Wires the browser to the database and the observed contacts: records come straight
    from :meth:`Repository.discoveries`, and hop hashes resolve to friendly names
    through the same resolver the trace screens use. *Trace this path* hands off to the
    live Trace path screen with the record's route prefilled; when that screen closes,
    this flow returns (unwinding to the main menu) instead of reopening the browser.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Returns:
        A summary dict for the tool's run row (the record count shown).

    Raises:
        RuntimeError: If called outside the interactive menu.
    """
    from .surface import TuiUi
    from .trace_screen import open_trace_path
    from .tui import CANCEL, Choice, SelectScreen, Separator

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu caller
        raise RuntimeError("the Trophy case screen is only available in the menu")
    session = ctx.ui.session

    contacts = await ctx.devstate.contacts()
    self_info = await ctx.devstate.self_info()
    resolve = trace_runner.make_node_resolver(contacts, ctx.repo.node_names())
    device_label = str(self_info.get("name") or "us")
    device_hash = str(self_info.get("public_key") or "") or None

    def _as_float(value) -> float | None:  # noqa: ANN001
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    lat, lon = _as_float(self_info.get("adv_lat")), _as_float(self_info.get("adv_lon"))
    self_pos = (lat, lon) if lat is not None and lon is not None and (lat or lon) else None

    # Per-target trace outcomes, for a record's reliability (the success rate of traces to
    # its far node). Read once here; the far node is matched against these keys per record.
    target_counts = ctx.repo.target_trace_counts()

    # The mark column every board heading writes its title after (see discipline_label).
    mark_lane = discipline_lane()

    # Node positions and types for a record's area drawing, gathered live the same way the
    # trace tools gather them to score a walk: adverts we've heard, contacts over them. A
    # record stores canonical ids, so match those the resolver's way (a prefix either side).
    node_entries: list[tuple[str, tuple[float, float] | None, int | None]] = []
    for heard in ctx.repo.heard_nodes():
        if not heard.node:
            continue
        pos = (heard.lat, heard.lon) if heard.has_location else None
        node_entries.append((heard.node.lower().removeprefix("0x"), pos, heard.node_type))
    for contact in contacts:
        ident = (contact.public_key or contact.key_prefix or "").lower().removeprefix("0x")
        if not ident:
            continue
        pos = (
            (contact.lat, contact.lon)
            if (contact.has_location and (contact.lat or contact.lon))
            else None
        )
        node_entries.append((ident, pos, contact.node_type))

    # Memoized: the boards ask for the same route nodes over and over (every hop of
    # every record, records sharing hops), and the entry list is fixed for this open —
    # so each distinct id pays the prefix scan once.
    geo_memo: dict[str, tuple[tuple[float, float] | None, int | None]] = {}

    def node_geo(node_id: str) -> tuple[tuple[float, float] | None, int | None]:
        """A route node's best-known position and type across the heard/contact entries."""
        cached = geo_memo.get(node_id)
        if cached is not None:
            return cached
        needle = node_id.lower().removeprefix("0x")
        pos: tuple[float, float] | None = None
        ntype: int | None = None
        for ident, epos, etype in node_entries:
            if not (ident.startswith(needle) or needle.startswith(ident)):
                continue
            if pos is None:
                pos = epos
            if ntype is None:
                ntype = etype
            if pos is not None and ntype is not None:
                break
        geo_memo[node_id] = (pos, ntype)
        return pos, ntype

    def walk_drawing(
        record: DiscoveredPath,
    ) -> tuple[str | None, str | None, list[WalkVertex] | None]:
        """The farthest node (name and id) and the walk's projected polygon.

        Mirrors the scoring geometry (see :mod:`~meshterm.services.records`): our node at
        the plane's origin, every positioned hop projected onto the same local km plane in
        walk order, the farthest hop named. That farthest hop is the walk's natural target,
        so its id is handed back for the reliability lookup. A polygon needs three points —
        us plus two positioned hops — so a sparser walk yields no drawing but still names
        its far point.
        """
        if self_pos is None:
            return None, None, None
        glyph, color = self_marker()
        verts = [WalkVertex(0.0, 0.0, glyph, color, True)]
        far_label: str | None = None
        far_id: str | None = None
        far_dist = -1.0
        for node_id in record.route:
            pos, ntype = node_geo(node_id)
            if pos is None:
                continue
            east, north = _local_xy(self_pos, pos)
            # The node-type glyph in the node's *name* hue — the pin reads as that mesh
            # name, the way the route line and graph labels colour it; an unnamed hop keeps
            # the neutral shape tone so it never masquerades as a coloured name.
            name = resolve(node_id)
            hop_glyph = node_marker(ntype)[0]
            hop_color = name_rgb(name, node_id) if name and name != node_id else _AREA_TONE
            verts.append(WalkVertex(east, north, hop_glyph, hop_color, False))
            dist = haversine_km(self_pos[0], self_pos[1], pos[0], pos[1])
            if dist > far_dist:
                far_dist = dist
                far_id = node_id
                far_label = name if name and name != node_id else None
        return far_label, far_id, (verts if len(verts) >= 3 else None)

    def walk_reliability(
        far_id: str | None, far_label: str | None
    ) -> tuple[float, int, int] | None:
        """A record's observed reliability: the success rate of traces to its far node.

        A walk's route can't be counted from history — a timed-out trace records no path —
        but its farthest node *is* the target a boomerang was aimed at, and every trace
        carries its target, failures included. So the far node's success rate (matched
        against the filed targets by name or hex hash) is an honest delivery figure. Thin
        by nature (the count is shown alongside); ``None`` when the node was never a target.
        """
        if not far_id:
            return None
        needle = far_id.lower().removeprefix("0x")
        wanted = far_label.lower() if far_label else None
        ok = total = 0
        for target, (t_ok, t_n) in target_counts.items():
            key = target.lower().removeprefix("0x")
            is_hex = bool(key) and all(c in "0123456789abcdef" for c in key)
            if (wanted is not None and key == wanted) or (
                is_hex and (key.startswith(needle) or needle.startswith(key))
            ):
                ok += t_ok
                total += t_n
        return (ok / total, ok, total) if total else None

    def ranked(category: Category) -> list[DiscoveredPath]:
        """One discipline's records, ranked best-first across every hash width."""
        rows = ctx.repo.discoveries(category.id)
        rows.sort(key=lambda r: r.score, reverse=not category.ascending)
        return rows

    def describe(category: Category) -> list:
        """The heading and its word-wrapped description as non-selectable rows."""
        rows: list = [section_heading(discipline_label(category, mark_lane))]
        desc = category.description
        if category.needs_positions and self_pos is None:
            desc += " — needs your location (set it in Config) to score"
        for line in textwrap.wrap(desc, _DESC_WRAP):
            rows.append(Separator(f"   {line}", style="muted"))
        return rows

    def scored(category: Category, record: DiscoveredPath) -> str:
        """A record's score in its discipline's unit, bounded where the walk was partial."""
        score = category.format_score(record.score)
        if category.id == "long_haul" and not record.stats.get("km_complete", True):
            score = "≥ " + score
        return score

    def browser_lanes(
        rank: int,
        category: Category,
        record: DiscoveredPath,
        *,
        show_width: bool,
        score_w: int,
    ) -> Text:
        """A record row's fixed lanes: rank, date, score, and the width when it disambiguates.

        Held to what they actually say — the day the record was set (the dialog carries the
        time), and a score lane fitted to the widest score on *this* board rather than a fixed
        twelve — because every cell they don't spend is a hop the route gets to show. Composed
        without a width, since nothing here fits itself to the terminal: this block is the
        same on every platform and at every size, which is also what makes it the row's
        h-scroll anchor (see :func:`browser_row`).
        """
        row = Text(f"#{rank} ", style="muted")
        row.append(record.discovered_at.astimezone().strftime("%b %d"), style="muted")
        row.append("  ")
        row.append(fit_cells(scored(category, record), score_w), style="accent")
        if show_width:
            row.append(f" {record.width_bytes} B", style="muted")
        row.append("  ")
        return row

    def browser_row(lanes: Text, record: DiscoveredPath) -> Text:
        """One record row: its fixed lanes (:func:`browser_lanes`), then the whole walk.

        The walk draws on THE path widget with both ends bare
        (:data:`~meshterm.ui.pathline.SELF_GLYPH`): a record is a boomerang by construction,
        so naming ourselves twice per row would cost more cells than the whole score lane and
        tell the reader what every other row already told them. Those stars keep the ``you``
        white (``dim_self=False``) — the fade means "not yours to compose", and nothing on a
        board of walks already made is being composed.

        The line is composed at its **natural** length and handed over whole, so every row is
        cut the same way the highlighted one is: anchored on its first hop and cracked at the
        lane's edge (:func:`~meshterm.ui.pathline.cut_to`, applied by the list). It is
        deliberately *not* middle-elided to the width. That rescue exists to save a route's
        two endpoints from a right truncation — but on this board both endpoints are the
        same ``★`` on every row, by construction, so the ``⋯`` would spend three cells and a
        hop to arrive back at the one thing the reader already knows. What differs from row to
        row is the walk's front, and every cell goes to it.

        A row that outruns the lane is read by *sliding* it: the highlight keeps the same line
        and h-scrolls under ←→ from the lanes' own width (``Choice.hscroll_from``), so the walk
        is the only thing on the row that moves — sliding rank and score off to the left would
        cost the reader their place in the board and buy back cells the walk was already going
        to be given. Unselected and selected therefore differ in exactly one thing, the shift.
        """
        row = lanes.copy()
        walk = path_line(
            [None, *record.route, None],
            resolve,
            prefix_bytes=record.width_bytes,
            hash_bytes=record.width_bytes,
            self_name=device_label,
            bare_self=True,
            dim_self=False,
        )
        row.append_text(walk.text())
        return row

    async def delete_category_flow() -> None:
        """Pick a discipline, confirm, and delete its records (every width)."""
        rows: list = []
        lane = discipline_lane()
        for category in CATEGORIES:
            count = len(ctx.repo.discoveries(category.id))
            rows.append(
                Choice(
                    title=Text.assemble(
                        (f"{discipline_label(category, lane)}  ", ""),
                        (f"{count} record{'s' if count != 1 else ''}, all widths", "muted"),
                    ),
                    value=category.id,
                )
            )
        picked = await session.run_screen(
            SelectScreen(
                "Delete a discipline's records",
                rows,
                footer_hint="↑↓ move · Enter select · Esc back",
                filterable=False,
            )
        )
        if picked is CANCEL or picked is None:  # Esc
            return None
        category = CATEGORY_BY_ID[str(picked)]
        count = len(ctx.repo.discoveries(category.id))
        if await session.typed_confirm(
            f"This deletes all {count} {category.title} "
            f"record{'s' if count != 1 else ''} — every hash width. "
            "They can only be re-earned by walking them again.",
            "delete",
            title="Delete discipline records",
        ):
            ctx.repo.delete_discoveries(category.id)

    while True:
        items: list = []
        for category in CATEGORIES:
            items.extend(describe(category))
            board = ranked(category)
            if not board:
                items.append(Separator("   no records yet", style="muted"))
            show_width = len({r.width_bytes for r in board}) > 1
            # The score lane is this board's own widest score — "3 nodes" and "+6.0 dB"
            # measure differently, and a lane sized for the worst case everywhere would
            # spend the difference on padding in front of every route.
            score_w = max((cell_len(scored(category, r)) for r in board), default=0)
            for rank, record in enumerate(board, start=1):
                lanes = browser_lanes(
                    rank,
                    category,
                    record,
                    show_width=show_width,
                    score_w=score_w,
                )
                items.append(
                    Choice(
                        # A finished line, not a width-aware callable: the row fits itself to
                        # nothing, so the list cuts every row — highlighted or not — the one way
                        # (see browser_row). Records are frozen, so it is composed once per open
                        # rather than per repaint.
                        title=browser_row(lanes, record),
                        value=("open", category, rank, record),
                        # ←→ slide the walk alone; the lanes in front of it hold (browser_row).
                        hscroll_from=lanes.cell_len,
                    )
                )
        total = len(ctx.repo.discoveries())
        if total:
            items.append(Separator(" "))
            items.append(
                Choice(
                    title=marked_label("🗑", "Delete a discipline's records…", "err"),
                    value=("del_cat", None, 0, None),
                )
            )
            items.append(
                Choice(
                    title=marked_label("🗑", "Delete all records…", "err"),
                    value=("del_all", None, 0, None),
                )
            )
        browser = SelectScreen(
            "Trophy case",
            items,
            footer_hint="↑↓ move · Enter open · Esc back",
            hscroll=True,  # a long walk slides under ←→ instead of dying at the fold
        )
        # The browser stays pushed for the whole visit, so every dialog that belongs *over*
        # the trophy case — the discipline picker, the delete confirms, a record's floating
        # story — already has it drawn full-frame behind them, and the cursor is still on the
        # record just read when they close. The loop only leaves (and the list only rebuilds,
        # losing that place) when the record set itself has changed under it.
        async with session.stay(browser) as visit:
            while True:
                picked = await visit.result()
                if picked is CANCEL or picked is None:  # Esc
                    return {"records": total}
                verb = picked[0]
                if verb == "del_cat":
                    await delete_category_flow()
                elif verb == "del_all":
                    if total and await session.typed_confirm(
                        f"This deletes all {total} records — every discipline, every width. "
                        "They can only be re-earned by walking them again.",
                        "delete",
                        title="Delete all records",
                    ):
                        ctx.repo.delete_discoveries()
                else:
                    _verb, category, rank, record = picked
                    far_label, far_id, shape = walk_drawing(record)
                    action = await session.run_screen(
                        RecordDialog(
                            record,
                            category,
                            rank,
                            resolve=resolve,
                            device_label=device_label,
                            device_hash=device_hash,
                            far_label=far_label,
                            far_id=far_id,
                            shape=shape,
                            reliability=walk_reliability(far_id, far_label),
                            type_of=lambda node_id: node_geo(node_id)[1],
                        )
                    )
                    if action == "trace":
                        # Walking a record's path opens the Trace screen *above* the trophy
                        # case, like any other sub-view: Esc from the trace is one pop back
                        # onto the record it was armed from, and ^W is what leaves the whole
                        # excursion. (It used to flatten the stack first and land the reader
                        # on the main menu, because there was no key that could climb out of
                        # a deep stack in one press. Now there is.)
                        await open_trace_path(ctx, spec=record.spec)
                    elif action == "delete":
                        sure = await ctx.ui.dialog(
                            f"Delete this {category.title} record?",
                            [("Cancel", False), ("Delete", True)],
                            title="Delete record",
                            default=1,
                            destructive=True,
                        )
                        if sure:
                            ctx.repo.delete_discovery(record.id)
                # A trace can set a record and any of the deletes can drop several, so the
                # board is rebuilt only when the record count actually moved — the one thing
                # worth losing the cursor over.
                if len(ctx.repo.discoveries()) != total:
                    break
