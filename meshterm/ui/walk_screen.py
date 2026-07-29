"""The Mesh walk: walk the mesh's observed shape one node at a time.

The interactive face of the ``walk`` tool. The trace path composer already distills
every fragment of topology we ever received — trace walks, firmware routes, RX-logged
relay chains, repeater neighbour tables — into one evidence graph
(:mod:`~meshterm.services.topology`); this screen is that graph made explorable. Nothing
here transmits: the walk is a reading of what the radio has already heard.

Rather than plotting the whole mesh at once (which reads as a hairball the moment the
graph grows), the walk keeps one node *in focus* — our own, to begin with — and shows
only its immediate neighbourhood:

* the **canvas** — the majority of the screen, so the shape stays legible — anchors the
  focus at the far **west** with its name to its right, then fans a deliberately sparse
  spread of its strongest neighbours across the width to the east, each named just to the
  right of its marker. Every marker is drawn in its own **key-derived hue** (the app-wide
  per-node colour, keyed on the node's key), never the node *type* — the glyph *shape*
  still carries the type. Edges are braille lines coloured by the link's median SNR
  (green → amber → red, slate for links with no reading) and faded by evidence age; they
  leave the *right end of the focus name* (and land on the right end of the came-from
  name) so a connecting line never crosses a label. The node the trail came from ducks
  in just west of the focus and lower, so walking always reads as moving right and backing
  up as moving left. Only as many neighbours as the canvas area can carry are drawn — a
  sparse fan reads far better than a crowded one; the weaker rest, ranked by observed
  strength (SNR, sample count, and recency folded together, so a strong-but-long-stale
  link is not promoted over a fresher one), collapse into one ``…`` marker (which lights
  up as whichever collapsed row the list highlights).
* the **link list** beneath names every neighbour as a selectable row, strongest
  observed link first: type glyph, name, hash, SNR with a quality bar, the evidence
  behind the link (samples, sources, age), and how many links continue onward from
  that node. The highlighted row's marker and label light white on the canvas. The
  list scrolls *within* the screen — the canvas, legend, and heading hold still, and
  faint ``↑/↓ n more`` markers bracket the window — with ↑↓ moving one row and
  PgUp/PgDn a windowful.

**Enter walks**: the highlighted neighbour becomes the new focus, the breadcrumb trail
across the top grows (a :mod:`~meshterm.ui.pathline` path line — powerline chips where the
terminal draws them, ``you › YUL-Cartierville › …`` where it doesn't, each name in its
node's own hue), and **⌫ steps back** along it. Walking to a node already on the trail
truncates the stack to its first appearance — the loop you walked to get back there is
dropped rather than recorded — and when the trail outgrows the line it neither wraps nor
scrolls: its *head* goes behind a leading ``⋯`` and the rest snaps flush right, so the
focus and the steps just taken stay in view. **Home** refocuses our own node. **Typing finds** — a
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
from .pathline import ELIDE_HEAD, PathHop, PathLine
from .theme import name_style, node_style, snr_style
from .trace_screen import snr_bar
from .tui.render import render_to_ansi
from .tui.screen import ListWindow, Screen
from .widgets import _format_age, highlighted_hash

if TYPE_CHECKING:
    from ..context import AppContext

#: Horizontal / vertical dot-space margins the neighbour fan keeps clear of the canvas
#: edge. The east margin holds a fan node's rightward label (see
#: :meth:`WalkScreen._label_right`); it is deliberately roomy — pulling the whole fan a
#: little west of the edge — so even the tightest (due-east) marker has cells to name
#: itself in full rather than clipping a long contact name, which the labels' room-to-edge
#: clamp then spends wherever the fan leaves it.
_PAD_X_DOTS = 44
_PAD_Y_DOTS = 6

#: The canvas's floor in character rows: below this the fan's shape stops reading.
_CANVAS_MIN_H = 6

#: Rows the link list always keeps for itself under the canvas, however tall the
#: graph would like to be — a windowed list needs at least a few rows to scroll in.
_LIST_MIN_ROWS = 3

#: The widest any on-canvas label renders before it ellipsizes — a generous cap that most
#: real node names clear whole. It is only an upper bound: each label is *also* clamped to
#: the cells actually free between its marker and the canvas edge (see :meth:`_label_right`,
#: :meth:`_focus_anchor`, :meth:`_place_label`), so a name loses letters only when it
#: genuinely won't fit, not to a fixed short budget — the fan's placement leaves most
#: markers far more room than the old flat cap allowed.
_LABEL_W = 22

#: Kept as the *fan* label cap for symmetry with the focus/selection one; both now defer to
#: the per-marker room-to-edge clamp, so the two need no longer differ.
_FAN_LABEL_W = _LABEL_W

#: The fan's angular reach on each side of due east, in radians. This sets the fan's
#: *vertical* spread (the marker rows the leaves fan across); the *horizontal* reach is
#: flattened from it by :data:`_FAN_X_FLATTEN`, so a leaf's height and its easting are
#: decoupled. The whole fan stays east of the focus — neighbours to the right, labels
#: rightward — and a smaller neighbourhood uses proportionally less of the arc so two
#: nodes never sit at its extremes with nothing between them.
_FAN_HALF_ANGLE = math.radians(72)

#: How much flatter the fan's horizontal reach is than its vertical spread. A leaf's full
#: fan angle sets its *height*; its *east reach* uses only this fraction of that angle, so
#: the rim (top/bottom) leaves bow just gently back from due-east instead of curling in
#: toward the focus on a true circle. The fan then spreads across the width rather than
#: bulging in the middle with empty corners. ``1.0`` restores the old circular arc; lower
#: flattens it further. The setback still scales with the fan's angular reach, so a small
#: fan sitting near due-east barely eases back at all.
_FAN_X_FLATTEN = 0.55

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
#: and flexes up to whatever the fixed lanes leave (see :meth:`WalkScreen._lane_widths`),
#: so as much of a long name shows as the row can spare.
_LIST_NAME_MIN = 10

#: The *widest* the key lane spans — the whole 12-hex canonical id, addressed prefix lit in
#: the node's hue (see :func:`highlighted_hash`). The lane flexes down from here on a byte
#: boundary to hand a long name more room, but never below its lit hash (see
#: :meth:`WalkScreen._lane_widths`): the name loses letters before the hash does.
_LIST_HASH_W = 12

#: Cells a link row spends *outside* its two flexing lanes (name and key), so those two can
#: size to what's left: pointer (2) + type glyph and its space (2) + the name→key space (1)
#: + gap (2) + SNR (5) + space (1) + quality bar (4) + samples (5) + source tags (5)
#: + age (5) + a reserve for the trailing ⌫/⋯ marker (8).
_LINK_ROW_FIXED = 2 + 2 + 1 + 2 + 5 + 1 + 4 + 5 + 5 + 5 + 8

#: The same, for a find-match row — which trails a short distance note rather than the SNR
#: evidence: pointer (2) + glyph and space (2) + name→key space (1) + gap (2)
#: + a distance reserve (12, for ``this device`` / ``N hops out``).
_MATCH_ROW_FIXED = 2 + 2 + 1 + 2 + 12

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


class WalkScreen(Screen):
    """The full-screen mesh walker: a focus neighbourhood canvas over a link list."""

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
        """Create the walk over a built topology snapshot.

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
        needle = self._filter.strip().casefold()
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
            if not data.isspace() or self._filter:  # never begin the filter with a space
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
        """``Mesh walk — focus`` plus the graph's status atoms."""
        title = f"Mesh walk — {self._label(self._focus)}"
        title += f" · {len(self._all_nodes())} nodes · {len(links)} links"
        if self._filter:
            matches = self._matches()
            title += f" · {len(matches)} match{'es' if len(matches) != 1 else ''}"
        return title

    def _header_lines(self, width: int, depths: dict[str, int]) -> list[str]:
        """The breadcrumb trail and the focus node's identity line.

        The trail draws always — even at the root, where it is just our own node — so the
        breadcrumb is a constant fixture and the header keeps a steady height whether you
        have walked or not, rather than the whole body shifting up a row the moment you take
        the first step.
        """
        return [
            render_to_ansi(self._trail_text(width), width, no_wrap=True),
            render_to_ansi(self._focus_line(depths), width, no_wrap=True),
        ]

    def _trail_text(self, width: int) -> Text:
        """The breadcrumb trail as a path line: one line, tail-anchored, never wrapped.

        The walk *is* a path — us, then every node stepped through, ending on the focus —
        so it renders through :class:`~meshterm.ui.pathline.PathLine` like every other hop
        sequence in the app: powerline chips wherever the terminal can draw them, the
        trail's own ``›`` arrows where it can't. Each hop wears its node's key-derived hue
        (ours the white ``you``; a node known only by a bare hash stays muted — colour is
        reserved for keyed identities), so the trail and the rows below it agree on who is
        who.

        The line neither wraps nor scrolls. When the walk outgrows the width the fit eats
        into its *head* (:data:`~meshterm.ui.pathline.ELIDE_HEAD`) rather than a route's
        usual tail: the oldest steps disappear behind a leading ``⋯`` and what survives
        snaps flush against the **right** edge, so the focus and the steps that just led
        to it are the ones always in view. A long walk therefore reads as a line that
        grows rightward until it meets the margin and then starts shedding its oldest
        steps, rather than one that pushes the focus off the end.
        """
        line = PathLine([self._trail_hop(node) for node in self._trail], separator=" › ")
        full = line.text()
        if full.cell_len <= width:
            return full
        fitted = line.ellipsized(width, elide=ELIDE_HEAD)
        snapped = Text(" " * max(0, width - fitted.cell_len))  # snap the tail to the edge
        snapped.append_text(fitted)
        return snapped

    def _trail_hop(self, node: str) -> PathHop:
        """One walked step as a path hop: its display name in its own identity colour."""
        style = self._list_name_style(node)
        return PathHop(
            self._label(node),
            key=None if style in ("you", "muted") else node,
            you=style == "you",
        )

    def _focus_line(self, depths: dict[str, int]) -> Text:
        """Who is in focus: glyph, name with its parenthesized hash, distance, and recency.

        The leading glyph carries the node type as its *shape* and the node's own key hue
        as its *colour* (:meth:`_glyph_style`, matching the canvas marker), so the line no
        longer spells the type out; the name reads ``name (hash)`` — the addressed
        path-hash in parentheses, its digits lit in the node's hue — rather than a bare
        slice of the key.

        The ``N hops out`` distance is the *shortest* observed path from us to this node
        (a BFS over the whole evidence graph, :meth:`_hops_out`) — the node's ring in the
        mesh. It is deliberately **not** the length of the breadcrumb trail above, which is
        the route you happened to *walk* to reach the focus: wander out a long way and
        double back, or step to a node also reachable by a shorter link, and the walk is
        longer than the ring. The two answer different questions — "how near is this node?"
        versus "how did I get here?" — so a shorter "hops out" than the trail is correct,
        not a miscount.
        """
        node = self._focus
        glyph, _type_color = self._glyph(node)
        contact = self._contacts.get(node)
        line = Text()
        line.append(glyph, style=self._glyph_style(node))
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
        """Draw the focus neighbourhood: focus at the far west, the strongest fan east.

        The focus icon sits at the far west with its name to its right; the fan spreads
        east across the available width (see :meth:`_place_neighbours`), each neighbour
        named to the right of its marker. Every marker takes its own **key-derived hue**
        (:meth:`_marker_rgb`) rather than a node-*type* colour — the glyph *shape* carries
        the type — and no marker is whited out, not even the selection: the highlighted row
        is shown by its *label* going white and by the lit route, so the icons stay
        colourful throughout.

        Only as many neighbours as the area can carry get their own marker (see
        :meth:`_fan_capacity`); the weaker rest collapse into one ``…`` marker at the fan's
        foot. Highlighting a collapsed row from the list lights that marker white and swaps
        its label for the highlighted node's name, so the selection is always somewhere on
        the picture. Every edge leaves the *right end of the focus name* (and lands on the
        right end of the came-from name) so a connecting line never crosses a label; the
        edges tracing the *route to the selected link* — that link plus the approach the
        walk took into the focus — draw brightest and undimmed, lighting the whole path that
        reaches it.
        """
        canvas = MapCanvas(width, canvas_h)
        fx, fy = self._focus_pos(width, canvas_h)
        ax, ay, focus_name = self._focus_anchor(width, canvas_h)

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

        # The came-from node keeps its icon on the west with its name to the right, but its
        # edge home attaches at the *right end of that name* — like the focus — so the line
        # never crosses the label. Work that attach point out before drawing edges.
        back_attach: Optional[tuple[int, int]] = None
        if back is not None and back in placed:
            bx, by = placed[back]
            back_name = self._clip(self._label(back), width - ((bx >> 1) + 2))
            back_attach = (((bx >> 1) + 2 + cell_len(back_name) + 1) * 2, by)

        # Edges first (markers and labels overprint them), coloured by SNR and faded by
        # evidence age. Every edge leaves the focus's name-end anchor; a fan edge lands on
        # its marker, the came-from edge on its own name-end. The *route to the selected
        # link* draws brightest and on top: the selected neighbour's own edge, plus the
        # approach the walk took into the focus (came_from → focus) — so selecting a link
        # lights the whole path that reaches it, west through the focus to east, not just the
        # one hop. Route edges shed the age fade (drawn full-strength) so they read as one
        # lit thread over the dimmer rest; the age is still legible in the row's age column.
        # The collapsed marker's edge is slate — unless the selection hides in it, when it
        # joins the route.
        now = utcnow()
        route = {selected} if selected is not None else set()
        if selected is not None and back is not None and back != selected:
            route.add(back)  # the last walked step, drawn as part of the lit route
        for other, (x, y) in placed.items():
            dest = back_attach if (other == back and back_attach is not None) else (x, y)
            if other == _MORE:
                if selected in hidden:
                    link = by_other[selected]
                    color = _scaled(_snr_rgb(link.median_snr), 1.0)
                    priority = 4
                else:
                    color = _scaled(_NO_READING, 0.6)
                    priority = 1
            else:
                link = by_other[other]
                if other in route:
                    color = _scaled(_snr_rgb(link.median_snr), 1.0)
                    priority = 4
                else:
                    color = _scaled(_snr_rgb(link.median_snr), _freshness(link.last_seen, now))
                    priority = 2
            canvas.draw_line([(ax, ay), dest], color, priority)

        # The focus marker at the far west, its name to the RIGHT — icon left, name right,
        # the walk's reading way — both in the node's key hue (ours white). The edges have
        # already left the name-end, so they run east clear of the label rather than through
        # it.
        glyph, _type_color = self._glyph(self._focus)
        canvas.marker(fx, fy, glyph, self._marker_rgb(self._focus))
        self._label_right(canvas, fx, fy, focus_name, self._label_rgb(self._focus))

        # Markers first, then labels. Every marker keeps its key hue — the selection is
        # shown by its white *label* and the lit route, never by whiting out the icon.
        # Labels are laid most-important-first (selection, then the trail-back node, then
        # strongest links) so the collision check drops the least important where two would
        # overprint.
        white = (255, 255, 255)
        for other, (x, y) in placed.items():
            if other == _MORE:
                rgb = white if selected in hidden else parse_hex(_UNKNOWN[1])
                canvas.marker(x, y, "…", rgb)
                continue
            glyph, _type_color = self._glyph(other)
            canvas.marker(x, y, glyph, self._marker_rgb(other))
        if selected is not None and selected in placed:
            x, y = placed[selected]
            self._place_label(canvas, x, y, self._label(selected), white)
        rightward = [n for n in (back,) if n is not None and n in placed and n != selected]
        rightward += [n for n in shown if n != selected and n != back]
        for other in rightward:
            x, y = placed[other]
            self._label_right(canvas, x, y, self._label(other), self._label_rgb(other))
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
        """The focus marker's dot position: near the west edge, name and fan to its east.

        The icon hugs the left so its name (drawn to the right) and the neighbour fan get
        the whole width to spread across — the graph aired out rather than squeezed into
        the middle; the came-from node ducks in just west of it and lower (see
        :meth:`_place_neighbours`).
        """
        dot_w = width * 2
        x = max(4, dot_w // 12)
        return x, (canvas_h * 4) // 2

    def _focus_anchor(self, width: int, canvas_h: int) -> tuple[int, int, str]:
        """The focus's edge-attach point and its on-canvas name.

        The focus icon sits at the far west (:meth:`_focus_pos`) with its name to the
        *right*; every edge leaves the **right end of that name** so the connecting lines
        never cross the label. Returns the attach point in dot coordinates and the name as
        it is drawn (clipped to the room between the icon and the canvas edge).
        """
        fx, fy = self._focus_pos(width, canvas_h)
        icon_cx = fx >> 1
        name = self._clip(self._label(self._focus), width - (icon_cx + 2))
        anchor_cx = icon_cx + 2 + cell_len(name) + 1
        return anchor_cx * 2, fy, name

    def _place_neighbours(
        self,
        width: int,
        canvas_h: int,
        shown: list[str],
        back: Optional[str],
        more: bool,
    ) -> dict[str, tuple[int, int]]:
        """Dot-space positions for the drawn neighbourhood.

        The trail-back node (when among the neighbours) ducks in just west of the focus
        and lower — its icon stays on the west side, under the focus, with its name to the
        right; everyone shown fans across the arc east of the focus's name-end anchor,
        strongest link at the top, weakest at the bottom — the same order as the list
        below, so the picture and the rows correspond — with the collapsed ``…`` marker
        (keyed :data:`_MORE`) taking the fan's last slot. Anchoring the fan at the name-end
        (rather than at the icon) hands it the whole width east of the focus label to
        breathe in. The arc is flattened horizontally (:data:`_FAN_X_FLATTEN`): a leaf's
        fan angle sets its row, but its east reach bows only gently back from due-east, so
        the rim leaves spread across the width instead of curling in and leaving the
        corners empty. A small fan uses proportionally less of the arc, so two neighbours
        sit near due east rather than at opposite rims.
        """
        dot_w, dot_h = width * 2, canvas_h * 4
        fx, fy = self._focus_pos(width, canvas_h)
        ax, ay, _name = self._focus_anchor(width, canvas_h)
        placed: dict[str, tuple[int, int]] = {}
        if back is not None:
            placed[back] = (max(0, fx - 6), min(fy + 14, dot_h - 4))
        slots = len(shown) + (1 if more else 0)
        if not slots:
            return placed
        rx = max(10.0, dot_w - _PAD_X_DOTS - ax)
        ry = max(4.0, dot_h / 2.0 - _PAD_Y_DOTS)
        phi = _FAN_HALF_ANGLE * min(1.0, (slots - 1) / 5.0)
        keys = list(shown) + ([_MORE] if more else [])
        for i, node in enumerate(keys):
            angle = 0.0 if slots == 1 else -phi + (2 * phi) * i / (slots - 1)
            # The leaf's full fan angle sets its height (y); its east reach traces a
            # *flatter* arc — only :data:`_FAN_X_FLATTEN` of that angle — so a rim leaf
            # still reaches well east instead of curling back toward the focus on a true
            # circle. The falloff scales with the fan's reach, so a small near-due-east
            # fan barely eases back while a wide one fills the corners.
            x = ax + rx * math.cos(angle * _FAN_X_FLATTEN)
            y = ay + ry * math.sin(angle)
            placed[node] = (round(x), round(y))
        return placed

    @staticmethod
    def _clip(label: str, room: int) -> str:
        """``label`` fit to ``room`` cells: whole if it fits, else ellipsized (``…`` alone
        at one cell, nothing at zero). The cap and the room-to-edge both flow through here,
        so a name is only ever shortened as far as it truly must be."""
        room = min(room, _LABEL_W)
        if room <= 0:
            return ""
        if len(label) <= room:
            return label
        return "…" if room == 1 else label[: room - 1] + "…"

    def _place_label(
        self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB
    ) -> None:
        """Place one marker label (right of the marker when it fits, else left),
        retrying a row below then above on collision.

        The label is clamped to whichever side has more room — the cells free to the
        right of the marker, or to its left — so the selection keeps as much of its name
        as the canvas allows before ellipsizing."""
        cx = x >> 1
        room = max(canvas.cell_w - (cx + 2), cx - 1)  # the roomier of right / left
        label = self._clip(label, room)
        if not label:
            return
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
        bare glyph. The name is clamped to the cells actually free between the marker
        and the canvas edge, so it keeps its full length wherever the fan leaves room
        and only clips on the tightest (due-east) markers.
        """
        cx, cy = x >> 1, y >> 2
        start = cx + 2
        label = self._clip(label, canvas.cell_w - start)
        if not label:
            return
        for dy in (0, 1, -1, 2, -2):
            if canvas._place_run(start, cy + dy, label, rgb, bold=True, checked=True):
                return
        canvas._place_run(start, cy, label, rgb, bold=True)

    def _legend(self) -> Text:
        """The one-line glyph legend and edge key under the canvas.

        A *shape* key: on the canvas the glyph carries the node type while its colour
        carries the node's identity (its key hue), so the role glyphs here are drawn muted
        rather than in a type colour that would falsely read as "repeaters are violet". Our
        own node keeps the white ``you`` mark it wears on the canvas.
        """
        legend = Text()
        for glyph, _type_color in (_SELF, _REPEATER, _NODE, _UNKNOWN):
            legend.append(glyph, style="you" if glyph == _SELF[0] else "muted")
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

            name_w, key_w = self._lane_widths(width, rows, _MATCH_ROW_FIXED)

            def render(i: int) -> Text:
                return self._match_row(rows[i], i == self._index, depths, name_w, key_w)
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
            name_w, key_w = self._lane_widths(
                width, [o for o, _ in pairs], _LINK_ROW_FIXED
            )

            def render(i: int) -> Text:
                other, link = pairs[i]
                return self._link_row(
                    other, link, i == self._index, onward.get(other, 0), name_w, key_w
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

    def _lane_widths(self, width: int, nodes: list[str], fixed: int) -> tuple[int, int]:
        """The list's ``(name, key)`` lane widths — the name lane first, the key filling in.

        The name lane sizes to the widest name the rows carry (a short-name list stays
        tight); the key lane takes whatever's left, capped at the whole 12-hex id. When the
        two together outrun the row, the *key* gives ground first — shrinking on a byte
        boundary down to a floor that still shows its whole lit hash plus a ``…`` — and only
        once the key is at that floor does a long name start losing letters. So the name is
        the last thing truncated and the hash never is (the user reads names, and addresses
        by hash).
        """
        hash_w = max(2, min(_LIST_HASH_W, self._prefix_bytes * 2))
        # The tightest key lane that still shows the whole hash: the hash plus a "…". An odd
        # width, so :func:`highlighted_hash` keeps an even hash_w digits with no wasted cell;
        # when the hash already fills the id there's nothing to drop, so the floor is the
        # full width.
        key_floor = _LIST_HASH_W if hash_w >= _LIST_HASH_W else hash_w + 1
        avail = width - fixed
        widest = max((cell_len(self._label(n)) for n in nodes), default=_LIST_NAME_MIN)
        name_w = max(_LIST_NAME_MIN, min(widest, avail - key_floor))
        key_w = max(key_floor, min(_LIST_HASH_W, avail - name_w))
        return name_w, key_w

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
        self, other: str, link: Link, selected: bool, onward: int, name_w: int, key_w: int
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
        row.append_text(highlighted_hash(other, self._prefix_bytes, width=key_w))
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
        self, node: str, selected: bool, depths: dict[str, int], name_w: int, key_w: int
    ) -> Text:
        """One find match: glyph, name, hash, and how far out it sits."""
        glyph, glyph_style = self._glyph(node)
        row = Text()
        row.append("❯ " if selected else "  ", style="brand" if selected else "")
        row.append(glyph, style=glyph_style)
        row.append(" ")
        row.append(fit_cells(self._label(node), name_w), style=self._list_name_style(node))
        row.append(" ")
        row.append_text(highlighted_hash(node, self._prefix_bytes, width=key_w))
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

    def _marker_rgb(self, node: str) -> RGB:
        """The marker colour for a node: its key-derived hue, ours white, a keyless id grey.

        The walk colours every marker by *identity* — the app-wide per-node hue
        (:func:`~meshterm.ui.theme.node_style`, keyed on the node's own key) — rather than
        by node *type*; the glyph *shape* (see :meth:`_glyph`) is what carries the type. Our
        own node keeps the pure-white ``you`` convention, and a non-hex placeholder id (our
        own ``"local"`` before a key is known) has no hue to derive, so it falls back to the
        unknown grey.
        """
        if node == self._topo.self_id:
            return (255, 255, 255)
        raw = node.lower()
        if not raw or any(c not in "0123456789abcdef" for c in raw):
            return parse_hex(_UNKNOWN[1])
        return parse_hex(node_style(node).rsplit("#", 1)[-1])

    def _label_rgb(self, node: str) -> RGB:
        """The label colour for a node on the canvas: its name hue, ours white, a bare hash muted.

        The RGB counterpart of :meth:`_list_name_style`: a named node's key hue, our own
        node white, and a node known only by a bare hash muted (a key standing in as a name
        is never itself coloured, so the marker carries the identity and the hash-label
        stays grey).
        """
        style = self._list_name_style(node)
        if style == "you":
            return (255, 255, 255)
        if style == "muted":
            return parse_hex("#94a3b8")
        return parse_hex(style.rsplit("#", 1)[-1])

    def _glyph_style(self, node: str) -> str:
        """The Rich style a node's type glyph takes in body text: its key hue (ours white).

        The text-side companion of :meth:`_marker_rgb`, so the focus line's leading glyph
        matches its canvas marker — key-hued for a real node, the white ``you`` for us, and
        muted for a keyless placeholder id.
        """
        if node == self._topo.self_id:
            return "you"
        raw = node.lower()
        if not raw or any(c not in "0123456789abcdef" for c in raw):
            return "muted"
        return node_style(node)

    def _empty_state(self, width: int) -> list[str]:
        """A friendly explanation while the evidence graph is still empty."""
        lines = [
            Text(),
            Text("This walk has no evidence to draw yet.", style="accent"),
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


async def open_walk(ctx: "AppContext") -> None:
    """Build the evidence graph and run the full-screen mesh walk until dismissed.

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
        raise RuntimeError("the mesh walk is only available in the menu")
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

    screen = WalkScreen(
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
