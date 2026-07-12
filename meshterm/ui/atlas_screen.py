"""The Mesh atlas: walk the mesh's observed shape one node at a time.

The interactive face of the ``atlas`` tool. The trace path composer already distills
every fragment of topology we ever received — trace walks, firmware routes, RX-logged
relay chains, repeater neighbour tables — into one evidence graph
(:mod:`~meshterm.services.topology`); this screen is that graph made explorable. Nothing
here transmits: the atlas is a reading of what the radio has already heard.

Rather than plotting the whole mesh at once (which reads as a hairball the moment the
graph grows), the atlas keeps one node *in focus* — our own, to begin with — and shows
only its immediate neighbourhood:

* the **canvas** draws the focus at the centre with its direct neighbours fanned around
  it, edges as braille lines coloured by the link's median SNR (green → amber → red,
  slate for links with no reading) and faded by evidence age. The node the trail came
  from is anchored to the **west**, so walking always reads as moving right and backing
  up as moving left. A dozen markers at most — never a tangle.
* the **link list** beneath repeats those neighbours as selectable rows, strongest
  observed link first: type glyph, name, hash, SNR with a quality bar, the evidence
  behind the link (samples, sources, age), and how many links continue onward from
  that node. The highlighted row's marker and label light white on the canvas.

**Enter walks**: the highlighted neighbour becomes the new focus, the breadcrumb trail
across the top grows (``you › YUL-Cartierville › …``), and **⌫ steps back** along it.
**Home** refocuses our own node. **Typing finds** — a global filter over every node in
the graph, islands included; Enter teleports the focus to the highlighted match (the
trail restarts there, since the walk didn't cross the gap). ``^R`` rebuilds the graph
from storage, and Esc peels find first, the screen second. PgUp/PgDn scroll when a hub
node's list outgrows the viewport.
"""

from __future__ import annotations

import math
from collections import deque
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
from .menus import fit_cells
from .theme import name_style, snr_style
from .trace_screen import snr_bar
from .tui.render import render_to_ansi
from .tui.screen import Screen
from .widgets import _format_age, highlighted_hash

if TYPE_CHECKING:
    from ..context import AppContext

#: Horizontal / vertical dot-space margins the neighbour fan keeps clear of the canvas
#: edge, so an outermost marker and its label have room to land.
_PAD_X_DOTS = 18
_PAD_Y_DOTS = 6

#: The canvas's height in character rows: enough to read the fan's shape, never so
#: tall it starves the link list (the body scrolls, but the list should open visible).
_CANVAS_MIN_H = 6
_CANVAS_MAX_H = 12

#: The widest a node label may render on the canvas before it is ellipsized.
_LABEL_W = 16

#: How many canvas labels are offered beyond the always-on ones (focus, selection,
#: trail-back). A busy hub keeps its markers but drops the excess labels — the list
#: below names every row anyway.
_LABEL_BUDGET = 8

#: The fan's angular reach on each side of due east, in radians. The west wedge is
#: reserved for the trail-back node, so the fan never overprints it.
_FAN_HALF_ANGLE = math.radians(130)

#: How many find matches the list shows at most (the filter narrows it fast).
_MAX_MATCHES = 10

#: Display cells the neighbour list's name lane spans (longer names ellipsize).
_LIST_NAME_W = 16

#: Display cells the hash lane spans (canonical ids are 12 hex; six with an ellipsis
#: keeps rows inside 72 columns while the prefix stays recognisable).
_LIST_HASH_W = 6

#: SNR (dB) → edge colour anchors, interpolated linearly and clamped at the ends: the
#: red/amber/green of the app's snr styles, so the graph and the rows agree.
_SNR_STOPS: tuple[tuple[float, RGB], ...] = (
    (-15.0, (239, 68, 68)),
    (0.0, (250, 204, 21)),
    (10.0, (74, 222, 128)),
)

#: Edge colour for a link with no SNR reading at all (e.g. known only from a route).
_NO_READING: RGB = (100, 116, 139)

#: Marker style for a node no contact matches — heard of, never identified.
_UNKNOWN = ("○", "#94a3b8")

