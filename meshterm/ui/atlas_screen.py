"""The Mesh Atlas: the mesh's observed shape, drawn as a live braille link graph.

The interactive face of the ``atlas`` tool. The trace path composer already distills
every fragment of topology we ever received — trace walks, firmware routes, RX-logged
relay chains, repeater neighbour tables — into one evidence graph
(:mod:`~meshterm.services.topology`); this screen is that graph made visible. Nothing
here transmits: the atlas is a reading of what the radio has already heard.

Our own node anchors the centre and every other node sits on a concentric ring at its
evidenced hop distance, so the picture answers the mesh's first spatial question — *how
far out is everything?* — at a glance. Edges are braille lines coloured by the link's
median SNR (green → amber → red, slate for links with no reading) and faded by evidence
age, drawn on the same :class:`~meshterm.ui.mapcanvas.MapCanvas` the street map uses.
Evidence islands — link clusters we know of only through a repeater's neighbour table,
with no observed path back to us — ride an outermost ring of their own.

``←``/``→`` walk the graph node by node: the selection brightens its links, dims the
rest, and fills the bottom panel with the node's links strongest-first; ``Enter`` floats
a detail dialog with every link's readings. ``r`` rebuilds from storage (new evidence
lands while the screen is open), ``Esc`` backs out.
"""

from __future__ import annotations

import asyncio
import math
import zlib
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from rich.text import Text

from ..core.models import (
    NODE_TYPE_LABELS,
    NODE_TYPE_REPEATER,
    Contact,
    utcnow,
)
from ..services.topology import Link, MeshTopology
from .map_render import _NODE, _REPEATER, _SELF
from .mapcanvas import RGB, MapCanvas, parse_hex
from .theme import snr_style
from .tui.render import render_to_ansi
from .tui.screen import Screen
from .widgets import _format_age

if TYPE_CHECKING:
    from ..context import AppContext

_TAU = math.tau

#: Horizontal / vertical dot-space margins the ring layout keeps clear of the canvas
#: edge, so a node's marker glyph and its collision-avoided label have room to land.
_PAD_X_DOTS = 16
_PAD_Y_DOTS = 5

#: Bottom lines reserved under the canvas for the info panel (a spacer + three rows).
_PANEL_H = 4

#: The widest a node label may render on the canvas before it is ellipsized.
_LABEL_W = 16

#: Radians each successive ring is twisted beyond its wish-fitted rotation. Sparse
#: rings (a two-node mesh is *pairs* all the way out) would otherwise all align along
#: their parents' axis and the whole graph would degenerate to one crowded line; the
#: twist staggers the rings so markers and labels get their own rows.
_RING_TWIST = 0.55

#: SNR (dB) → edge colour anchors, interpolated linearly and clamped at the ends: the
#: red/amber/green of the app's snr styles, so the graph and the tables agree.
_SNR_STOPS: tuple[tuple[float, RGB], ...] = (
    (-15.0, (239, 68, 68)),
    (0.0, (250, 204, 21)),
    (10.0, (74, 222, 128)),
)

#: Edge colour for a link with no SNR reading at all (e.g. known only from a route).
_NO_READING: RGB = (100, 116, 139)

#: Marker style for a node no contact matches — heard of, never identified.
_UNKNOWN = ("○", "#94a3b8")


def _snr_rgb(snr: Optional[float]) -> RGB:
    """The edge colour for a link's median SNR (see :data:`_SNR_STOPS`)."""
    if snr is None:
        return _NO_READING
    if snr <= _SNR_STOPS[0][0]:
        return _SNR_STOPS[0][1]
    if snr >= _SNR_STOPS[-1][0]:
        return _SNR_STOPS[-1][1]
    (x0, c0), (x1, c1) = next(
        (lo, hi) for lo, hi in zip(_SNR_STOPS, _SNR_STOPS[1:]) if lo[0] <= snr <= hi[0]
    )
    f = (snr - x0) / (x1 - x0)
    return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))  # type: ignore[return-value]


def _scaled(rgb: RGB, factor: float) -> RGB:
    """``rgb`` dimmed (or mildly brightened) by ``factor``, clamped to byte range."""
    return tuple(max(0, min(255, round(c * factor))) for c in rgb)  # type: ignore[return-value]


