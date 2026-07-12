"""The Mesh Atlas: the mesh's observed shape, drawn as an explorable braille graph.

The interactive face of the ``atlas`` tool. The trace path composer already distills
every fragment of topology we ever received — trace walks, firmware routes, RX-logged
relay chains, repeater neighbour tables — into one evidence graph
(:mod:`~meshterm.services.topology`); this screen is that graph made visible. Nothing
here transmits: the atlas is a reading of what the radio has already heard.

Our own node anchors the centre and every other node sits on a concentric ring at its
evidenced hop distance — the rings are drawn as faint dotted guides, so the picture
answers *how far out is everything?* like a radar scope. Angles come from the
:mod:`~meshterm.services.atlas_layout` relaxation, so linked nodes gather into wedges
instead of piling onto one line. Edges are braille lines coloured by the link's median
SNR (green → amber → red, slate for links with no reading) and faded by evidence age.

The camera is the map's: **arrows pan** (Shift for fine), **PgUp/PgDn zoom**, and
**Home** resets to the full fit — so a tangle that reads as noise at 1× opens right up
at 4×, with the label budget growing as the zoom does. **Typing finds nodes** (matches
keep bright labels, everything else dims; ``Enter`` selects the first match), **Tab**
walks the graph selection node by node (recentring when the selection is off-screen),
``Enter`` floats the selected node's per-link details, ``Ctrl+R`` rebuilds from
storage, and ``Esc`` peels find → selection → screen. The bottom panel reads the
overview, the selection, or the find state, whichever is active.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from rich.text import Text

from ..core.models import (
    NODE_TYPE_LABELS,
    NODE_TYPE_REPEATER,
    Contact,
    utcnow,
)
from ..services.atlas_layout import AtlasLayout, compute_layout
from ..services.topology import Link, MeshTopology
from .map_render import _NODE, _REPEATER, _SELF
from .mapcanvas import RGB, MapCanvas, parse_hex
from .theme import snr_style
from .tui.render import render_to_ansi
from .tui.screen import Screen
from .widgets import _format_age

if TYPE_CHECKING:
    from ..context import AppContext

#: Horizontal / vertical dot-space margins the 1× fit keeps clear of the canvas edge,
#: so an outermost node's marker and label have room to land.
_PAD_X_DOTS = 16
_PAD_Y_DOTS = 5

#: Bottom lines reserved under the canvas for the info panel (a spacer + three rows).
_PANEL_H = 4

#: The widest a node label may render on the canvas before it is ellipsized.
_LABEL_W = 16

#: Zoom bounds and the factor one PgUp/PgDn step multiplies by. 1.0 is the full fit;
#: the ceiling is deep enough to open up any realistic wedge of nodes.
_ZOOM_MIN = 1.0
_ZOOM_MAX = 12.0
_ZOOM_STEP = 1.5

#: Fraction of the visible span one coarse pan keypress moves the camera.
_PAN_STEP = 0.30

#: How many labels the canvas offers at 1×; the budget grows with the zoom's square
#: (zooming in *creates* the space the labels need). Selection, find matches, and our
#: own node are always offered regardless.
_LABEL_BUDGET_BASE = 6

#: Ring-guide dot colour — dark slate, beneath every edge — and the dot-space gap
#: between its dots (sparse enough to read as a guide, not a feature).
_RING_RGB: RGB = (52, 61, 78)
_RING_DOT_GAP = 4.0

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

#: Label colour for find-filter matches: full white, the brightest thing drawn.
_MATCH_LABEL: RGB = (255, 255, 255)


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


class AtlasScreen(Screen):
    """The full-screen atlas: pannable/zoomable canvas, a live info panel beneath."""

    floating = False

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
            rebuild: Rebuilds the graph from storage for the ``Ctrl+R`` key; ``None``
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
        #: Tab cycle: us first, then rings inside-out, each ring by angle.
        self._cycle: list[str] = []
        #: The camera: zoom (1.0 = the whole graph fits) and its unit-space centre.
        self._zoom = _ZOOM_MIN
        self._cam = (0.0, 0.0)
        #: The live find-as-you-type node filter ("" = off).
        self._filter = ""
        #: A node to bring on-screen at the next paint (set by Tab / find-select).
        self._reveal: Optional[str] = None
        self._layout: Optional[AtlasLayout] = None
        self._layout_generation: Optional[int] = None
        self._generation = 0  # bumped by rebuild so the layout cache invalidates
        self._dot_size = (2, 2)  # dot-space canvas size, recorded each render
        self._dialog_open = False
        self._needs_scrub = True  # braille smear scrub, exactly like the map

    # --- input -----------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Camera and find hints; the find query takes the row over while active."""
        if self._filter:
            return f"find: {self._filter}▏ · Enter select · Bksp erase · Esc clear"
        return "↑↓←→ pan · PgUp/PgDn zoom · Tab walk · Enter info · type to find · Esc"

    def handle(self, action: str, data: str = "") -> None:
        """Pan, zoom, walk, find, rebuild, or dismiss.

        Every printable key feeds the find filter — no letters are bound to actions —
        and Esc peels one layer at a time: the filter, then the selection, then the
        screen itself.
        """
        if action == "escape":
            if self._filter:
                self._filter = ""
            elif self._selected is not None:
                self._selected = None
            else:
                self.resolve(None)
                return
        elif action in ("up", "down", "left", "right"):
            self._pan(action, fine=False)
        elif action.startswith("shift_") and action[len("shift_"):] in (
            "up", "down", "left", "right",
        ):
            self._pan(action[len("shift_"):], fine=True)
        elif action == "pageup":
            self._set_zoom(self._zoom * _ZOOM_STEP)
        elif action == "pagedown":
            self._set_zoom(self._zoom / _ZOOM_STEP)
        elif action in ("home", "ctrl_home"):
            self._zoom = _ZOOM_MIN
            self._cam = (0.0, 0.0)
        elif action == "tab":
            self._step_selection(1)
        elif action == "enter":
            self._commit_enter()
        elif action == "text":
            self._filter += data
        elif action == "space" and self._filter:
            self._filter += " "  # node names carry spaces; only meaningful mid-query
        elif action == "backspace":
            self._filter = self._filter[:-1]
        elif action == "retry":  # Ctrl+R
            self._do_rebuild()
        self._needs_scrub = True
        self._session.invalidate()

    def _pan(self, direction: str, *, fine: bool) -> None:
        """Move the camera one step (or one character cell, when ``fine``).

        The step is a fraction of the *visible* unit span, so panning covers the same
        on-screen distance at every zoom; the camera is then clamped so the graph can
        never be lost off-screen (at 1× it snaps back to centre).
        """
        dx = {"left": -1, "right": 1}.get(direction, 0)
        dy = {"up": -1, "down": 1}.get(direction, 0)
        span = 2.0 / self._zoom  # the visible extent of unit space, edge to edge
        step = span * (_PAN_STEP / 8 if fine else _PAN_STEP)
        self._cam = (self._cam[0] + dx * step, self._cam[1] + dy * step)
        self._clamp_cam()

    def _set_zoom(self, zoom: float) -> None:
        """Clamp and apply a zoom level, re-clamping the camera to the new range."""
        self._zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, zoom))
        self._clamp_cam()

    def _clamp_cam(self) -> None:
        """Keep the camera within the graph: the pan range grows as the zoom does.

        At 1× the whole graph is on screen and the only sensible centre is the
        origin; each zoom step frees ``1 - 1/zoom`` of unit space per side (plus a
        small overshoot so an edge node can be centred).
        """
        limit = max(0.0, 1.0 - 1.0 / self._zoom) * 1.1
        self._cam = (
            max(-limit, min(limit, self._cam[0])),
            max(-limit, min(limit, self._cam[1])),
        )

    def _step_selection(self, delta: int) -> None:
        """Cycle ``… → overview → us → ring by ring …`` and reveal the new selection."""
        if not self._cycle:
            return
        stops: list[Optional[str]] = [None, *self._cycle]
        at = stops.index(self._selected) if self._selected in stops else 0
        self._selected = stops[(at + delta) % len(stops)]
        self._reveal = self._selected

    def _matches(self) -> list[str]:
        """Nodes the live find filter matches, in the Tab cycle's stable order."""
        needle = self._filter.casefold()
        if not needle:
            return []
        return [n for n in self._cycle if needle in self._label(n).casefold()]

    def _commit_enter(self) -> None:
        """Enter: adopt the find's first match, or open the selection's details."""
        if self._filter:
            matches = self._matches()
            if matches:
                self._selected = matches[0]
                self._reveal = matches[0]
                self._filter = ""
            return
        if self._selected is not None:
            self._open_details()

    def _do_rebuild(self) -> None:
        """Re-read the evidence and relayout, keeping the selection when it survives."""
        if self._rebuild is None:
            return
        self._topo = self._rebuild()
        self._generation += 1  # the next paint recomputes the layout

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

    # --- projection --------------------------------------------------------------------

    def _project(self, unit: tuple[float, float]) -> tuple[int, int]:
        """Unit-space → dot-space through the current camera (aspect, zoom, pan)."""
        dot_w, dot_h = self._dot_size
        rx = max(8.0, dot_w / 2.0 - _PAD_X_DOTS)
        ry = max(6.0, dot_h / 2.0 - _PAD_Y_DOTS)
        x = dot_w / 2.0 + (unit[0] - self._cam[0]) * rx * self._zoom
        y = dot_h / 2.0 + (unit[1] - self._cam[1]) * ry * self._zoom
        return round(x), round(y)

    def _reveal_if_offscreen(self, node: str) -> None:
        """Centre the camera on ``node`` if it currently projects off the canvas."""
        layout = self._layout
        if layout is None or node not in layout.positions:
            return
        dot_w, dot_h = self._dot_size
        x, y = self._project(layout.positions[node])
        if 0 <= x < dot_w and 0 <= y < dot_h:
            return
        self._cam = layout.positions[node]
        self._clamp_cam()

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Draw the ring guides, edges, markers, and labels, then the info panel."""
        _, cell_h = self._session.base_body_size()
        canvas_h = max(4, cell_h - _PANEL_H)
        self._dot_size = (width * 2, canvas_h * 4)

        links = self._topo.links()
        matches = self._matches()
        self.title = self._compose_title(links, matches)
        if not links:
            return self._empty_state(width)

        self._ensure_layout(links)
        if self._reveal is not None:
            self._reveal_if_offscreen(self._reveal)
            self._reveal = None
        layout = self._layout
        assert layout is not None  # _ensure_layout just ran

        canvas = MapCanvas(width, canvas_h)
        now = utcnow()
        match_set = set(matches)

        self._draw_rings(canvas, layout)
        self._draw_edges(canvas, layout, links, now, match_set)
        self._draw_nodes(canvas, layout, match_set)

        return canvas.to_ansi_lines() + self._panel_lines(width, links, matches)

    def _compose_title(self, links: list[Link], matches: list[str]) -> str:
        """The status title: counts, the zoom level, and the find tally when active."""
        nodes = len(self._layout_nodes(links))
        title = f"Mesh atlas · {nodes} nodes · {len(links)} links"
        if self._zoom > _ZOOM_MIN:
            title += f" · {self._zoom:.1f}×"
        if self._filter:
            title += f" · {len(matches)} match{'es' if len(matches) != 1 else ''}"
        return title

    def _draw_rings(self, canvas: MapCanvas, layout: AtlasLayout) -> None:
        """Dot the hop-ring guides under everything: the atlas's radar-scope grid.

        Each ring is sampled at a spacing that lands one dot every
        :data:`_RING_DOT_GAP` dots of on-screen arc, so the guides stay sparse at 1×
        and don't thicken as the zoom multiplies their radius.
        """
        dot_w, dot_h = self._dot_size
        rx = max(8.0, dot_w / 2.0 - _PAD_X_DOTS) * self._zoom
        ry = max(6.0, dot_h / 2.0 - _PAD_Y_DOTS) * self._zoom
        for ring in range(1, layout.max_ring + 1):
            radius = ring / layout.max_ring
            arc = math.tau * radius * max(rx, ry)  # on-screen circumference, roughly
            samples = max(24, int(arc / _RING_DOT_GAP))
            for i in range(samples):
                angle = i / samples * math.tau
                x, y = self._project((radius * math.cos(angle), radius * math.sin(angle)))
                canvas.plot(x, y, _RING_RGB, 0)

    def _draw_edges(
        self,
        canvas: MapCanvas,
        layout: AtlasLayout,
        links: list[Link],
        now: datetime,
        match_set: set[str],
    ) -> None:
        """Draw every link, emphasis following the selection or the find filter."""
        for link in links:
            pa = layout.positions.get(link.a)
            pb = layout.positions.get(link.b)
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
            elif match_set and not (link.a in match_set or link.b in match_set):
                color = _scaled(color, 0.3)
                priority = 1
            canvas.draw_line([self._project(pa), self._project(pb)], color, priority)

    def _draw_nodes(
        self, canvas: MapCanvas, layout: AtlasLayout, match_set: set[str]
    ) -> None:
        """Place markers, then labels in importance order under the zoom's budget.

        Markers always draw (they are the point of the graph); labels are the scarce
        resource. The budget starts at :data:`_LABEL_BUDGET_BASE` for the 1× overview
        and grows with the square of the zoom — zooming in is what creates label room
        — while the selection, find matches, and our own node are always offered.
        The canvas's collision avoidance still gets the final say, so a label that
        doesn't fit cleanly is dropped rather than overprinting a neighbour.
        """
        order = self._marker_order(match_set)
        for node in order:
            x, y = self._project(layout.positions[node])
            glyph, color_hex = self._glyph(node)
            rgb = parse_hex(color_hex)
            if node == self._selected:
                rgb = (255, 255, 255)
            elif match_set and node not in match_set:
                rgb = _scaled(rgb, 0.35)
            canvas.marker(x, y, glyph, rgb)

        budget = int(_LABEL_BUDGET_BASE * self._zoom * self._zoom)
        placed = 0
        for node in order:
            always = (
                node == self._selected
                or node == self._topo.self_id
                or node in match_set
            )
            if not always and placed >= budget:
                continue
            if match_set and not always:
                continue  # while finding, non-matches stay as dim context glyphs
            x, y = self._project(layout.positions[node])
            label = self._label(node)
            if len(label) > _LABEL_W:
                label = label[: _LABEL_W - 1] + "…"
            if node in match_set:
                rgb = _MATCH_LABEL
            elif node == self._selected:
                rgb = (255, 255, 255)
            else:
                rgb = parse_hex(self._glyph(node)[1])
            # Both sides of a crowded marker can be claimed; retry a cell below,
            # then above, before giving the label up entirely.
            for dy in (0, 4, -4):
                if canvas.marker_label(x, y + dy, label, rgb):
                    placed += 1
                    break

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

    def _ensure_layout(self, links: list[Link]) -> None:
        """(Re)compute the unit-space layout when the evidence generation changed.

        The layout is camera-independent (zoom and pan happen at projection time),
        so moving the view never pays for a relayout — only fresh evidence does.
        """
        if self._layout is not None and self._layout_generation == self._generation:
            return
        now = utcnow()
        triples = [(l.a, l.b, l.strength(now)) for l in links]
        self._layout = compute_layout(self._topo.self_id, triples)
        self._layout_generation = self._generation
        angle = {
            node: math.atan2(y, x) % math.tau
            for node, (x, y) in self._layout.positions.items()
        }
        order = sorted(
            (n for n in self._layout.positions if n != self._topo.self_id),
            key=lambda n: (self._layout.rings.get(n, 99), angle[n]),
        )
        self._cycle = [self._topo.self_id, *order]
        if self._selected is not None and self._selected not in self._layout.positions:
            self._selected = None  # the rebuild dropped it

    def _marker_order(self, match_set: set[str]) -> list[str]:
        """Marker/label priority: selection, matches, us, repeaters, then outward."""
        layout = self._layout
        assert layout is not None

        def rank(node: str) -> tuple[int, int, str]:
            if node == self._selected:
                lead = 0
            elif node in match_set:
                lead = 1
            elif node == self._topo.self_id:
                lead = 2
            elif (
                self._contacts.get(node) is not None
                and self._contacts[node].node_type == NODE_TYPE_REPEATER
            ):
                lead = 3
            else:
                lead = 4
            return (lead, layout.rings.get(node, 99), node)

        return sorted(layout.positions, key=rank)

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

    def _panel_lines(self, width: int, links: list[Link], matches: list[str]) -> list[str]:
        """The bottom panel: a spacer plus three lines of find, selection, or overview."""
        if self._filter:
            rows = self._find_rows(matches)
        elif self._selected is not None:
            rows = self._selection_rows(self._selected)
        else:
            rows = self._overview_rows(links)
        rows = (rows + [Text()] * 3)[:3]
        return [""] + [render_to_ansi(t, width, no_wrap=True) for t in rows]

    def _find_rows(self, matches: list[str]) -> list[Text]:
        """The find state: the query's tally and who matched, brightest first."""
        tally = Text("find  ", style="muted")
        tally.append(self._filter, style="bold")
        tally.append(
            f"  ·  {len(matches)} of {max(0, len(self._cycle))} nodes match",
            style="muted",
        )
        names = Text("      ", style="muted")
        for i, node in enumerate(matches[:6]):
            if i:
                names.append("  ·  ", style="muted")
            names.append(self._label(node), style="bold")
        if len(matches) > 6:
            names.append(f"  ·  +{len(matches) - 6} more", style="muted")
        hint = Text("      Enter selects the first match", style="muted")
        return [tally, names if matches else Text("      no matches", style="muted"), hint]

    def _overview_rows(self, links: list[Link]) -> list[Text]:
        """Legend, evidence tallies, and the graph's age span."""
        # Legend text budgeted to a 72-column terminal (the panel rows never wrap).
        legend = Text()
        for glyph, color in (_SELF, _REPEATER, _NODE, _UNKNOWN):
            legend.append(glyph, style=color)
            legend.append(
                {"★": " you   ", "▲": " repeater   ", "●": " node   ", "○": " unknown"}[glyph],
                style="muted",
            )
        legend.append("  ·  edge = SNR · faint = stale", style="muted")

        counts = {
            src: sum(1 for l in links if src in l.sources)
            for src in ("trace", "route", "packet", "neighbour")
        }
        evidence = Text("evidence  ", style="muted")
        parts = [f"{src} {n}" for src, n in counts.items() if n]
        evidence.append(" · ".join(parts) if parts else "—", style="brand")
        evidence.append("   (links per source)", style="muted")

        stamps = [l.last_seen for l in links if l.last_seen is not None]
        span = Text("", style="muted")
        if stamps:
            newest = max(0.0, (utcnow() - max(stamps)).total_seconds())
            oldest = max(0.0, (utcnow() - min(stamps)).total_seconds())
            span.append("freshest ", style="muted")
            span.append(_format_age(newest), style="brand")
            span.append(" · oldest ", style="muted")
            span.append(_format_age(oldest), style="brand")
            span.append(" · ", style="muted")
        span.append("rings = hops out · Home reset · ^R rebuild", style="muted")
        return [legend, evidence, span]

    def _selection_rows(self, node: str) -> list[Text]:
        """Who the selection is, its links strongest-first, and its evidence roll-up."""
        glyph, color = self._glyph(node)
        contact = self._contacts.get(node)
        layout = self._layout

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
            if layout is not None and node in layout.islands:
                who.append("  ·  island — no observed path to us", style="warn")
            else:
                ring = layout.rings.get(node, 0) if layout is not None else 0
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
    names), the graph itself comes entirely from the repository, and ``Ctrl+R``
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