#: One-letter tags for the evidence classes backing a link, matching the path
#: composer's: T(race), R(oute), P(acket log), N(eighbour table).
_SOURCE_TAGS = {"trace": "T", "route": "R", "packet": "P", "neighbour": "N"}


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
    """The full-screen atlas walker: a focus neighbourhood canvas over a link list."""

    floating = False

    def __init__(
        self,
        *,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        topo: MeshTopology,
        contacts: dict[str, Contact],
        self_label: str,
        prefix_bytes: int = 0,
        rebuild: Optional[Callable[[], MeshTopology]] = None,
    ) -> None:
        """Create the atlas over a built topology snapshot.

        Args:
            session: The running TUI session (repaints).
            topo: The evidence graph to walk.
            contacts: Contacts keyed by canonical id, for glyphs, names, and ages.
            self_label: Display name for our own node (its mesh name when known).
            prefix_bytes: Path-hash width to light in the hash lane (0 = none).
            rebuild: Rebuilds the graph from storage for the ``^R`` key; ``None``
                leaves the snapshot fixed (tests, and the odd caller without a repo).
        """
        super().__init__()
        self._session = session
        self._topo = topo
        self._contacts = contacts
        self._self_label = self_label
        self._prefix_bytes = prefix_bytes
        self._rebuild = rebuild
        #: The walked trail of canonical ids; the focus is its last entry. Walking
        #: appends, ⌫ pops, Home resets to us, a find teleport restarts it.
        self._trail: list[str] = [topo.self_id]
        #: Index of the highlighted row in the current list (neighbours or matches).
        self._index = 0
        #: The live find-as-you-type filter ("" = off; matches every node known).
        self._filter = ""
        self._needs_scrub = True  # braille smear scrub, exactly like the map
        self._cursor: Optional[int] = None

    # --- state -------------------------------------------------------------------

    @property
    def _focus(self) -> str:
        """The node currently in focus (the trail's last step)."""
        return self._trail[-1]

    @property
    def _came_from(self) -> Optional[str]:
        """The node the trail arrived from, or ``None`` at the trail's start."""
        return self._trail[-2] if len(self._trail) > 1 else None

    def _links_of(self, node: str) -> list[tuple[str, Link]]:
        """``(other, link)`` for every link off ``node``, strongest evidence first."""
        now = utcnow()
        pairs = [
            (link.b if link.a == node else link.a, link)
            for link in self._topo.links()
            if node in (link.a, link.b)
        ]
        pairs.sort(key=lambda pair: -pair[1].strength(now))
        return pairs

    def _all_nodes(self) -> set[str]:
        """Every node the graph mentions, plus us (walkable even when alone)."""
        nodes = {self._topo.self_id}
        for link in self._topo.links():
            nodes.add(link.a)
            nodes.add(link.b)
        return nodes

    def _hops_out(self) -> dict[str, int]:
        """BFS hop distance from our own node over the evidence links.

        Nodes with no path to us are absent — they are the islands, flagged as such
        wherever a distance would otherwise show.
        """
        adjacency: dict[str, set[str]] = {}
        for link in self._topo.links():
            adjacency.setdefault(link.a, set()).add(link.b)
            adjacency.setdefault(link.b, set()).add(link.a)
        depths = {self._topo.self_id: 0}
        queue: deque[str] = deque([self._topo.self_id])
        while queue:
            node = queue.popleft()
            for neighbour in adjacency.get(node, ()):
                if neighbour not in depths:
                    depths[neighbour] = depths[node] + 1
                    queue.append(neighbour)
        return depths

    def _matches(self) -> list[str]:
        """Nodes the find filter matches: nearest first, then by display name."""
        needle = self._filter.casefold()
        if not needle:
            return []
        depths = self._hops_out()
        candidates = [
            node for node in self._all_nodes()
            if needle in self._label(node).casefold() or needle in node.casefold()
        ]
        candidates.sort(key=lambda n: (depths.get(n, 999), self._label(n).casefold()))
        return candidates[:_MAX_MATCHES]

    def _rows(self) -> list[str]:
        """The selectable node ids the list currently shows (matches, or neighbours)."""
        if self._filter:
            return self._matches()
        return [other for other, _link in self._links_of(self._focus)]

    # --- input -------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Walking keys — or the live find query while one is being typed."""
        if self._filter:
            return f"find: {self._filter}▏ · ↑↓ move · Enter focus · ⌫ erase · Esc clear"
        return "↑↓ move · Enter focus · ⌫ back · Home you · type to find · Esc back"

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, walk, back up, find, rebuild, or dismiss."""
        rows = self._rows()
        if action == "escape":
            if self._filter:
                self._filter = ""
                self._index = 0
            else:
                self.resolve(None)
                return
        elif action == "up" and rows:
            self._index = (self._index - 1) % len(rows)
        elif action == "down" and rows:
            self._index = (self._index + 1) % len(rows)
        elif action == "pageup" and rows:
            # The highlight pages (the dashboard feed's behaviour): a row is always
            # selected here, and the cursor pin keeps it in view, so paging the view
            # without the highlight would just snap straight back.
            self._index = max(0, self._index - self._page_step)
        elif action == "pagedown" and rows:
            self._index = min(len(rows) - 1, self._index + self._page_step)
        elif action == "enter":
            self._walk(rows)
        elif action == "backspace":
            if self._filter:
                self._filter = self._filter[:-1]
                self._index = 0
            elif len(self._trail) > 1:
                self._trail.pop()
                self._index = 0
        elif action in ("home", "ctrl_home"):
            self._trail = [self._topo.self_id]
            self._filter = ""
            self._index = 0
        elif action == "text":
            self._filter += data
            self._index = 0
        elif action == "space" and self._filter:
            self._filter += " "  # node names carry spaces; only meaningful mid-query
        elif action == "retry":  # ^R
            self._do_rebuild()
        self._needs_scrub = True
        self._session.invalidate()

    def _walk(self, rows: list[str]) -> None:
        """Focus the highlighted row: a step along the trail, or a find teleport."""
        if not rows:
            return
        target = rows[min(self._index, len(rows) - 1)]
        if self._filter:
            # A teleport restarts the trail at the target — the walk didn't cross the
            # gap, so pretending it did would make ⌫ retrace a path never taken.
            self._trail = [target]
            self._filter = ""
        elif target == self._came_from:
            self._trail.pop()  # walking back through the west node = one step back
        else:
            self._trail.append(target)
        self._index = 0

    def _do_rebuild(self) -> None:
        """Re-read the evidence and keep the trail where it survives."""
        if self._rebuild is None:
            return
        self._topo = self._rebuild()
        known = self._all_nodes()
        kept = [node for node in self._trail if node in known]
        self._trail = kept or [self._topo.self_id]
        self._index = 0

    # --- smear scrub (same fallback-glyph problem as the map) ---------------------

    def consume_edge_scrub(self) -> int:
        """Right-edge columns to force-repaint after a redraw (see the map screen)."""
        if not self._needs_scrub:
            return 0
        self._needs_scrub = False
        return 2

    # --- rendering -----------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the trail, the focus line, the canvas, and the link list."""
        links = self._topo.links()
        rows = self._rows()
        self._index = max(0, min(self._index, len(rows) - 1)) if rows else 0
        self.title = self._compose_title(links)
        self._cursor = None
        if not links:
            return self._empty_state(width)

        depths = self._hops_out()
        selected = rows[self._index] if rows else None

        lines: list[str] = []
        lines.extend(self._header_lines(width, depths))
        lines.extend(self._canvas_lines(width, selected))
        lines.append(render_to_ansi(self._legend(), width, no_wrap=True))
        lines.append("")
        lines.extend(self._list_lines(width, rows, depths))
        self._scroll_total = max(1, len(lines))
        return lines

    def cursor_line(self) -> Optional[int]:
        """The highlighted list row, so the session keeps it in view."""
        return self._cursor

    def _compose_title(self, links: list[Link]) -> str:
        """``Mesh atlas — focus`` plus the graph's status atoms."""
        title = f"Mesh atlas — {self._label(self._focus)}"
        title += f" · {len(self._all_nodes())} nodes · {len(links)} links"
        if self._filter:
            matches = self._matches()
            title += f" · {len(matches)} match{'es' if len(matches) != 1 else ''}"
        return title

    def _header_lines(self, width: int, depths: dict[str, int]) -> list[str]:
        """The breadcrumb trail (when walking) and the focus node's identity line."""
        out: list[str] = []
        if len(self._trail) > 1:
            trail = Text()
            for i, node in enumerate(self._trail):
                if i:
                    trail.append(" › ", style="muted")
                last = i == len(self._trail) - 1
                trail.append(
                    self._label(node), style="bold" if last else "muted"
                )
            trail.truncate(width, overflow="ellipsis")
            out.append(render_to_ansi(trail, width, no_wrap=True))
        out.append(render_to_ansi(self._focus_line(depths), width, no_wrap=True))
        return out

    def _focus_line(self, depths: dict[str, int]) -> Text:
        """Who is in focus: glyph, name, hash, kind, distance, and recency."""
        node = self._focus
        glyph, color = self._glyph(node)
        contact = self._contacts.get(node)
        line = Text()
        line.append(glyph, style=color)
        line.append(" ")
        line.append(self._label(node), style=self._list_name_style(node))
        line.append("  ")
        line.append_text(highlighted_hash(node, self._prefix_bytes))
        if node == self._topo.self_id:
            line.append("  ·  this device", style="muted")
        else:
            kind = (
                NODE_TYPE_LABELS.get(contact.node_type, "node")
                if contact is not None
                else "unknown node"
            )
            line.append(f"  ·  {kind}", style="muted")
            if node not in depths:
                line.append("  ·  island — no observed path to you", style="warn")
            else:
                ring = depths[node]
                line.append(f"  ·  {ring} hop{'s' if ring != 1 else ''} out", style="muted")
            if contact is not None and contact.last_seen is not None:
                secs = max(0.0, (utcnow() - contact.last_seen).total_seconds())
                line.append(f"  ·  heard {_format_age(secs)}", style="muted")
        return line

    # -- the canvas --

    def _canvas_lines(self, width: int, selected: Optional[str]) -> list[str]:
        """Draw the focus neighbourhood: centre marker, west trail-back, eastern fan."""
        _, cell_h = self._session.base_body_size()
        # Sized to the neighbourhood: a two-node link needs no twelve-row void, while
        # a hub earns the full fan height — always capped so the list opens visible.
        crowd = len(self._links_of(self._focus))
        canvas_h = max(_CANVAS_MIN_H, min(_CANVAS_MAX_H, cell_h - 10, 5 + crowd))
        canvas = MapCanvas(width, canvas_h)
        dot_w, dot_h = width * 2, canvas_h * 4
        cx, cy = dot_w // 2, dot_h // 2
        rx = max(10.0, dot_w / 2.0 - _PAD_X_DOTS)
        ry = max(6.0, dot_h / 2.0 - _PAD_Y_DOTS)

        now = utcnow()
        placed = self._place_neighbours(cx, cy, rx, ry)

        # Edges first (markers and labels overprint them), coloured by SNR and faded
        # by evidence age; the highlighted neighbour's edge wins its cells.
        by_other = {other: link for other, link in self._links_of(self._focus)}
        for other, (x, y) in placed.items():
            link = by_other.get(other)
            if link is None:  # pragma: no cover - placed comes from the same list
                continue
            color = _scaled(_snr_rgb(link.median_snr), _freshness(link.last_seen, now))
            priority = 3 if other == selected else 2
            canvas.draw_line([(cx, cy), (x, y)], color, priority)

        # The focus marker and its label, always on and always white-labelled.
        glyph, color_hex = self._glyph(self._focus)
        canvas.marker(cx, cy, glyph, parse_hex(color_hex))
        self._place_label(canvas, cx, cy, self._label(self._focus), (255, 255, 255))

        # Neighbour markers, then labels under a budget (selection and trail-back
        # always labelled; the rest strongest-first until the budget runs out).
        for other, (x, y) in placed.items():
            glyph, color_hex = self._glyph(other)
            rgb = (255, 255, 255) if other == selected else parse_hex(color_hex)
            canvas.marker(x, y, glyph, rgb)
        budget = _LABEL_BUDGET
        for other, (x, y) in placed.items():
            always = other in (selected, self._came_from)
            if not always:
                if budget <= 0:
                    continue
                budget -= 1
            rgb = (255, 255, 255) if other == selected else parse_hex(self._glyph(other)[1])
            self._place_label(canvas, x, y, self._label(other), rgb)

        return canvas.to_ansi_lines()

    def _place_neighbours(
        self, cx: int, cy: int, rx: float, ry: float
    ) -> dict[str, tuple[int, int]]:
        """Dot-space positions for the focus's neighbours.

        The trail-back node (when among them) anchors due west; everyone else fans
        across the eastern arc, strongest link at the top, weakest at the bottom —
        the same order as the list below, so the picture and the rows correspond.
        """
        neighbours = [other for other, _link in self._links_of(self._focus)]
        placed: dict[str, tuple[int, int]] = {}
        back = self._came_from
        if back in neighbours:
            placed[back] = (round(cx - rx), cy)
            fan = [n for n in neighbours if n != back]
        else:
            fan = neighbours
        n = len(fan)
        for i, node in enumerate(fan):
            if n == 1:
                angle = 0.0
            else:
                angle = -_FAN_HALF_ANGLE + (2 * _FAN_HALF_ANGLE) * i / (n - 1)
            x = cx + rx * math.cos(angle)
            y = cy + ry * math.sin(angle)
            placed[node] = (round(x), round(y))
        return placed

    def _place_label(
        self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB
    ) -> None:
        """Place one marker label, retrying a row below then above on collision."""
        if len(label) > _LABEL_W:
            label = label[: _LABEL_W - 1] + "…"
        for dy in (0, 4, -4):
            if canvas.marker_label(x, y + dy, label, rgb):
                return

    def _legend(self) -> Text:
        """The one-line glyph legend and edge key under the canvas."""
        legend = Text()
        for glyph, color in (_SELF, _REPEATER, _NODE, _UNKNOWN):
            legend.append(glyph, style=color)
            legend.append(
                {"★": " you   ", "▲": " repeater   ", "●": " node   ", "○": " unknown"}[glyph],
                style="muted",
            )
        legend.append("  ·  edge = SNR · faint = stale", style="muted")
        return legend

    # -- the list --

    def _list_lines(
        self, width: int, rows: list[str], depths: dict[str, int]
    ) -> list[str]:
        """The selectable rows: find matches, or the focus's links strongest-first."""
        out: list[str] = []
        if self._filter:
            heading = Text("Matches", style="accent")
            heading.append("  ·  nearest first · Enter focuses", style="muted")
            out.append(render_to_ansi(heading, width, no_wrap=True))
            if not rows:
                out.append(render_to_ansi(Text("no matches", style="muted"), width))
            for i, node in enumerate(rows):
                text = self._match_row(node, i == self._index, depths)
                if i == self._index:
                    self._cursor = len(out)
                out.append(render_to_ansi(text, width, no_wrap=True))
            return out

        heading = Text("Links", style="accent")
        heading.append("  ·  strongest observed first · Enter walks", style="muted")
        out.append(render_to_ansi(heading, width, no_wrap=True))
        pairs = self._links_of(self._focus)
        if not pairs:
            note = Text("no observed links from here — type to find another node", style="muted")
            out.append(render_to_ansi(note, width))
        onward = self._onward_counts(pairs)
        for i, (other, link) in enumerate(pairs):
            text = self._link_row(other, link, i == self._index, onward.get(other, 0))
            if i == self._index:
                self._cursor = len(out)
            out.append(render_to_ansi(text, width, no_wrap=True))
        return out

    def _onward_counts(self, pairs: list[tuple[str, Link]]) -> dict[str, int]:
        """How many links continue from each neighbour, the one back here excluded."""
        counts: dict[str, int] = {}
        for other, _link in pairs:
            counts[other] = sum(
                1
                for link in self._topo.links()
                if other in (link.a, link.b) and self._focus not in (link.a, link.b)
            )
        return counts

    def _link_row(self, other: str, link: Link, selected: bool, onward: int) -> Text:
        """One neighbour row: glyph, name, hash, SNR + bar, evidence, onward count."""
        glyph, glyph_style = self._glyph(other)
        row = Text()
        row.append("❯ " if selected else "  ", style="brand" if selected else "")
        row.append(glyph, style=glyph_style)
        row.append(" ")
        name_style_ = self._list_name_style(other)
        row.append(fit_cells(self._label(other), _LIST_NAME_W), style=name_style_)
        row.append(" ")
        row.append_text(highlighted_hash(other, self._prefix_bytes, width=_LIST_HASH_W))
        row.append("  ")
        snr = link.median_snr
        if snr is not None:
            row.append(f"{snr:+5.1f}", style=snr_style(snr))
        else:
            row.append("    —", style="muted")
        row.append(" ")
        row.append_text(snr_bar(snr, width=4))
        row.append(f" {min(link.samples, 999):>3}×", style="muted")
        tags = "".join(_SOURCE_TAGS[s] for s in sorted(link.sources & _SOURCE_TAGS.keys()))
        row.append(f" {tags:<4}", style="faint")
        age = _format_age(
            max(0.0, (utcnow() - link.last_seen).total_seconds())
            if link.last_seen is not None and getattr(link.last_seen, "tzinfo", None)
            else None
        )
        row.append(f"{age:>5}", style="muted")
        if other == self._came_from:
            row.append("  ⌫ back", style="faint")
        elif onward:
            row.append(f"  ⋯ {onward}", style="faint")
        if selected:
            row.style = "brand"
        return row

    def _match_row(self, node: str, selected: bool, depths: dict[str, int]) -> Text:
        """One find match: glyph, name, hash, and how far out it sits."""
        glyph, glyph_style = self._glyph(node)
        row = Text()
        row.append("❯ " if selected else "  ", style="brand" if selected else "")
        row.append(glyph, style=glyph_style)
        row.append(" ")
        row.append(fit_cells(self._label(node), _LIST_NAME_W), style=self._list_name_style(node))
        row.append(" ")
        row.append_text(highlighted_hash(node, self._prefix_bytes, width=_LIST_HASH_W))
        row.append("  ")
        if node == self._topo.self_id:
            row.append("this device", style="muted")
        elif node not in depths:
            row.append("island", style="warn")
        else:
            ring = depths[node]
            row.append(f"{ring} hop{'s' if ring != 1 else ''} out", style="muted")
        if selected:
            row.style = "brand"
        return row

    def _list_name_style(self, node: str) -> str:
        """The list's name colour: the app-wide palette hue, us in pure white."""
        if node == self._topo.self_id:
            return "you"
        label = self._label(node)
        if label == node[:8]:  # a bare hash is not a name — colour is the name signal
            return "muted"
        return name_style(label)

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


async def open_atlas(ctx: "AppContext") -> None:
    """Build the evidence graph and run the full-screen atlas until dismissed.

    Contacts and our own identity come from the device when one is reachable
    (best-effort — the stored evidence draws fine without them, just with hashes for
    names), the graph itself comes entirely from the repository, and ``^R`` re-reads
    storage so evidence landing while the screen is open can be pulled in. No
    transmissions, ever.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services.topology import build_topology
    from .surface import TuiUi
    from .timemachine_screen import _routing_prefix_bytes

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
    prefix_bytes = await _routing_prefix_bytes(ctx)

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
        prefix_bytes=prefix_bytes,
        rebuild=build,
    )
    try:
        await session.run_screen(screen)
    finally:
        # The same clean-slate repaint as the map: braille fallback glyphs may have
        # smeared cells prompt_toolkit's differential paint will never rewrite.
        session.request_full_repaint()