def _freshness(last_seen: Optional[datetime], now: datetime) -> float:
    """How brightly a link draws for its evidence age: 1.0 fresh → 0.5 stale."""
    if last_seen is None or getattr(last_seen, "tzinfo", None) is None:
        return 0.55
    age = (now - last_seen).total_seconds()
    if age < 86400:
        return 1.0
    if age < 7 * 86400:
        return 0.8
    return 0.5


def _hash_angle(node: str) -> float:
    """A deterministic per-node angle, so a node keeps its bearing across rebuilds.

    Derived from a CRC of the canonical id rather than :func:`hash`, which Python
    salts per process — the whole point is that YUL sits at the same clock position
    tomorrow as today.
    """
    return zlib.crc32(node.encode()) / 0xFFFFFFFF * _TAU


def _ring_angles(desired: dict[str, float], twist: float = 0.0) -> dict[str, float]:
    """Distribute one ring's members evenly, honouring their desired bearings.

    Members are placed at exactly even angular slots — the only spacing that keeps
    markers and their labels apart on a crowded ring — but in the *order* of the
    angles they wished for, and with the whole ring rotated to the circular best
    fit of those wishes plus ``twist`` (see :data:`_RING_TWIST`). A child therefore
    still comes out on its parent's side of the graph; it just never piles onto a
    sibling, and successive rings never fall into one straight line.
    """
    if not desired:
        return {}
    ordered = sorted(desired, key=lambda n: desired[n] % _TAU)
    count = len(ordered)
    slots = [i * _TAU / count for i in range(count)]
    sin_sum = sum(math.sin(desired[n] - s) for n, s in zip(ordered, slots))
    cos_sum = sum(math.cos(desired[n] - s) for n, s in zip(ordered, slots))
    phase = math.atan2(sin_sum, cos_sum) if (sin_sum or cos_sum) else 0.0
    return {n: (s + phase + twist) % _TAU for n, s in zip(ordered, slots)}


def radial_layout(
    self_id: str,
    links: list[tuple[str, str, float]],
    dot_w: int,
    dot_h: int,
) -> tuple[dict[str, tuple[int, int]], dict[str, int], frozenset[str]]:
    """Place every linked node on concentric rings around our own node.

    Rings are evidenced hop distance (BFS over the link graph from ``self_id``);
    nodes with no observed path to us — evidence islands — take one shared ring
    outside everything reached. Within a ring each node *wants* the angle of its
    strongest parent one ring in (ring one and islands want a stable per-id hash
    angle instead), and the wishes are then pushed apart (:func:`_relax`) so markers
    and labels never pile up. Rings span an ellipse fitted to the canvas, using the
    terminal's width rather than forcing a circle into it.

    Args:
        self_id: Canonical id of our own node (always placed, even with no links).
        links: The evidence graph as ``(a, b, strength)`` triples.
        dot_w: Canvas width in braille dots.
        dot_h: Canvas height in braille dots.

    Returns:
        ``(positions, rings, islands)``: dot-space coordinates per node, the ring
        number per node (0 = us), and the ids that had no path back to us.
    """
    adjacency: dict[str, dict[str, float]] = {self_id: {}}
    for a, b, strength in links:
        adjacency.setdefault(a, {})
        adjacency.setdefault(b, {})
        adjacency[a][b] = max(strength, adjacency[a].get(b, 0.0))
        adjacency[b][a] = max(strength, adjacency[b].get(a, 0.0))

    rings: dict[str, int] = {self_id: 0}
    frontier = [self_id]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for other in adjacency.get(node, {}):
                if other not in rings:
                    rings[other] = rings[node] + 1
                    nxt.append(other)
        frontier = nxt

    islands = frozenset(n for n in adjacency if n not in rings)
    if islands:
        island_ring = max(rings.values(), default=0) + 1
        for node in islands:
            rings[node] = island_ring
    max_ring = max(rings.values(), default=0)

    angles: dict[str, float] = {self_id: 0.0}
    for ring in range(1, max_ring + 1):
        members = sorted(n for n, r in rings.items() if r == ring)
        if not members:
            continue
        desired: dict[str, float] = {}
        for node in members:
            parents = sorted(
                (
                    (strength, p)
                    for p, strength in adjacency.get(node, {}).items()
                    if rings.get(p) == ring - 1 and p in angles and rings[p] > 0
                ),
                key=lambda t: -t[0],
            )
            if parents:
                # Near the strongest parent, offset a touch by identity so siblings
                # wish for distinct bearings before the even spacing orders them.
                desired[node] = (
                    angles[parents[0][1]] + (_hash_angle(node) / _TAU - 0.5) * 0.4
                )
            else:
                # Ring one (and any orphan) wishes for its stable per-id bearing, so
                # a node keeps roughly the same clock position across rebuilds.
                desired[node] = _hash_angle(node)
        angles.update(_ring_angles(desired, twist=_RING_TWIST * (ring - 1)))

    cx, cy = dot_w / 2.0, dot_h / 2.0
    rx = max(8.0, dot_w / 2.0 - _PAD_X_DOTS)
    ry = max(6.0, dot_h / 2.0 - _PAD_Y_DOTS)
    positions: dict[str, tuple[int, int]] = {}
    for node, ring in rings.items():
        if ring == 0:
            positions[node] = (round(cx), round(cy))
            continue
        frac = ring / max_ring if max_ring else 1.0
        a = angles[node]
        positions[node] = (
            round(cx + rx * frac * math.cos(a)),
            round(cy + ry * frac * math.sin(a)),
        )
    return positions, rings, islands


