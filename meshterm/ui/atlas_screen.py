"""The Mesh atlas: walk the mesh's observed shape one node at a time.

The interactive face of the ``atlas`` tool. The trace path composer already distills
every fragment of topology we ever received — trace walks, firmware routes, RX-logged
relay chains, repeater neighbour tables — into one evidence graph
(:mod:`~meshterm.services.topology`); this screen is that graph made explorable. Nothing
here transmits: the atlas is a reading of what the radio has already heard.

Rather than plotting the whole mesh at once (which reads as a hairball the moment the
graph grows), the atlas keeps one node *in focus* — our own, to begin with — and shows
only its immediate neighbourhood:

* the **canvas** — the majority of the screen, so the shape stays legible — draws the
  focus toward the left (its label to its left) with a deliberately sparse fan of its
  strongest neighbours to the right, each named just to the right of its marker, edges
  as braille lines coloured by the link's median SNR (green → amber → red, slate for
  links with no reading) and faded by evidence age. The node the trail came from is
  anchored at the far **west**, so walking always reads as moving right and backing up
  as moving left. Only as many neighbours as the canvas area can carry are drawn — a
  sparse fan reads far better than a crowded one; the weaker rest collapse into one
  ``…`` marker (which lights up as whichever collapsed row the list highlights).
* the **link list** beneath names every neighbour as a selectable row, strongest
  observed link first: type glyph, name, hash, SNR with a quality bar, the evidence
  behind the link (samples, sources, age), and how many links continue onward from
  that node. The highlighted row's marker and label light white on the canvas. The
  list scrolls *within* the screen — the canvas, legend, and heading hold still, and
  faint ``↑/↓ n more`` markers bracket the window — with ↑↓ moving one row and
  PgUp/PgDn a windowful.

**Enter walks**: the highlighted neighbour becomes the new focus, the breadcrumb trail
across the top grows (``you › YUL-Cartierville › …``, each name in its node's own hue),
and **⌫ steps back** along it. Walking to a node already on the trail truncates the stack
to its first appearance — the loop you walked to get back there is dropped rather than
recorded — and when the trail outgrows the line its *head* is dropped behind a leading
``…`` so the focus stays visible. **Home** refocuses our own node. **Typing finds** — a
global filter over every node in
the graph, islands included; Enter teleports the focus to the highlighted match (the
trail restarts there, since the walk didn't cross the gap). Esc peels find first, the
screen second.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from rich.cells import cell_len
from rich.text import Text

from ..core.models import (
    NODE_TYPE_REPEATER,
    Contact,
    utcnow,
)
from ..services.topology import Link, MeshTopology
from .map_render import _NODE, _REPEATER, _SELF, _UNKNOWN
from .mapcanvas import RGB, MapCanvas, parse_hex
from .menus import fit_cells
from .theme import name_style, snr_style
from .trace_screen import snr_bar
from .tui.render import render_to_ansi
from .tui.screen import ListWindow, Screen
from .widgets import _format_age, highlighted_hash

if TYPE_CHECKING:
    from ..context import AppContext

#: Horizontal / vertical dot-space margins the neighbour fan keeps clear of the canvas
#: edge. The east margin is wide enough to hold a fan node's rightward label (see
#: :meth:`AtlasScreen._label_right`), so even the fan's outermost marker has room to
#: name itself without the label running off the edge.
_PAD_X_DOTS = 28
_PAD_Y_DOTS = 6

#: The canvas's floor in character rows: below this the fan's shape stops reading.
_CANVAS_MIN_H = 6

#: Rows the link list always keeps for itself under the canvas, however tall the
#: graph would like to be — a windowed list needs at least a few rows to scroll in.
_LIST_MIN_ROWS = 3

#: The widest the focus/selection label may render on the canvas before it ellipsizes.
_LABEL_W = 16

#: The widest a *fan* node's rightward label renders before it ellipsizes. Shorter than
#: the focus/selection cap so a name always fits in the east gutter (:data:`_PAD_X_DOTS`)
#: even on the fan's outermost marker; the list below carries the fuller name.
_FAN_LABEL_W = 12

#: The fan's angular reach on each side of due east, in radians. The whole fan stays
#: east of the focus — neighbours to the right, labels rightward — and a smaller
#: neighbourhood uses proportionally less of the arc so two nodes never sit at its
#: extremes with nothing between them.
_FAN_HALF_ANGLE = math.radians(72)

#: Vertical dot span one fan marker (with the row its rightward label lands in) claims;
#: the canvas area divided by this is how many neighbours the graph draws before the
#: weaker rest collapse into the one ``…`` marker. Deliberately roomy — a sparser fan
#: with space to breathe reads far better than a full one, and each marker's name gets
#: a clear row beside it rather than jostling its neighbours'.
_FAN_SLOT_DOTS = 12

#: Sentinel key for the collapsed weaker-links marker in the placed-node map. NUL can
#: never collide with a canonical id (those are hex).
_MORE = "\x00more"

#: How many find matches the list shows at most (the filter narrows it fast).
_MAX_MATCHES = 10

#: The narrowest the neighbour/match list's name lane shrinks to. The lane is content-sized
#: and flexes up to whatever the fixed lanes leave (see :meth:`AtlasScreen._name_lane_w`),
#: so as much of a long name shows as the row can spare.
_LIST_NAME_MIN = 10

#: Display cells the key lane spans: the whole 12-hex canonical id, no ellipsis — the node's
#: full hash, with its addressed prefix lit in the node's hue (see :func:`highlighted_hash`).
_LIST_HASH_W = 12

#: Cells a link row spends *outside* its (flexing) name lane, so the lane can size to the
#: rest: pointer (2) + type glyph and its space (2) + the name→key space (1) + the key lane
#: (:data:`_LIST_HASH_W`) + gap (2) + SNR (5) + space (1) + quality bar (4) + samples (5)
#: + source tags (5) + age (5) + a reserve for the trailing ⌫/⋯ marker (8).
_LINK_ROW_FIXED = 2 + 2 + 1 + _LIST_HASH_W + 2 + 5 + 1 + 4 + 5 + 5 + 5 + 8

#: The same, for a find-match row — which trails a short distance note rather than the SNR
#: evidence: pointer (2) + glyph and space (2) + name→key space (1) + the key lane + gap (2)
#: + a distance reserve (12, for ``this device`` / ``N hops out``).
_MATCH_ROW_FIXED = 2 + 2 + 1 + _LIST_HASH_W + 2 + 12

#: SNR (dB) → edge colour anchors, interpolated linearly and clamped at the ends: the
#: red/amber/green of the app's snr styles, so the graph and the rows agree.
_SNR_STOPS: tuple[tuple[float, RGB], ...] = (
    (-15.0, (239, 68, 68)),
    (0.0, (250, 204, 21)),
    (10.0, (74, 222, 128)),
)

#: Edge colour for a link with no SNR reading at all (e.g. known only from a route).
_NO_READING: RGB = (100, 116, 139)

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
    ) -> None:
        """Create the atlas over a built topology snapshot.

        Args:
            session: The running TUI session (repaints).
            topo: The evidence graph to walk.
            contacts: Contacts keyed by canonical id, for glyphs, names, and ages.
            self_label: Display name for our own node (its mesh name when known).
            prefix_bytes: Path-hash width to light in the hash lane (0 = none).
        """
        super().__init__()
        self._session = session
        self._topo = topo
        self._contacts = contacts
        self._self_label = self_label
        self._prefix_bytes = prefix_bytes
        #: The walked trail of canonical ids; the focus is its last entry. Walking
        #: appends, ⌫ pops, Home resets to us, a find teleport restarts it.
        self._trail: list[str] = [topo.self_id]
        #: Index of the highlighted row in the current list (neighbours or matches).
        self._index = 0
        #: The live find-as-you-type filter ("" = off; matches every node known).
        self._filter = ""
        self._needs_scrub = True  # braille smear scrub, exactly like the map
        #: The link list's window (the list scrolls, the screen doesn't); its
        #: settled capacity is the stride a PgUp/PgDn moves the highlight by.
        self._list = ListWindow()

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
        """Move the highlight, walk, back up, find, or dismiss."""
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
            # The highlight pages by one list windowful: the window follows the
            # highlight, so paging the view without it would just snap straight back.
            self._index = max(0, self._index - self._list.page)
        elif action == "pagedown" and rows:
            self._index = min(len(rows) - 1, self._index + self._list.page)
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
        elif target in self._trail:
            # Revisiting a node already on the trail — stepping back through the west
            # node, or looping round to an earlier one — truncates the stack to that
            # node's first appearance. We drop the circular stretch we walked to get
            # back here rather than recording the round trip; losing the loop is the point.
            self._trail = self._trail[: self._trail.index(target) + 1]
        else:
            self._trail.append(target)
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
        """Render the trail, the focus line, the canvas, and the windowed link list.

        The body is laid out to fit the frame's viewport exactly: the canvas takes
        the majority of the rows (a touch less of the share on tall terminals), the
        chrome around it holds still, and whatever remains is the link list's
        window — only its rows scroll, inside :meth:`_list_lines`.
        """
        links = self._topo.links()
        rows = self._rows()
        self._index = max(0, min(self._index, len(rows) - 1)) if rows else 0
        self.title = self._compose_title(links)
        if not links:
            return self._empty_state(width)

        depths = self._hops_out()
        selected = rows[self._index] if rows else None
        viewport = self._scroll_viewport  # recorded by the frame before this render

        header = self._header_lines(width, depths)
        chrome = len(header) + 3  # legend, blank, list heading
        canvas_h = self._canvas_height(viewport, chrome)
        list_win = max(1, viewport - chrome - canvas_h)

        lines: list[str] = list(header)
        lines.extend(self._canvas_lines(width, canvas_h, selected))
        lines.append(render_to_ansi(self._legend(), width, no_wrap=True))
        lines.append("")
        lines.extend(self._list_lines(width, rows, depths, list_win))
        self._scroll_total = max(1, len(lines))
        return lines

    def _canvas_height(self, viewport: int, chrome: int) -> int:
        """Rows the canvas takes: the majority of the screen, ceded where pointless.

        The share starts at ~62% of the viewport and tapers toward half on tall
        terminals (a huge graph area buys little once the fan is legible, while the
        list keeps earning rows). A sparse neighbourhood caps it lower — a two-node
        link needs no half-screen void — and the link list always keeps its
        :data:`_LIST_MIN_ROWS` under the fixed chrome.
        """
        crowd = len(self._links_of(self._focus))
        share = 0.62 - 0.12 * min(max(viewport - 20, 0) / 24.0, 1.0)
        height = round(viewport * share)
        height = min(height, max(_CANVAS_MIN_H, 5 + 2 * crowd))
        height = min(height, viewport - chrome - _LIST_MIN_ROWS)
        return max(4, height)

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
            out.append(render_to_ansi(self._trail_text(width), width, no_wrap=True))
        out.append(render_to_ansi(self._focus_line(depths), width, no_wrap=True))
        return out

    def _trail_text(self, width: int) -> Text:
        """The breadcrumb trail, each name in its own key-derived hue, tail-anchored.

        Names carry the app-wide per-node hue (ours the white ``you``, a nameless node
        muted), the focus bold. When the whole trail won't fit on the line the *head* is
        dropped behind a leading ``…`` — never the tail — so the focus and the steps that
        led to it are always the ones kept in view.
        """
        nodes = self._trail
        sep = " › "

        def width_of(start: int) -> int:
            total = cell_len("…") + cell_len(sep) if start else 0  # the leading "… › "
            for k in range(start, len(nodes)):
                total += cell_len(sep) if k > start else 0
                total += cell_len(self._label(nodes[k]))
            return total

        start = 0
        while start < len(nodes) - 1 and width_of(start) > width:
            start += 1

        trail = Text()
        if start:
            trail.append("…" + sep, style="muted")
        for k in range(start, len(nodes)):
            if k > start:
                trail.append(sep, style="muted")
            last = k == len(nodes) - 1
            trail.append(self._label(nodes[k]), style=self._trail_style(nodes[k], last))
        trail.truncate(width, overflow="ellipsis")  # guard a lone label wider than the line
        return trail

    def _trail_style(self, node: str, last: bool) -> str:
        """A trail name's style: its key hue (ours white, a nameless node muted), focus bold."""
        base = self._list_name_style(node)
        return f"bold {base}" if last else base

    def _focus_line(self, depths: dict[str, int]) -> Text:
        """Who is in focus: glyph, name with its parenthesized hash, distance, and recency.

        The leading glyph already carries the node type, so the line no longer spells it
        out; the name reads ``name (hash)`` — the addressed path-hash in parentheses, its
        digits lit in the node's hue — rather than a bare slice of the key.
        """
        node = self._focus
        glyph, color = self._glyph(node)
        contact = self._contacts.get(node)
        line = Text()
        line.append(glyph, style=color)
        line.append(" ")
        line.append(self._label(node), style=self._list_name_style(node))
        short = self._short_hash(node)
        if short:
            line.append(" (", style="muted")
            line.append_text(highlighted_hash(short, self._prefix_bytes))
            line.append(")", style="muted")
        if node == self._topo.self_id:
            line.append("  ·  this device", style="muted")
        else:
            if node not in depths:
                line.append("  ·  island — no observed path to you", style="warn")
            else:
                ring = depths[node]
                line.append(f"  ·  {ring} hop{'s' if ring != 1 else ''} out", style="muted")
            if contact is not None and contact.last_seen is not None:
                secs = max(0.0, (utcnow() - contact.last_seen).total_seconds())
                line.append(f"  ·  heard {_format_age(secs)}", style="muted")
        return line

    def _short_hash(self, node: str) -> str:
        """The node's addressable path-hash as hex, or ``''`` for a placeholder id.

        The first ``prefix_bytes`` bytes (at least one, so there is always a hash to show)
        of a hex id; a non-hex stand-in like ``"local"`` — our own node with no key — has
        no hash and yields the empty string, so the focus line drops the parenthetical.
        """
        raw = node.lower()
        if not raw or any(c not in "0123456789abcdef" for c in raw):
            return ""
        return raw[: max(1, self._prefix_bytes) * 2]

    # -- the canvas --

    def _canvas_lines(
        self, width: int, canvas_h: int, selected: Optional[str]
    ) -> list[str]:
        """Draw the focus neighbourhood: focus at the left, the strongest fan east.

        Only as many neighbours as the area can carry get their own marker (see
        :meth:`_fan_capacity`); the weaker rest collapse into one ``…`` marker at
        the fan's foot. Highlighting a collapsed row from the list lights that
        marker white and swaps its label for the highlighted node's name, so the
        selection is always somewhere on the picture.
        """
        canvas = MapCanvas(width, canvas_h)
        fx, fy = self._focus_pos(width, canvas_h)

        pairs = self._links_of(self._focus)
        by_other = dict(pairs)
        back = self._came_from if self._came_from in by_other else None
        fan = [other for other, _link in pairs if other != back]
        capacity = self._fan_capacity(canvas_h)
        if len(fan) > capacity + 1:  # collapsing exactly one node would save nothing
            shown, hidden = fan[:capacity], fan[capacity:]
        else:
            shown, hidden = fan, []
        placed = self._place_neighbours(width, canvas_h, shown, back, bool(hidden))

        # Edges first (markers and labels overprint them), coloured by SNR and faded
        # by evidence age; the highlighted neighbour's edge wins its cells. The
        # collapsed marker's edge is slate — unless the selection hides in it, when
        # it takes the selected link's colour instead.
        now = utcnow()
        for other, (x, y) in placed.items():
            if other == _MORE:
                if selected in hidden:
                    link = by_other[selected]
                    color = _scaled(
                        _snr_rgb(link.median_snr), _freshness(link.last_seen, now)
                    )
                    priority = 3
                else:
                    color = _scaled(_NO_READING, 0.6)
                    priority = 1
            else:
                link = by_other[other]
                color = _scaled(_snr_rgb(link.median_snr), _freshness(link.last_seen, now))
                priority = 3 if other == selected else 2
            canvas.draw_line([(fx, fy), (x, y)], color, priority)

        # The focus marker, its label to the LEFT — the walker reads left-to-right,
        # so the focus name never sits in the fan's way.
        glyph, color_hex = self._glyph(self._focus)
        canvas.marker(fx, fy, glyph, parse_hex(color_hex))
        self._label_left(canvas, fx, fy, self._label(self._focus), (255, 255, 255))

        # Markers first, then labels. The selection keeps the smart two-sided
        # placement (it reads well as-is); every other node is named to the RIGHT
        # of its icon, the reading direction of the walk. Labels are laid
        # most-important-first (selection, then the trail-back node, then strongest
        # links) so the collision check drops the least important where two would
        # overprint.
        white = (255, 255, 255)
        for other, (x, y) in placed.items():
            if other == _MORE:
                rgb = white if selected in hidden else parse_hex(_UNKNOWN[1])
                canvas.marker(x, y, "…", rgb)
                continue
            glyph, color_hex = self._glyph(other)
            canvas.marker(x, y, glyph, white if other == selected else parse_hex(color_hex))
        if selected is not None and selected in placed:
            x, y = placed[selected]
            self._place_label(canvas, x, y, self._label(selected), white)
        rightward = [n for n in (back,) if n is not None and n in placed and n != selected]
        rightward += [n for n in shown if n != selected and n != back]
        for other in rightward:
            x, y = placed[other]
            self._label_right(canvas, x, y, self._label(other), parse_hex(self._glyph(other)[1]))
        if _MORE in placed:
            x, y = placed[_MORE]
            if selected in hidden:
                self._label_right(canvas, x, y, self._label(selected), white)
            else:
                self._label_right(canvas, x, y, f"+{len(hidden)} weaker", parse_hex(_UNKNOWN[1]))

        return canvas.to_ansi_lines()

    def _fan_capacity(self, canvas_h: int) -> int:
        """How many fan markers the canvas area carries before the rest collapse.

        One marker (plus the row its label may need) wants :data:`_FAN_SLOT_DOTS`
        of the fan's vertical span; the area's height decides the count — a taller
        graph area simply shows more of the mesh.
        """
        span = canvas_h * 4 - 2 * _PAD_Y_DOTS
        return max(3, span // _FAN_SLOT_DOTS + 1)

    def _focus_pos(self, width: int, canvas_h: int) -> tuple[int, int]:
        """The focus marker's dot position: left of centre, room for its west label."""
        label_cells = min(len(self._label(self._focus)), _LABEL_W)
        dot_w = width * 2
        x = max((label_cells + 3) * 2, dot_w // 5)
        return min(x, dot_w // 3), (canvas_h * 4) // 2

    def _place_neighbours(
        self,
        width: int,
        canvas_h: int,
        shown: list[str],
        back: Optional[str],
        more: bool,
    ) -> dict[str, tuple[int, int]]:
        """Dot-space positions for the drawn neighbourhood.

        The trail-back node (when among the neighbours) anchors at the far west,
        a couple of rows below the focus — the focus's own label owns the row to
        its left, so the back node ducks under it and keeps its label eastward;
        everyone shown fans across the arc east of the focus, strongest link at
        the top, weakest at the bottom — the same order as the list below, so the
        picture and the rows correspond — with the collapsed ``…`` marker (keyed
        :data:`_MORE`) taking the fan's last slot. A small fan uses proportionally
        less of the arc, so two neighbours sit near due east rather than at
        opposite rims.
        """
        dot_w, dot_h = width * 2, canvas_h * 4
        fx, fy = self._focus_pos(width, canvas_h)
        placed: dict[str, tuple[int, int]] = {}
        if back is not None:
            placed[back] = (4, min(fy + 8, dot_h - 4))
        slots = len(shown) + (1 if more else 0)
        if not slots:
            return placed
        rx = max(10.0, dot_w - _PAD_X_DOTS - fx)
        ry = max(4.0, dot_h / 2.0 - _PAD_Y_DOTS)
        phi = _FAN_HALF_ANGLE * min(1.0, (slots - 1) / 5.0)
        keys = list(shown) + ([_MORE] if more else [])
        for i, node in enumerate(keys):
            angle = 0.0 if slots == 1 else -phi + (2 * phi) * i / (slots - 1)
            x = fx + rx * math.cos(angle)
            y = fy + ry * math.sin(angle)
            placed[node] = (round(x), round(y))
        return placed

    def _place_label(
        self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB
    ) -> None:
        """Place one marker label (right of the marker when it fits, else left),
        retrying a row below then above on collision."""
        if len(label) > _LABEL_W:
            label = label[: _LABEL_W - 1] + "…"
        for dy in (0, 4, -4):
            if canvas.marker_label(x, y + dy, label, rgb):
                return

    def _label_right(
        self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB
    ) -> None:
        """Place a node's label to the *right* of its marker, dodging by row.

        Every node but the selected one (which keeps the two-sided
        :meth:`_place_label`) is named to the right of its icon — the reading
        direction of the walk. The label tries the marker's own row first, then a
        row below and above to slip past a crowded neighbour; if every checked row
        is blocked it is stamped to the right regardless, so a node is never left a
        bare glyph. The fan's east gutter (:data:`_PAD_X_DOTS`) is sized so a
        :data:`_FAN_LABEL_W`-capped name lands without running off the edge, even on
        the fan's outermost marker.
        """
        if len(label) > _FAN_LABEL_W:
            label = label[: _FAN_LABEL_W - 1] + "…"
        cx, cy = x >> 1, y >> 2
        start = cx + 2
        for dy in (0, 1, -1, 2, -2):
            if canvas._place_run(start, cy + dy, label, rgb, bold=True, checked=True):
                return
        canvas._place_run(start, cy, label, rgb, bold=True)

    def _label_left(
        self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB
    ) -> None:
        """Place the focus label to the *left* of its marker, forced if it must be.

        The focus is drawn first, so a collision is rare (a trail-back label at
        most); after the dodge rows are exhausted the label is stamped anyway —
        the focus must always be named.
        """
        if len(label) > _LABEL_W:
            label = label[: _LABEL_W - 1] + "…"
        cx, cy = x >> 1, y >> 2
        start = cx - 1 - len(label)
        for dy in (0, 1, -1):
            if canvas._place_run(start, cy + dy, label, rgb, bold=True, checked=True):
                return
        canvas._place_run(start, cy, label, rgb, bold=True)

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
        self, width: int, rows: list[str], depths: dict[str, int], win: int
    ) -> list[str]:
        """The selectable rows, windowed to ``win`` lines under a pinned heading.

        Only the rows scroll — the heading (and everything above it) holds still.
        When the list outgrows the window, faint ``↑/↓ n more`` markers take the
        window's edge rows and the highlight is kept inside what remains; the
        window's row count becomes the PgUp/PgDn stride.
        """
        out: list[str] = []
        if self._filter:
            heading = Text("Matches", style="accent")
            heading.append("  ·  nearest first · Enter focuses", style="muted")
            out.append(render_to_ansi(heading, width, no_wrap=True))
            if not rows:
                out.append(render_to_ansi(Text("no matches", style="muted"), width))
                return out

            name_w = self._name_lane_w(width, rows, _MATCH_ROW_FIXED)

            def render(i: int) -> Text:
                return self._match_row(rows[i], i == self._index, depths, name_w)
        else:
            heading = Text("Links", style="accent")
            heading.append("  ·  strongest observed first · Enter walks", style="muted")
            out.append(render_to_ansi(heading, width, no_wrap=True))
            pairs = self._links_of(self._focus)
            if not pairs:
                note = Text(
                    "no observed links from here — type to find another node", style="muted"
                )
                out.append(render_to_ansi(note, width))
                return out
            onward = self._onward_counts(pairs)
            name_w = self._name_lane_w(width, [o for o, _ in pairs], _LINK_ROW_FIXED)

            def render(i: int) -> Text:
                other, link = pairs[i]
                return self._link_row(
                    other, link, i == self._index, onward.get(other, 0), name_w
                )

        top, count = self._list.fit(len(rows), win, self._index)
        if top > 0:
            out.append(render_to_ansi(ListWindow.marker(top, "above"), width))
        for i in range(top, top + count):
            out.append(render_to_ansi(render(i), width, no_wrap=True))
        below = len(rows) - top - count
        if below > 0:
            out.append(render_to_ansi(ListWindow.marker(below, "below"), width))
        return out

    def _name_lane_w(self, width: int, nodes: list[str], fixed: int) -> int:
        """The list's name-lane width: as much of the name as fits, sized to content.

        Grows to the widest name the rows carry but never past the width the fixed lanes
        leave, so a short-name list stays tight while a long name shows as much of itself
        as the row can spare (ellipsized only past that).
        """
        avail = max(_LIST_NAME_MIN, width - fixed)
        widest = max((cell_len(self._label(n)) for n in nodes), default=_LIST_NAME_MIN)
        return max(_LIST_NAME_MIN, min(widest, avail))

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

    def _link_row(
        self, other: str, link: Link, selected: bool, onward: int, name_w: int
    ) -> Text:
        """One neighbour row: glyph, name, hash, SNR + bar, evidence, onward count."""
        glyph, glyph_style = self._glyph(other)
        row = Text()
        row.append("❯ " if selected else "  ", style="brand" if selected else "")
        row.append(glyph, style=glyph_style)
        row.append(" ")
        name_style_ = self._list_name_style(other)
        row.append(fit_cells(self._label(other), name_w), style=name_style_)
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

    def _match_row(
        self, node: str, selected: bool, depths: dict[str, int], name_w: int
    ) -> Text:
        """One find match: glyph, name, hash, and how far out it sits."""
        glyph, glyph_style = self._glyph(node)
        row = Text()
        row.append("❯ " if selected else "  ", style="brand" if selected else "")
        row.append(glyph, style=glyph_style)
        row.append(" ")
        row.append(fit_cells(self._label(node), name_w), style=self._list_name_style(node))
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
        """The list's name colour: the app-wide palette hue (hash-derived), us in pure white."""
        if node == self._topo.self_id:
            return "you"
        label = self._label(node)
        if label == node[:8]:  # a bare hash is not a name — colour is the name signal
            return "muted"
        return name_style(label, node)

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
    names); the graph itself comes entirely from the repository, snapshotted once when
    the screen opens. No transmissions, ever.

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
            contacts = await ctx.devstate.contacts()
            info = await ctx.devstate.self_info()
            self_label = str(info.get("name") or "you")
            self_hash = str(info.get("public_key") or "") or None
    except Exception:  # noqa: BLE001 - names are a nicety; the graph renders without them
        contacts = []
    prefix_bytes = await _routing_prefix_bytes(ctx)

    topo = build_topology(
        self_id=self_hash or "local",
        contacts=contacts,
        trace_paths=ctx.repo.trace_paths(),
        packet_paths=ctx.repo.packet_paths(),
        neighbour_links=ctx.repo.neighbour_links(),
    )
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
    )
    try:
        await session.run_screen(screen)
    finally:
        # The same clean-slate repaint as the map: braille fallback glyphs may have
        # smeared cells prompt_toolkit's differential paint will never rewrite.
        session.request_full_repaint()
