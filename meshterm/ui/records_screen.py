"""The Trophy case: the record-setting walks the trace tools have turned up.

A read-only board over the ``discovered_paths`` table (:meth:`Repository.discoveries`),
opened from the main menu. Every successful trace — a *Trace target* boomerang or a
*Trace path* walk — is scored against all six disciplines and offered to their boards
(see :mod:`~meshterm.services.records`); this screen is where the survivors live.

* the browser groups the six disciplines, each under its heading with a one-line
  description of the game (word-wrapped when it must), then that discipline's records
  ranked best-first — dated, scored in the discipline's own unit, with the walked route
  through THE path widget. A discipline holding records at more than one hash width tags
  each row with its width, since the widths are genuinely different games;
* opening a record floats :class:`RecordDialog` — every stat the walk was measured by
  (the far point named with the node it reached), the walk drawn two ways: on THE route
  graph (the Message paths dialog's shape, us at both ends) and, beside the stats, as the
  enclosed area it swept on a braille mini-map (us and every positioned hop, coloured node
  pins, no labels); then the full route and spec, and when/by which app version it was
  set. The card scrolls (PgUp/PgDn/Home/End) when it outgrows the terminal. From there
  *Trace this path* reopens Trace path with the record's route prefilled, so a claim
  worth re-testing is one Enter from the air again;
* deletion comes in three grains, each behind a red data-loss dialog: one record
  behind a Cancel/Delete confirm, and — the bulk grains — one discipline (every width)
  or everything, each gated behind typing ``delete``.

Nothing here transmits: it reads the boards the trace tools filled.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence

from rich.text import Text

from ..core.geo import haversine_km
from ..persistence.repository import DiscoveredPath
from ..services import trace_runner
from ..services.records import CATEGORIES, CATEGORY_BY_ID, Category, _local_xy
from .mapcanvas import RGB, MapCanvas
from .menus import back_rows, section_heading
from .pathgraph import PathLayer, render_path_graph
from .theme import name_style, snr_rgb, snr_style
from .tui.render import render_hanging, render_to_ansi
from .tui.screen import Screen
from .widgets import (
    NodeResolver,
    TypeOf,
    name_rgb,
    node_marker,
    node_type_legend,
    path_text,
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
    history, the far point named with the node it reached — with the walk's enclosed area
    drawn beside them on a braille mini-map (us at the origin, every positioned hop pinned
    in its own name hue, no labels) when the terminal has the room; below, the walk on THE
    route graph (us at both ends, coloured by its weakest link, relays wearing their map
    marker over a node-type key), the route in full (wrapped, never truncated), and the
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
        device_hash: Optional[str],
        far_label: Optional[str] = None,
        shape: Optional[Sequence[WalkVertex]] = None,
        reliability: Optional[tuple[float, int, int]] = None,
        type_of: Optional[TypeOf] = None,
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
            shape: The walk's positioned circuit, projected for the area drawing; ``None``
                below the three points a polygon needs.
            reliability: ``(rate, successes, total)`` of traces to the walk's far node, or
                ``None`` when that node was never a trace target.
            type_of: Maps a route hash to its node type, so relays draw their map marker
                (``▲`` repeater, …) on the graph; ``None`` falls back to generic dots.
        """
        super().__init__()
        self.title = f"Record — {category.title} #{rank}"
        self.footer_hint = "↑↓ move · PgUp/PgDn scroll · Enter commit · Esc back"
        self._record = record
        self._category = category
        self._resolve = resolve
        self._device_label = device_label
        self._device_hash = device_hash
        self._far_label = far_label
        self._shape = list(shape) if shape else None
        self._reliability = reliability
        self._type_of = type_of
        self._actions = ("trace", "delete", "back")
        self._index = 0
        self._cursor: Optional[int] = None
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
            self._index = (self._index - 1) % len(self._actions)
        elif action == "down":
            self._follow = True
            self._index = (self._index + 1) % len(self._actions)
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
            key = self._actions[self._index]
            self.resolve(None if key == "back" else key)
        elif action == "escape":
            self.resolve(None)

    def cursor_line(self) -> Optional[int]:
        """Keep the selected action visible while arrowing; scroll free once paging."""
        return self._cursor if self._follow else None

    def _lane(self, label: str, value: Text) -> Text:
        """One label/value stat lane (label lane fixed so values align)."""
        row = Text(f"{label:<12}", style="muted")
        row.append_text(value)
        return row

    def _graph_lines(self, width: int) -> list[str]:
        """Draw the walked route as one path on THE route graph — us at both ends.

        A scored walk leaves home and comes back, so it draws us (left) → its relays →
        us (right) as a single path through the shared fan-lane widget
        (:func:`~meshterm.ui.pathgraph.render_path_graph`), the same shape the Message
        paths dialog draws a delivery over. The path is coloured by the walk's weakest link
        (the SNR palette), so the loop reads red when a hop barely carried; relays wear
        their own map marker (``▲`` repeater, …) where the type is known. Nodes the walk
        passed through more than once — a boomerang's mirrored return leg — collapse to
        their first appearance: the depth-ordered graph can only seat a node once, and the
        route line below still carries every hop, revisits and all.
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
        min_snr = self._record.stats.get("min_snr")
        color = snr_rgb(min_snr) if min_snr is not None else _WALK_EDGE
        return render_path_graph(
            [PathLayer(hops=tuple(hops), color=color, priority=3)],
            width,
            glyph_of=glyph_of, label_of=label_of, label_rgb_of=label_rgb_of,
        )

    def _stat_lanes(self) -> list[Text]:
        """Every stat the walk was measured by, as label/value lanes (score → round trip).

        Which lanes appear varies by discipline — a walk with no positions has no distance,
        far point, or area — so callers size to the returned list rather than a fixed count.
        The far-point lane carries the reached node's name when one is known.
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
        walk.append(f" · {distinct} distinct node{'s' if distinct != 1 else ''}",
                    style="muted")
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
                value.append(self._far_label, style=name_style(self._far_label))
            lanes.append(self._lane("far point", value))
        area = stats.get("area_km2")
        if area is not None:
            lanes.append(self._lane("area", Text(f"{area:.1f} km²")))
        snr = stats.get("min_snr")
        if snr is not None:
            lanes.append(self._lane(
                "weakest", Text(f"{snr:+.1f} dB", style=snr_style(snr))
            ))
        rtt = stats.get("rtt_ms")
        if rtt is not None:
            lanes.append(self._lane("round trip", Text(f"{rtt:.0f} ms")))
        return lanes

    def _compose_stats(self, lanes: list[Text], width: int) -> list[str]:
        """Lay the stat lanes out, the area drawing pinned to their right when it fits.

        With a drawable walk and room to spare, the polygon takes a fixed cell box on the
        right and the lanes are cropped to the column beside it; too narrow, or no shape,
        and the lanes reclaim the whole width and the drawing is dropped.
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
        return out

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
        ring = [
            (origin_x + (v.x - min_x) * scale, origin_y + (max_y - v.y) * scale)
            for v in verts
        ]
        closed = ring + ring[:1]
        # One flat tone for the enclosed side and its loop: even-odd shades only the odd
        # (enclosed) region — a self-crossing walk's crossing stays unshaded — and nothing
        # is drawn darker than anything else, so there is no dimmer sub-region.
        canvas.fill_polygon([closed], _AREA_TONE, priority=0)
        canvas.draw_line(closed, _AREA_TONE, priority=0)
        # Hops first, then our own node last, so the star always wins its cell — a hop that
        # projects onto the same cell can never hide us.
        for v, (dx, dy) in zip(verts, ring):
            if not v.is_self:
                canvas.marker(int(round(dx)), int(round(dy)), v.glyph, v.color)
        for v, (dx, dy) in zip(verts, ring):
            if v.is_self:
                canvas.marker(int(round(dx)), int(round(dy)), v.glyph, v.color)
        return canvas.to_ansi_lines()

    def render_body(self, width: int) -> list[str]:
        """Stats lanes, the route graph, the full route, provenance, then the actions."""
        record = self._record
        lines: list[str] = []

        lines.extend(self._compose_stats(self._stat_lanes(), width))

        lines.append("")
        lines.extend(self._graph_lines(width))
        caption = Text("you → … → you · labels = hash byte", style="faint")
        lines.append(render_to_ansi(caption, width, no_wrap=True))
        lines.append(render_to_ansi(node_type_legend(), width, no_wrap=True))

        lines.append("")
        route = path_text(
            [None, *record.route, None],
            self._resolve,
            self_name=self._device_label,
            show_hash=True,
            hash_bytes=record.width_bytes,
            device_hash=self._device_hash,
        )
        lines.extend(render_hanging(Text("route       ", style="muted"), route, width,
                                    indent=12))
        spec = Text(record.spec, style="brand")
        spec.append(f"  ({record.width_bytes}-byte hops)", style="muted")
        lines.extend(render_hanging(Text("spec        ", style="muted"), spec, width,
                                    indent=12))

        stamp = record.discovered_at.astimezone().strftime("%b %d %Y %H:%M")
        when = Text(stamp)
        when.append(f" · MeshTerm {record.app_version}", style="muted")
        lines.append(render_to_ansi(self._lane("recorded", when), width, no_wrap=True))

        lines.append("")
        self._cursor = None
        for i, key in enumerate(self._actions):
            if key == "back":
                lines.append("")
            selected = i == self._index
            row = Text("❯ " if selected else "  ", style="brand" if selected else "")
            if key == "trace":
                row.append("👣 ", style="accent")
                row.append("Trace this path — reopen in Trace path")
            elif key == "delete":
                row.append("🗑 ", style="err")
                row.append("Delete record…")
            else:
                row.append("Back")
            if selected:
                row.style = "brand"
                self._cursor = len(lines)
            row.no_wrap = True
            row.truncate(width, overflow="ellipsis")
            lines.append(render_to_ansi(row, width))
        self._scroll_total = max(1, len(lines))
        return lines


async def open_records(ctx: "AppContext") -> dict:
    """Open the Trophy case browser and run it until dismissed.

    Wires the browser to the database and the observed contacts: records come straight
    from :meth:`Repository.discoveries`, and hop hashes resolve to friendly names
    through the same resolver the trace screens use. *Trace this path* hands off to the
    live Trace path screen with the record's route prefilled and returns here after.

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

    def _as_float(value) -> Optional[float]:  # noqa: ANN001
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    lat, lon = _as_float(self_info.get("adv_lat")), _as_float(self_info.get("adv_lon"))
    self_pos = (lat, lon) if lat is not None and lon is not None and (lat or lon) else None

    # Per-target trace outcomes, for a record's reliability (the success rate of traces to
    # its far node). Read once here; the far node is matched against these keys per record.
    target_counts = ctx.repo.target_trace_counts()

    # Node positions and types for a record's area drawing, gathered live the same way the
    # trace tools gather them to score a walk: adverts we've heard, contacts over them. A
    # record stores canonical ids, so match those the resolver's way (a prefix either side).
    node_entries: list[tuple[str, Optional[tuple[float, float]], Optional[int]]] = []
    for heard in ctx.repo.heard_nodes():
        if not heard.node:
            continue
        pos = (heard.lat, heard.lon) if heard.has_location else None
        node_entries.append((heard.node.lower().removeprefix("0x"), pos, heard.node_type))
    for contact in contacts:
        ident = (contact.public_key or contact.key_prefix or "").lower().removeprefix("0x")
        if not ident:
            continue
        pos = (contact.lat, contact.lon) if (
            contact.has_location and (contact.lat or contact.lon)
        ) else None
        node_entries.append((ident, pos, contact.node_type))

    def node_geo(node_id: str) -> tuple[Optional[tuple[float, float]], Optional[int]]:
        """A route node's best-known position and type across the heard/contact entries."""
        needle = node_id.lower().removeprefix("0x")
        pos: Optional[tuple[float, float]] = None
        ntype: Optional[int] = None
        for ident, epos, etype in node_entries:
            if not (ident.startswith(needle) or needle.startswith(ident)):
                continue
            if pos is None:
                pos = epos
            if ntype is None:
                ntype = etype
            if pos is not None and ntype is not None:
                break
        return pos, ntype

    def walk_drawing(
        record: DiscoveredPath,
    ) -> tuple[Optional[str], Optional[str], Optional[list[WalkVertex]]]:
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
        far_label: Optional[str] = None
        far_id: Optional[str] = None
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
            hop_color = name_rgb(name) if name and name != node_id else _AREA_TONE
            verts.append(WalkVertex(east, north, hop_glyph, hop_color, False))
            dist = haversine_km(self_pos[0], self_pos[1], pos[0], pos[1])
            if dist > far_dist:
                far_dist = dist
                far_id = node_id
                far_label = name if name and name != node_id else None
        return far_label, far_id, (verts if len(verts) >= 3 else None)

    def walk_reliability(
        far_id: Optional[str], far_label: Optional[str]
    ) -> Optional[tuple[float, int, int]]:
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
        rows: list = [section_heading(f"{category.icon} {category.title}")]
        desc = category.description
        if category.needs_positions and self_pos is None:
            desc += " — needs your location (set it in Config) to score"
        for line in textwrap.wrap(desc, _DESC_WRAP):
            rows.append(Separator(f"   {line}", style="muted"))
        return rows

    def browser_row(
        rank: int, category: Category, record: DiscoveredPath, *, show_width: bool
    ) -> Text:
        """One record row: rank, date, score (width when it disambiguates), route."""
        row = Text(f"#{rank}  ", style="muted")
        row.append(record.discovered_at.astimezone().strftime("%b %d %H:%M"),
                   style="muted")
        row.append("  ")
        score = category.format_score(record.score)
        if category.id == "long_haul" and not record.stats.get("km_complete", True):
            score = "≥ " + score
        row.append(f"{score:<12}", style="accent")
        if show_width:
            row.append(f"{record.width_bytes} B  ", style="muted")
        row.append(" ")
        row.append_text(
            path_text(
                [None, *record.route, None],
                resolve,
                self_name=device_label,
                show_hash=True,
                hash_bytes=record.width_bytes,
                device_hash=device_hash,
            )
        )
        return row

    async def delete_category_flow() -> None:
        """Pick a discipline, confirm, and delete its records (every width)."""
        rows: list = []
        for category in CATEGORIES:
            count = len(ctx.repo.discoveries(category.id))
            rows.append(Choice(
                title=Text.assemble(
                    (f"{category.icon} {category.title}  ", ""),
                    (f"{count} record{'s' if count != 1 else ''}, all widths", "muted"),
                ),
                value=category.id,
            ))
        rows.extend(back_rows("__back__"))
        picked = await session.run_screen(
            SelectScreen(
                "Delete a discipline's records",
                rows,
                footer_hint="↑↓ move · Enter pick · Esc back",
                filterable=False,
                wrap=False,
            )
        )
        if picked in (CANCEL, None, "__back__"):
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
            for rank, record in enumerate(board, start=1):
                items.append(Choice(
                    title=browser_row(rank, category, record, show_width=show_width),
                    value=("open", category, rank, record),
                ))
        total = len(ctx.repo.discoveries())
        items.append(Separator(" "))
        if total:
            items.append(Choice(
                title=Text.assemble(("🗑 ", "err"), "Delete a discipline's records…"),
                value=("del_cat", None, 0, None),
            ))
            items.append(Choice(
                title=Text.assemble(("🗑 ", "err"), "Delete all records…"),
                value=("del_all", None, 0, None),
            ))
        items.extend(back_rows(("back", None, 0, None)))
        picked = await session.run_screen(
            SelectScreen(
                "Trophy case",
                items,
                footer_hint="↑↓ move · Enter open · Esc back",
                wrap=False,
            )
        )
        if picked is CANCEL or picked is None or picked[0] == "back":
            return {"records": total}
        verb = picked[0]
        if verb == "del_cat":
            await delete_category_flow()
            continue
        if verb == "del_all":
            if not total:
                continue
            if await session.typed_confirm(
                f"This deletes all {total} records — every discipline, every width. "
                "They can only be re-earned by walking them again.",
                "delete",
                title="Delete all records",
            ):
                ctx.repo.delete_discoveries()
            continue
        _verb, category, rank, record = picked
        far_label, far_id, shape = walk_drawing(record)
        action = await session.run_screen(RecordDialog(
            record, category, rank,
            resolve=resolve, device_label=device_label, device_hash=device_hash,
            far_label=far_label, shape=shape,
            reliability=walk_reliability(far_id, far_label),
            type_of=lambda node_id: node_geo(node_id)[1],
        ))
        if action == "trace":
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