class AtlasScreen(Screen):
    """The full-screen atlas: canvas on top, a live info panel along the bottom."""

    floating = False
    footer_hint = "←/→ walk nodes · Enter details · r rebuild · Esc back"

    def __init__(
        self,
        *,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        topo: MeshTopology,
        contacts: dict[str, Contact],
        self_label: str,
        rebuild: Optional[Callable[[], MeshTopology]] = None,
    ) -> None:
        """Create the atlas over a built topology snapshot.

        Args:
            session: The running TUI session (size, repaints, and the detail dialog).
            topo: The evidence graph to draw.
            contacts: Contacts keyed by canonical id, for glyphs, names, and ages.
            self_label: Display name for our own node (its mesh name when known).
            rebuild: Rebuilds the graph from storage for the ``r`` key; ``None``
                leaves the snapshot fixed (tests, and the odd caller without a repo).
        """
        super().__init__()
        self._session = session
        self._topo = topo
        self._contacts = contacts
        self._self_label = self_label
        self._rebuild = rebuild
        #: Selected canonical id, or ``None`` for the whole-mesh overview.
        self._selected: Optional[str] = None
        #: Selection cycle: us first, then rings inside-out, each ring by angle.
        self._cycle: list[str] = []
        self._positions: dict[str, tuple[int, int]] = {}
        self._rings: dict[str, int] = {}
        self._islands: frozenset[str] = frozenset()
        self._layout_key: Optional[tuple[int, int, int]] = None
        self._generation = 0  # bumped by rebuild so the layout cache invalidates
        self._dialog_open = False
        self._needs_scrub = True  # braille smear scrub, exactly like the map

    # --- input -----------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Walk the selection, open details, rebuild, or dismiss."""
        if action == "escape":
            self.resolve(None)
            return
        if action in ("right", "down", "tab"):
            self._step_selection(1)
        elif action in ("left", "up"):
            self._step_selection(-1)
        elif action == "enter":
            self._open_details()
        elif action == "text":
            low = data.lower()
            if low == "r":
                self._do_rebuild()
            elif low == "q":
                self.resolve(None)
                return
        self._needs_scrub = True
        self._session.invalidate()

    def _step_selection(self, delta: int) -> None:
        """Cycle ``… → overview → us → ring by ring …`` in either direction."""
        if not self._cycle:
            return
        stops: list[Optional[str]] = [None, *self._cycle]
        at = stops.index(self._selected) if self._selected in stops else 0
        self._selected = stops[(at + delta) % len(stops)]

    def _do_rebuild(self) -> None:
        """Re-read the evidence and relayout, keeping the selection when it survives."""
        if self._rebuild is None:
            return
        self._topo = self._rebuild()
        self._generation += 1
        self._layout_key = None  # force a relayout on the next paint

    def _open_details(self) -> None:
        """Float the selected node's full link listing over the atlas."""
        node = self._selected
        if node is None or self._dialog_open:
            return
        self._dialog_open = True

        async def run() -> None:
            try:
                await self._session.message_dialog(
                    self._details_text(node), title=self._label(node)
                )
            finally:
                self._dialog_open = False
                self._session.invalidate()

        try:
            asyncio.ensure_future(run())
        except RuntimeError:  # pragma: no cover - no running loop (unit rendering)
            self._dialog_open = False

    # --- smear scrub (same fallback-glyph problem as the map) -------------------------

    def consume_edge_scrub(self) -> int:
        """Right-edge columns to force-repaint after a redraw (see the map screen)."""
        if not self._needs_scrub:
            return 0
        self._needs_scrub = False
        return 2

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Draw the graph canvas and the info panel to exactly the viewport height."""
        _, cell_h = self._session.base_body_size()
        canvas_h = max(4, cell_h - _PANEL_H)

        links = self._topo.links()
        self.title = (
            f"Mesh Atlas · {max(0, len(self._layout_nodes(links)) )} nodes"
            f" · {len(links)} links"
        )
        if not links:
            return self._empty_state(width)

        self._ensure_layout(width * 2, canvas_h * 4, links)
        canvas = MapCanvas(width, canvas_h)
        now = utcnow()

        for link in links:
            pa = self._positions.get(link.a)
            pb = self._positions.get(link.b)
            if pa is None or pb is None:
                continue
            color = _scaled(_snr_rgb(link.median_snr), _freshness(link.last_seen, now))
            priority = 2
            if self._selected is not None:
                if self._selected in (link.a, link.b):
                    priority = 3  # the selection's own links win their cells
                else:
                    color = _scaled(color, 0.3)
                    priority = 1
            canvas.draw_line([pa, pb], color, priority)

        for node in self._marker_order():
            x, y = self._positions[node]
            glyph, color_hex = self._glyph(node)
            rgb = (255, 255, 255) if node == self._selected else parse_hex(color_hex)
            canvas.marker(x, y, glyph, rgb)
        for node in self._marker_order():
            x, y = self._positions[node]
            label = self._label(node)
            if len(label) > _LABEL_W:
                label = label[: _LABEL_W - 1] + "…"
            _, color_hex = self._glyph(node)
            rgb = (255, 255, 255) if node == self._selected else parse_hex(color_hex)
            # Both sides of a crowded marker (the centre, typically) can be claimed;
            # retry a cell below, then above, before giving the label up entirely.
            for dy in (0, 4, -4):
                if canvas.marker_label(x, y + dy, label, rgb):
                    break

        return canvas.to_ansi_lines() + self._panel_lines(width, links)

    def _empty_state(self, width: int) -> list[str]:
        """A friendly explanation while the evidence graph is still empty."""
        lines = [
            Text(),
            Text("The atlas has no evidence to draw yet.", style="accent"),
            Text(),
            Text("Topology accrues passively as the mesh talks:", style="muted"),
            Text("  · every successful trace maps each link it walked", style="muted"),
            Text("  · firmware routes and overheard relay chains fill in more", style="muted"),
            Text("  · a repeater's neighbour table adds its own vantage point", style="muted"),
            Text(),
            Text("Run a trace, or just leave MeshTerm listening.", style="muted"),
        ]
        return [render_to_ansi(t, width, no_wrap=True) for t in lines]

    def _layout_nodes(self, links: list[Link]) -> set[str]:
        """Every node the graph mentions, plus us (drawn even when alone)."""
        nodes = {self._topo.self_id}
        for link in links:
            nodes.add(link.a)
            nodes.add(link.b)
        return nodes

    def _ensure_layout(self, dot_w: int, dot_h: int, links: list[Link]) -> None:
        """(Re)compute positions when the canvas size or the evidence changed."""
        key = (dot_w, dot_h, self._generation)
        if key == self._layout_key:
            return
        self._layout_key = key
        now = utcnow()
        triples = [(l.a, l.b, l.strength(now)) for l in links]
        self._positions, self._rings, self._islands = radial_layout(
            self._topo.self_id, triples, dot_w, dot_h
        )
        order = sorted(
            (n for n in self._positions if n != self._topo.self_id),
            key=lambda n: (self._rings.get(n, 99), _hash_angle(n)),
        )
        self._cycle = [self._topo.self_id, *order]
        if self._selected is not None and self._selected not in self._positions:
            self._selected = None  # the rebuild dropped it

    def _marker_order(self) -> list[str]:
        """Marker/label placement order: the selection first (its label must win),
        then us, then repeaters, then everything else working outward."""

        def rank(node: str) -> tuple[int, int, float]:
            if node == self._selected:
                lead = 0
            elif node == self._topo.self_id:
                lead = 1
            elif self._contacts.get(node) is not None and (
                self._contacts[node].node_type == NODE_TYPE_REPEATER
            ):
                lead = 2
            else:
                lead = 3
            return (lead, self._rings.get(node, 99), _hash_angle(node))

        return sorted(self._positions, key=rank)

    def _glyph(self, node: str) -> tuple[str, str]:
        """The marker glyph and hex colour for a node, by identity and type."""
        if node == self._topo.self_id:
            return _SELF
        contact = self._contacts.get(node)
        if contact is None:
            return _UNKNOWN
        if contact.node_type == NODE_TYPE_REPEATER:
            return _REPEATER
        return _NODE

    def _label(self, node: str) -> str:
        """A node's display name: its own name for us, contact name, or short hash."""
        if node == self._topo.self_id:
            return self._self_label
        return self._topo.display_name(node) or node[:8]

    # --- the info panel ------------------------------------------------------------

    def _panel_lines(self, width: int, links: list[Link]) -> list[str]:
        """The bottom panel: a spacer plus three lines of overview or selection."""
        if self._selected is None:
            rows = self._overview_rows(links)
        else:
            rows = self._selection_rows(self._selected)
        rows = (rows + [Text()] * 3)[:3]
        return [""] + [render_to_ansi(t, width, no_wrap=True) for t in rows]

    def _overview_rows(self, links: list[Link]) -> list[Text]:
        """Legend, evidence tallies, and the graph's age span."""
        legend = Text()
        for glyph, color in (_SELF, _REPEATER, _NODE, _UNKNOWN):
            legend.append(glyph, style=color)
            legend.append(
                {"★": " you   ", "▲": " repeater   ", "●": " node   ", "○": " unknown"}[glyph],
                style="muted",
            )
        legend.append("  ·  edge colour = SNR · faint = stale", style="muted")

        counts = {
            src: sum(1 for l in links if src in l.sources)
            for src in ("trace", "route", "packet", "neighbour")
        }
        evidence = Text("evidence  ", style="muted")
        parts = [f"{src} {n}" for src, n in counts.items() if n]
        evidence.append(" · ".join(parts) if parts else "—", style="brand")
        evidence.append("   (links seen by each source)", style="muted")

        stamps = [l.last_seen for l in links if l.last_seen is not None]
        span = Text("", style="muted")
        if stamps:
            newest = max(0.0, (utcnow() - max(stamps)).total_seconds())
            oldest = max(0.0, (utcnow() - min(stamps)).total_seconds())
            span.append("freshest ", style="muted")
            span.append(_format_age(newest), style="brand")
            span.append(" · oldest ", style="muted")
            span.append(_format_age(oldest), style="brand")
            span.append("  ·  ", style="muted")
        span.append("←/→ walk the graph", style="muted")
        return [legend, evidence, span]

    def _selection_rows(self, node: str) -> list[Text]:
        """Who the selection is, its links strongest-first, and its evidence roll-up."""
        glyph, color = self._glyph(node)
        contact = self._contacts.get(node)

        who = Text()
        who.append(glyph, style=color)
        who.append(f" {self._label(node)} ", style="bold")
        who.append(f"({node[:12]})", style="muted")
        if node == self._topo.self_id:
            who.append("  ·  this device", style="muted")
        else:
            kind = (
                NODE_TYPE_LABELS.get(contact.node_type, "node")
                if contact is not None
                else "unknown node"
            )
            who.append(f"  ·  {kind}", style="muted")
            if node in self._islands:
                who.append("  ·  island — no observed path to us", style="warn")
            else:
                ring = self._rings.get(node, 0)
                who.append(f"  ·  {ring} hop{'s' if ring != 1 else ''} out", style="muted")
            if contact is not None and contact.last_seen is not None:
                secs = max(0.0, (utcnow() - contact.last_seen).total_seconds())
                who.append(f"  ·  heard {_format_age(secs)}", style="muted")

        now = utcnow()
        neighbours = sorted(
            (
                (link.strength(now), link.b if link.a == node else link.a, link)
                for link in self._topo.links()
                if node in (link.a, link.b)
            ),
            key=lambda t: -t[0],
        )
        row = Text("links ", style="muted")
        row.append(str(len(neighbours)), style="brand")
        row.append("  strongest first:  ", style="muted")
        for i, (_s, other, link) in enumerate(neighbours):
            if i:
                row.append("  ·  ", style="muted")
            row.append(self._label(other))
            snr = link.median_snr
            if snr is not None:
                row.append(f" {snr:+.1f}", style=snr_style(snr))
            else:
                row.append(" —", style="muted")

        samples = sum(link.samples for _s, _o, link in neighbours)
        sources = sorted({src for _s, _o, link in neighbours for src in link.sources})
        stamps = [link.last_seen for _s, _o, link in neighbours if link.last_seen]
        rollup = Text("evidence  ", style="muted")
        rollup.append(f"{samples} reading{'s' if samples != 1 else ''}", style="brand")
        if sources:
            rollup.append(f"  ·  {', '.join(sources)}", style="muted")
        if stamps:
            newest = max(0.0, (utcnow() - max(stamps)).total_seconds())
            rollup.append("  ·  freshest ", style="muted")
            rollup.append(_format_age(newest), style="brand")
        rollup.append("  ·  Enter for details", style="muted")
        return [who, row, rollup]

    def _details_text(self, node: str) -> Text:
        """The Enter dialog: every link off ``node``, one line each, strongest first."""
        now = utcnow()
        neighbours = sorted(
            (
                (link.strength(now), link.b if link.a == node else link.a, link)
                for link in self._topo.links()
                if node in (link.a, link.b)
            ),
            key=lambda t: -t[0],
        )
        if not neighbours:
            return Text("No observed links.", style="muted")
        name_w = min(
            _LABEL_W, max(len(self._label(other)) for _s, other, _l in neighbours)
        )
        body = Text()
        for i, (_s, other, link) in enumerate(neighbours):
            if i:
                body.append("\n")
            label = self._label(other)
            if len(label) > name_w:
                label = label[: name_w - 1] + "…"
            body.append(label.ljust(name_w + 2))
            snr = link.median_snr
            if snr is not None:
                body.append(f"{snr:+5.1f} dB", style=snr_style(snr))
            else:
                body.append("   — dB", style="muted")
            body.append(f"  {link.samples:>3}×", style="brand")
            body.append(f"  {'+'.join(sorted(link.sources))}", style="muted")
            if link.last_seen is not None:
                secs = max(0.0, (now - link.last_seen).total_seconds())
                body.append(f"  {_format_age(secs)}", style="muted")
        return body


async def open_atlas(ctx: "AppContext") -> None:
    """Build the evidence graph and run the full-screen atlas until dismissed.

    Contacts and our own identity come from the device when one is reachable
    (best-effort — the stored evidence draws fine without them, just with hashes for
    names), the graph itself comes entirely from the repository, and the ``r`` key
    re-reads storage so evidence landing while the screen is open can be pulled in.
    No transmissions, ever.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services.topology import build_topology
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the atlas is only available in the menu")
    session = ctx.ui.session

    contacts: list[Contact] = []
    self_label = "you"
    self_hash: Optional[str] = None
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            device = await ctx.device()
            contacts = await device.get_contacts()
            info = await device.get_self_info()
            self_label = str(info.get("name") or "you")
            self_hash = str(info.get("public_key") or "") or None
    except Exception:  # noqa: BLE001 - names are a nicety; the graph renders without them
        contacts = []

    def build() -> MeshTopology:
        """One fresh graph from everything currently stored."""
        return build_topology(
            self_id=self_hash or "local",
            contacts=contacts,
            trace_paths=ctx.repo.trace_paths(),
            packet_paths=ctx.repo.packet_paths(),
            neighbour_links=ctx.repo.neighbour_links(),
        )

    topo = build()
    by_id: dict[str, Contact] = {}
    for contact in contacts:
        canonical = topo.canonical(contact.public_key or contact.key_prefix)
        if canonical:
            by_id[canonical] = contact

    screen = AtlasScreen(
        session=session,
        topo=topo,
        contacts=by_id,
        self_label=self_label,
        rebuild=build,
    )
    try:
        await session.run_screen(screen)
    finally:
        # The same clean-slate repaint as the map: braille fallback glyphs may have
        # smeared cells prompt_toolkit's differential paint will never rewrite.
        session.request_full_repaint()
