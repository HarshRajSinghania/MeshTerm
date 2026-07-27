"""The Node detail screen: one node's whole story on a single page, and the ways into it.

Reached by pressing Enter on any contact in the Contacts list (see
:mod:`~meshterm.ui.contacts_screen`). Where the list is one aligned row per node, this is
the node itself, in full:

* **who it is** — a one-line identity header (its type glyph, its name in the node's own
  hue, its type label), the only chrome pinned above the stage — so the stage keeps nearly
  the whole screen;
* **a tabbed stage** below it — one full-height view at a time, switched with
  ``Tab``/``Shift+Tab`` across a boxed tab strip (see
  :func:`~meshterm.ui.widgets.tab_strip`). Rather than stack the vitals, the location
  preview, and the route graph down one long scroll, each view earns the whole stage:

  * **Info** — the node's vitals as labelled rows (its key with the routing hash lit, when
    it was first and last heard, how many packets we've overheard, its reception SNR and
    last RSSI, and where it sits), then — when the node has advertised a location — a
    static basemap preview (see :class:`~meshterm.ui.minimap.MiniMap`) centred on the node,
    grown to whatever rows the viewport spares. Its actions: ``Open full map`` (the full
    map opens centred here with its find filter seeded to this node, so it lights among
    the rest), ``Time machine``, and ``Share contact`` — a popup contact card (QR code +
    ``meshcore://`` link, see :func:`~meshterm.ui.config_editor.show_contact_card`),
    offered whenever the node's full key is known.
  * **Routes** — the routes we've actually heard the node arrive over, drawn on the shared
    route graph (:mod:`~meshterm.ui.pathgraph`) node→us (the inbound direction the packets
    travelled, contact on the left, us on the right). Beneath the graph sits the *route
    list*: one selectable row per distinct route (the firmware's learned route and the
    observed alternatives, the strongest marked ``★ best``), each spelled out through THE
    path widget with a hanging wrap. ``↑↓`` moves the selection; the picked route lights
    white in the graph while the rest go grey and every node not on it fades its label to
    grey, so the graph reads as *this* route through the fan. Only *good* routes are drawn —
    stale evidence and far-weaker outliers are dropped, so the list is the routes worth
    trusting rather than every chain ever heard. Enter on a route arms a trace on it
    (nothing transmits here — it opens the trace screen loaded with that path); when there
    is no route evidence to list, a ``🎯 Trace — auto route …`` action stands in.

* **the ways in** — each tab's action rows (``↑↓`` moves the cursor through them, Enter
  commits) and the shared ``Back`` closing every tab; Esc backs to the list.

The page never scrolls as one long strip. The identity header, the tab strip, and the
stage are pinned; the stage sizes itself to the terminal (the route graph compresses its
lanes before it would overflow, the location preview grows into what the Info tab spares)
and a faint rule closes it, so the drawn view and the rows below read as separate bands.
The route list scrolls *inside* the leftover rows with faint ``↑ n more`` / ``↓ n more``
edge markers — the app-wide windowed-list pattern (see
:class:`~meshterm.ui.tui.screen.ListWindow`; the variable-height rows get their own fit
here) — so the graph, the selection driving it, and the action rows share one screen
however many routes a busy node has. ``PgUp/PgDn`` page the cursor through the window.

The screen is a pure read-and-route view: it renders already-resolved display data and
resolves an action token (the Trace token carrying the selected route's spec via
:meth:`NodeDetailScreen.selected_spec`); :func:`open_node_detail` owns the data-gathering
and runs the sub-flows each action opens, then re-shows the page — the same loop the Time
Machine and Contacts list use. Nothing here transmits.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from rich.text import Text

from ..core.geo import EARTH_RADIUS_KM, usable_fix
from ..core.models import NODE_TYPE_LABELS, Contact, utcnow
from .mapcanvas import RGB, parse_hex
from .minimap import MiniMap
from .pathgraph import (
    DST_NODE,
    SRC_NODE,
    GlyphOf,
    LabelOf,
    LabelRgbOf,
    PathLayer,
    _coalesce_prefixes,
    bidir_clusters,
    render_path_graph,
)
from .theme import name_style, snr_style
from .tui.render import render_hanging, render_lines, render_to_ansi
from .pathline import PathHop, PathLine, path_line
from .tui.screen import CANCEL, ListWindow, Screen
from .widgets import (
    _DEFAULT_GLYPH,
    _NODE_GLYPHS,
    _age_seconds,
    _recency_style,
    format_ago,
    highlighted_hash,
    node_type_legend,
    tab_strip,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext

#: Our own node's marker glyph and hue, matching the map/atlas star.
_SELF_GLYPH = ("★", "#facc15")

#: The inline location preview's row bounds: it grows into whatever the Info tab's
#: viewport spares (the vitals and action rows are short), floored so a cramped terminal
#: still shows a recognisable neighbourhood and capped so a tall one doesn't become all map.
_MAP_MIN_ROWS = 5
_MAP_MAX_ROWS = 13

#: Route-graph tuning for the Routes tab. It draws the contact on the left and us on the
#: right (node → us, so the graph reads left to right as the inbound direction its packets
#: travelled to reach us). A well-connected node offers several candidate paths at once, and
#: cramming them into a short box compresses the lanes together — so this page keeps the shared
#: lane pitch and lets the graph grow with its lanes, up to this ceiling. The real budget is
#: struck per render against the terminal: the stage takes what the viewport leaves after the
#: pinned chrome, the action rows, and the route list's guaranteed window — so the graph
#: compresses its pitch before it would ever push the list or the actions off the screen, and
#: this ceiling is only reached on a terminal tall enough to afford it.
_PATH_MAX_ROWS = 22

#: The fewest canvas rows the route graph is ever granted (the shared renderer's own floor);
#: on a terminal too short for even this, the frame's cursor-follow keeps the active row in
#: view rather than the stage shrinking into noise.
_GRAPH_MIN_ROWS = 5

#: Route-list lines the stage must leave room for before taking the rest of the viewport —
#: enough for a couple of routes (or one wrapped one) to show beside the graph they light.
_LIST_MIN_LINES = 4

#: A drawn route is dropped as *stale* when its freshest-limiting link — the stalest hop it
#: rides through — has not been heard in this many days. A route is only as current as its
#: weakest-heard link: if any hop along it has gone quiet, the whole chain may no longer
#: carry. Matches the spirit of the topology's one-week evidence half-life (four half-lives
#: leaves a link weighted ~1/16), the point past which a path is more memory than fact.
_PATH_STALE_DAYS = 28.0

#: A drawn alternative route is dropped as an *outlier* when its evidence score falls below
#: this fraction of the strongest observed alternative's. Keeps the graph to the handful of
#: routes actually worth trusting rather than every far-weaker chain the evidence can string
#: together. The best-evidence (white) route is always drawn regardless — it is the answer to
#: "how do we reach it", not one of the alternatives being weighed.
_PATH_OUTLIER_RATIO = 0.25

#: Cells the labelled info rows reserve for their label lane, so the value blocks line up
#: and a wrapped value hangs under itself rather than under the label (the app-wide
#: hanging-indent rule). Sized to the widest label the block uses ("packets").
_LABEL_LANE = 9

#: The selected route's edge colour (white, the spine) over the alternatives' grey, and the
#: grey a node's label fades to when it sits on no part of the selected route. One grey for
#: both, so an off-route relay's line and its name read as the same "not this route" dim.
_WHITE = (255, 255, 255)
_GREY = (120, 120, 120)

#: The synthetic node-id prefix a contracted bidirectional cluster draws under. A ``\x00``
#: lead keeps it non-hex (so the graph never tries to coalesce it against a hash) and clear of
#: any real hop, mirroring the graph's own endpoint sentinels.
_CLUSTER_NODE = "\x00clu"


@dataclass(slots=True)
class _Action:
    """One action row at the foot of a tab.

    Attributes:
        key: The token the screen resolves with when this row is committed.
        glyph: The leading icon.
        glyph_style: The icon's style.
        label: The row's text (a current value inlined, muted where it's context).
    """

    key: str
    glyph: str
    glyph_style: str
    label: str


@dataclass(slots=True)
class _Route:
    """One selectable route on the Routes tab: how it draws, how it traces, how it reads.

    Attributes:
        draw: The relay hops in the graph's inbound draw order (contact → us), so a
            :class:`~meshterm.ui.pathgraph.PathLayer` built from them lands the contact on
            the left endpoint and our star on the right. Empty = a straight zero-hop shot.
        spec: The forced-path spec a trace arms on when this route is selected (the symmetric
            round trip through these hops); ``""`` lets the trace screen auto-resolve.
        row: The pre-rendered route line — the whole path named through THE path widget,
            contact → relays → us, with the bottleneck SNR / sample count and a ``★ best`` or
            ``device route`` tag trailing. Shown hanging-wrapped, so a long route lines up
            under itself.
    """

    draw: tuple[str, ...]
    spec: str
    row: Text


@dataclass(slots=True)
class _Cluster:
    """A contracted bidirectional cluster's stand-in marker on the graph.

    Three or more repeaters that relay each other in every order are a knot the left-to-right
    flow can't seat (see :func:`~meshterm.ui.pathgraph.bidir_clusters`); they draw as one
    super-node instead, and this is how it presents. The route rows below the graph still name
    every member in order, so the detail the marker folds away is one glance down.

    Attributes:
        glyph: The marker glyph — the members' shared node-type mark when they agree
            (``▲`` repeater, ``■`` room, …), else the plain node dot.
        color: The marker glyph's ``#rrggbb`` colour (the node-type hue, or the plain dot's).
        label: The count-and-type label, e.g. ``3 repeaters`` (``n nodes`` when mixed).
        rgb: The label colour — the same type hue as the marker, so the super-node reads as a
            typed group rather than a named node.
    """

    glyph: str
    color: str
    label: str
    rgb: tuple[int, int, int]


@dataclass(slots=True)
class _RoutesView:
    """The Routes tab's selectable routes and the shared per-node draw callbacks, or a note.

    The routes are drawn as one fan (best-evidence spine plus alternatives); which one lights
    white — and which nodes keep their name hue rather than fading to grey — follows the
    screen's live selection, so the layers and label colours are composed per render rather
    than baked in here.

    Attributes:
        routes: The selectable routes (strongest first), or empty when there is no route
            evidence at all — then ``note`` carries the muted stand-in.
        glyph_of: Per-node marker callback for the graph.
        label_of: Per-node label callback for the graph.
        label_rgb_of: Per-node label-colour callback (the un-muted base; the screen fades the
            off-route nodes over it).
        legend: Whether to draw the node-type key beneath the graph (a typed relay showed).
        note: The muted line shown instead of a graph when there is no evidence.
    """

    routes: list[_Route] = field(default_factory=list)
    glyph_of: Optional[GlyphOf] = None
    label_of: Optional[LabelOf] = None
    label_rgb_of: Optional[LabelRgbOf] = None
    legend: bool = False
    note: str = ""


@dataclass(slots=True)
class _Tab:
    """One tab in the stage's strip.

    Attributes:
        name: The strip label (``Info`` / ``Routes``).
        kind: Which stage it draws (``info`` / ``routes``).
    """

    name: str
    kind: str


class NodeDetailScreen(Screen):
    """A full-screen page for one node: identity, a tabbed stage, and the ways in.

    A read-and-route view. It renders already-resolved display data (see
    :func:`open_node_detail`, which assembles it) and, on Enter, resolves the highlighted
    action's token for the opener to act on (a Trace token is paired with
    :meth:`selected_spec`); Esc resolves :data:`CANCEL` to leave.

    Two axes of navigation, matching the app's spatial feel: ``Tab``/``Shift+Tab`` switches
    which view fills the stage, and ``↑↓`` moves the cursor *within* the active tab — through its route list (on the Routes tab, the selection drives the
    graph highlight and Enter arms a trace on the picked route) and action rows. The page
    itself never scrolls: the identity header, tab strip, and stage are pinned, the stage
    is sized to the viewport, and the route list windows itself into the leftover rows —
    ``PgUp/PgDn`` page the cursor through it, ``Home/End`` jump it to the ends.
    """

    floating = False

    def __init__(
        self,
        *,
        title: str,
        header: Text,
        info_rows: list[tuple[str, Text]],
        tabs: list[_Tab],
        minimap: Optional[MiniMap] = None,
        map_caption: Optional[Text] = None,
        routes: Optional[_RoutesView] = None,
        info_actions: Optional[list[_Action]] = None,
        trace_action: Optional[_Action] = None,
        tail_actions: Optional[list[_Action]] = None,
    ) -> None:
        """Build the page over resolved display data.

        Args:
            title: The screen heading (``Node — <name>``).
            header: The identity line: type glyph, the coloured name, its type label.
            info_rows: ``(label, value)`` pairs for the Info tab's vitals block; each
                renders as a muted label lane with the value hanging under itself when it
                wraps.
            tabs: The stage tabs to offer, in strip order (empty = no stage, just actions).
            minimap: The Info tab's inline location preview, or ``None`` (no advertised
                fix).
            map_caption: A faint line under the preview (its centre/scale), when a map shows.
            routes: The Routes tab's route list + graph callbacks (or a muted note), or
                ``None`` when there is no Routes tab (we never overhear our own node).
            info_actions: The Info tab's action rows (Open full map when there is a fix,
                Time machine when the recorder holds history, Share contact when the full
                key is known).
            trace_action: The Routes tab's ``Trace — auto route …`` action, shown only when
                there are no routes to list (with routes listed, Enter on a route row is the
                trace entry point), or ``None``.
            tail_actions: The always-available actions closing every tab (Back).
        """
        super().__init__()
        self.title = title
        self._header = header
        self._info_rows = info_rows
        self._tabs = tabs
        self._minimap = minimap
        self._map_caption = map_caption
        self._routes = routes
        self._info_actions = info_actions or []
        self._trace_action = trace_action
        self._tail_actions = tail_actions or []
        self._tab_index = 0
        self._row_index = 0
        #: The highlighted route on the Routes tab (drives the graph); tracks the cursor as
        #: it moves onto a route row and holds while it rests on an action row, so the graph
        #: keeps showing the last pick.
        self._route_sel = 0
        self._cursor: Optional[int] = None
        #: The route list's window state: the first visible row, the rows the last fit
        #: carried (the PgUp/PgDn stride), and whether any rows are hidden (gates the
        #: footer's scroll atom).
        self._list_top = 0
        self._list_page = 1
        self._list_hidden = False

    # --- input -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Tab switch (when there are tabs to switch), move, open, list paging, Esc last."""
        parts: list[str] = []
        if len(self._tabs) >= 2:
            parts.append("Tab/⇧Tab switch")
        parts.extend(("↑↓ move", "Enter open"))
        if self._list_hidden:
            parts.append("PgUp/PgDn scroll")
        parts.append("Esc back")
        return " · ".join(parts)

    def selected_spec(self) -> str:
        """The forced-path spec of the currently-highlighted route (``""`` = auto).

        The opener reads this after a ``trace`` token resolves, so a trace arms on whichever
        route the cursor had picked rather than always the best one.
        """
        routes = self._routes.routes if self._routes is not None else []
        if routes:
            sel = self._route_sel if 0 <= self._route_sel < len(routes) else 0
            return routes[sel].spec
        return ""

    def consume_edge_scrub(self) -> int:
        """Scrub the panel's right edge only while the Info tab is showing its braille map."""
        if self._minimap is None or not self._tabs:
            return 0
        return 2 if self._tabs[self._tab_index].kind == "info" else 0

    def handle(self, action: str, data: str = "") -> None:
        """Switch tab, move the cursor within a tab, commit a row, page the list, or leave."""
        focus = self._focusables()
        n = len(focus)
        if action == "enter":
            if n:
                kind, payload = focus[self._row_index % n]
                if kind == "path":
                    # A route row is itself the trace entry point: Enter arms a trace on the
                    # route it names (the opener reads :meth:`selected_spec` for the pick).
                    self.resolve("trace")
                else:
                    assert isinstance(payload, _Action)
                    # Back leaves the page exactly as Esc does — it resolves the same
                    # CANCEL the opener's loop breaks on, not a "back" token the loop would
                    # ignore and re-show the page over.
                    self.resolve(CANCEL if payload.key == "back" else payload.key)
        elif action == "tab":
            self._switch_tab(1)
        elif action == "shift_tab":
            self._switch_tab(-1)
        elif action == "up":
            if n:
                self._row_index = (self._row_index - 1) % n
                self._sync_route_sel(focus)
        elif action == "down":
            if n:
                self._row_index = (self._row_index + 1) % n
                self._sync_route_sel(focus)
        elif action == "pageup":
            if n:
                self._row_index = max(0, self._row_index - max(1, self._list_page))
                self._sync_route_sel(focus)
        elif action in ("pagedown", "space"):
            if n:
                self._row_index = min(n - 1, self._row_index + max(1, self._list_page))
                self._sync_route_sel(focus)
        elif action in ("home", "ctrl_home"):
            if n:
                self._row_index = 0
                self._sync_route_sel(focus)
        elif action in ("end", "ctrl_end"):
            if n:
                self._row_index = n - 1
                self._sync_route_sel(focus)
        elif action == "escape":
            self.resolve(CANCEL)

    def cursor_line(self) -> Optional[int]:
        """The highlighted row's body line — the frame keeps it in view, which only matters
        on a terminal too short for the pinned layout's minimums."""
        return self._cursor

    def _switch_tab(self, delta: int) -> None:
        """Move the active tab, resetting the cursor and list window to that tab's top."""
        if len(self._tabs) < 2:
            return
        self._tab_index = (self._tab_index + delta) % len(self._tabs)
        self._row_index = 0
        self._route_sel = 0
        self._list_top = 0
        self.scroll_to_top()
        self._sync_route_sel(self._focusables())

    def _sync_route_sel(self, focus: list[tuple[str, object]]) -> None:
        """Point the graph highlight at the route under the cursor, when it rests on one."""
        if focus:
            kind, payload = focus[self._row_index % len(focus)]
            if kind == "path":
                assert isinstance(payload, int)
                self._route_sel = payload

    def _focusables(self) -> list[tuple[str, object]]:
        """The active tab's cursor stops: ``("path", route_idx)`` and ``("action", _Action)``.

        On the Routes tab the route rows come first — each is itself the trace entry
        point — with the auto-route Trace action standing in only when there are no routes
        to list; the Info tab offers its own actions (Open full map, Time machine). The
        shared tail (Back) closes every tab.
        """
        tab = self._tabs[self._tab_index] if self._tabs else None
        focus: list[tuple[str, object]] = []
        if tab is not None and tab.kind == "routes" and self._routes is not None:
            focus.extend(("path", i) for i in range(len(self._routes.routes)))
            if not self._routes.routes and self._trace_action is not None:
                focus.append(("action", self._trace_action))
        elif tab is not None and tab.kind == "info":
            focus.extend(("action", a) for a in self._info_actions)
        focus.extend(("action", a) for a in self._tail_actions)
        return focus

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the pinned chrome, the sized-to-fit stage, and the windowed cursor rows.

        The viewport the frame recorded (:meth:`~meshterm.ui.tui.screen.Screen.note_viewport`)
        is split three ways each paint: the pinned chrome (identity header, tab strip) and
        the action rows take their fixed lines first, the stage takes what it needs of the
        rest (the route graph's row ceiling shrinks to fit, the Info tab's location preview
        grows into what its rows spare), and the route list windows itself into the leftover
        lines — so nothing here ever pushes the graph or the actions off the screen.
        """
        viewport = self._scroll_viewport
        focus = self._focusables()
        if focus:
            self._row_index %= len(focus)
        self._cursor = None
        self._list_hidden = False

        # -- pinned chrome: the identity header, then the tab strip (a lone tab collapses
        # to one line; a multi-tab strip boxes the active tab across three). The budget
        # below is struck from ``len(lines)`` after this, so either shape sizes correctly.
        lines: list[str] = []
        lines.extend(render_lines(self._header, width))
        if self._tabs:
            lines.append("")
            lines.extend(
                render_lines(
                    tab_strip([t.name for t in self._tabs], self._tab_index, width),
                    width,
                    no_wrap=True,
                )
            )

        # The action rows are fixed chrome too — a leading blank, one line per row, a blank
        # setting Back apart when rows precede it — struck before the stage draws so it can
        # size against them.
        actions = [payload for _kind, payload in focus if _kind == "action"]
        action_lines = 1 + len(actions) + (
            1
            if len(actions) > 1
            and any(a.key == "back" for a in actions if isinstance(a, _Action))
            else 0
        )

        # -- the stage, sized to what the viewport leaves, closed by a faint rule.
        route_blocks: list[list[str]] = []
        tab = self._tabs[self._tab_index] if self._tabs else None
        if tab is not None:
            if tab.kind == "info":
                budget = viewport - len(lines) - action_lines - 1  # the rule's line
                lines.extend(self._info_stage(width, budget))
            else:
                route_blocks = [
                    self._route_row_lines(route, i == self._row_index, width)
                    for i, route in enumerate(self._routes.routes if self._routes else [])
                ]
                if not route_blocks:
                    lines.append("")  # the bare note pays for its own air under the strip
                budget = viewport - len(lines) - action_lines - 1  # the rule's line
                lines.extend(self._routes_stage(width, budget, route_blocks))
            # The rule closes the stage, so the drawn view and the rows below it read as
            # separate bands rather than one run-on column.
            lines.append(render_to_ansi(Text("─" * width, style="faint"), width))

        # -- the route list, windowed into whatever the stage left.
        if route_blocks:
            window = max(1, viewport - len(lines) - action_lines)
            heights = [len(block) for block in route_blocks]
            on_route = self._row_index if self._row_index < len(route_blocks) else None
            top, count = self._fit_blocks(heights, window, on_route)
            self._list_page = max(1, count)
            if top > 0:
                lines.append(render_to_ansi(ListWindow.marker(top, "above"), width))
            for i in range(top, top + count):
                if i == self._row_index:
                    self._cursor = len(lines)
                lines.extend(route_blocks[i])
            below = len(route_blocks) - top - count
            if below > 0:
                lines.append(render_to_ansi(ListWindow.marker(below, "below"), width))
            self._list_hidden = top > 0 or below > 0

        # -- the pinned action rows (the route rows precede them in focus order).
        lines.append("")
        base = len(route_blocks)
        for j, payload in enumerate(actions):
            assert isinstance(payload, _Action)
            if payload.key == "back" and j > 0:
                lines.append("")  # set the exit row apart, as the menus do
            selected = base + j == self._row_index
            if selected:
                self._cursor = len(lines)
            lines.append(self._action_line(payload, selected, width))

        self._scroll_total = max(1, len(lines))
        return lines

    def _fit_blocks(self, heights: list[int], win: int, index: Optional[int]) -> tuple[int, int]:
        """Settle the route-list window over variable-height rows: ``(top, count)`` to draw.

        The hanging-wrap sibling of :meth:`~meshterm.ui.tui.screen.ListWindow.fit`: each row
        is a whole block of rendered lines (a wrapped route never splits mid-hang), the faint
        edge markers eat a window line exactly when rows hide beyond them, and the cursor's
        row (``index``, or ``None`` while it rests on the action rows) is walked into view.

        Args:
            heights: Rendered line count per route row.
            win: Lines the window may spend — on content and markers alike.
            index: The cursor's route row to keep visible, or ``None`` to just clamp.
        """
        n = len(heights)
        if sum(heights) <= win:
            self._list_top = 0
            return 0, n
        top = max(0, min(self._list_top, n - 1))
        if index is not None:
            top = min(top, index)
        while True:
            above = 1 if top > 0 else 0
            count = _fill(heights, top, win - above)
            if top + count < n:  # rows hide below: the marker takes one of the lines
                count = max(1, _fill(heights, top, win - above - 1))
            if index is None or index < top + count:
                break
            top += 1  # walk the window down until the cursor's row is inside
        self._list_top = top
        return top, count

    def _info_stage(self, width: int, budget: int) -> list[str]:
        """The vitals block and, with a fix, the location preview grown to fit.

        ``budget`` is the viewport lines left for the whole stage; the vitals rows never
        truncate — it is the preview that flexes, taking whatever they and its caption
        leave, clamped to ``[_MAP_MIN_ROWS, _MAP_MAX_ROWS]``.
        """
        lines: list[str] = [""]  # the stage's one line of air under the strip
        for label, value in self._info_rows:
            lines.extend(
                render_hanging(
                    Text(f"{label:<{_LABEL_LANE}}", style="muted"),
                    value,
                    width,
                    indent=_LABEL_LANE,
                )
            )
        if self._minimap is not None:
            lines.append("")
            caption = 1 if self._map_caption is not None else 0
            rows = max(_MAP_MIN_ROWS, min(_MAP_MAX_ROWS, budget - len(lines) - caption))
            lines.extend(self._minimap.render(width, rows))
            if self._map_caption is not None:
                lines.extend(render_lines(self._map_caption, width, no_wrap=True))
        return lines

    def _routes_stage(
        self, width: int, budget: int, route_blocks: list[list[str]]
    ) -> list[str]:
        """The route-graph fan, sized to fit — or the muted note when there is no evidence.

        ``budget`` is what the viewport leaves for the stage *and* the route list together;
        the graph's row ceiling is what remains after its caption/legend and the list's
        guaranteed minimum (the lesser of :data:`_LIST_MIN_LINES` and what the rows actually
        need), floored at :data:`_GRAPH_MIN_ROWS` — so the graph compresses its lane pitch
        before the list would lose its window, and a busy node's fan only spreads out on a
        terminal tall enough to afford it. The selected route draws white with its off-route
        labels faded, as ever.
        """
        rv = self._routes
        assert rv is not None
        if not rv.routes or rv.glyph_of is None:
            return render_lines(Text(rv.note or "no route observed yet", style="muted"), width)
        sel = self._route_sel if 0 <= self._route_sel < len(rv.routes) else 0
        # Priority is fixed by evidence order (route 0, the best-evidence route, is always the
        # spine) so the fan's geometry never moves as the selection changes — only emphasis
        # (which route is drawn white and on top) and the label muting below follow the pick.
        layers = [
            PathLayer(
                hops=route.draw,
                color=_WHITE if i == sel else _GREY,
                priority=len(rv.routes) - i,
                emphasis=1 if i == sel else 0,
            )
            for i, route in enumerate(rv.routes)
        ]
        # A node keeps its name hue only while it sits on the selected route (its endpoints
        # always do); every other node in the fan fades its label to the same grey its line
        # went, so the picture reads as *this* route rather than the whole tangle.
        # Membership is decided in the graph's own id space: the widget folds a short hop
        # into the wide id it can only be among these routes (a 1-byte trace hop beside the
        # same relay's full contact id), so the raw draw hops would grey the very marker the
        # selected line rides through.
        on_route = set(_coalesce_prefixes(layers)[sel].hops) | {SRC_NODE, DST_NODE}
        base_rgb = rv.label_rgb_of
        assert base_rgb is not None

        def label_rgb_of(node: str):
            return base_rgb(node) if node in on_route else _GREY

        caption_lines = 1 + (1 if rv.legend else 0)
        list_need = min(sum(len(block) for block in route_blocks), _LIST_MIN_LINES)
        max_rows = max(_GRAPH_MIN_ROWS, min(_PATH_MAX_ROWS, budget - caption_lines - list_need))
        lines = list(
            render_path_graph(
                layers,
                width,
                glyph_of=rv.glyph_of,
                label_of=rv.label_of,  # type: ignore[arg-type]
                label_rgb_of=label_rgb_of,
                max_rows=max_rows,
            )
        )
        caption = Text("node → you, as heard  ·  white = selected route", style="faint")
        lines.extend(render_lines(caption, width, no_wrap=True))
        if rv.legend:
            lines.extend(render_lines(node_type_legend(), width, no_wrap=True))
        return lines

    def _route_row_lines(self, route: _Route, selected: bool, width: int) -> list[str]:
        """One route row: a ``❯`` pointer when picked, the route hanging-wrapped under itself.

        The route keeps its per-node colours even when selected (the pointer, and the white
        line in the graph above, carry the selection) — unlike the plain action rows, which
        go fully brand, since here the colour *is* the content. The trailing ``…`` is the
        app-wide opens-further-prompts mark: Enter on the row arms a trace on it.
        """
        prefix = Text("❯ " if selected else "  ", style="brand" if selected else "")
        row = route.row.copy()
        row.append(" …", style="muted")
        return render_hanging(prefix, row, width, indent=2)

    def _action_line(self, action: _Action, selected: bool, width: int) -> str:
        """One action row: ``❯`` + icon + label, the whole row brand when it is the cursor's."""
        text = Text("❯ " if selected else "  ", style="brand" if selected else "")
        if action.glyph:
            text.append(f"{action.glyph} ", style=action.glyph_style)
        text.append(action.label)
        if selected:
            text.style = "brand"
        text.no_wrap = True
        text.truncate(width, overflow="ellipsis")
        return render_to_ansi(text, width)


def _fill(heights: list[int], top: int, budget: int) -> int:
    """How many whole blocks from ``top`` fit into ``budget`` lines (greedy, in order)."""
    used = 0
    count = 0
    for h in heights[top:]:
        if used + h > budget:
            break
        used += h
        count += 1
    return count


# -- geographic helpers --------------------------------------------------------

_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two coordinates, in kilometres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """The 8-point compass direction from ``(lat1, lon1)`` toward ``(lat2, lon2)``."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _COMPASS[round(deg / 45) % 8]


def _range_text(
    lat: float, lon: float, self_lat: Optional[float], self_lon: Optional[float]
) -> Text:
    """A location value: the coordinates, plus range + bearing from us when we're placed."""
    text = Text(f"{lat:.4f}, {lon:.4f}", style="")
    if self_lat is not None and self_lon is not None:
        km = _distance_km(self_lat, self_lon, lat, lon)
        dist = f"{km * 1000:.0f} m" if km < 1 else f"{km:.1f} km"
        text.append(f"  ·  {dist} {_bearing(self_lat, self_lon, lat, lon)}", style="muted")
    return text


def _as_float(value: object) -> Optional[float]:
    """Best-effort float coercion for the raw lat/lon a device reports (``None`` on junk)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _located(lat: Optional[float], lon: Optional[float]) -> bool:
    """Whether a coordinate is a real, plottable fix (see :func:`~meshterm.core.geo.usable_fix`).

    Rejects both the 0/0 null-island a no-GPS node reports and the out-of-range nonsense a
    misconfigured companion sometimes advertises (``lat -97, lon -1042`` seen in the wild),
    either of which would drop the location preview onto an all-black off-world view.
    """
    return lat is not None and lon is not None and usable_fix(lat, lon)


# -- data gathering + the action loop -----------------------------------------


async def open_node_detail(ctx: "AppContext", contact: Optional["Contact"]) -> None:
    """Open the Node detail page for a contact (or our own node) and run its action loop.

    Assembles the page from stored history, the device's contacts, and the observed
    topology — identity, reception stats, a location preview, and the observed routes — then
    loops: show the page, run whatever action the user commits (trace, full map, time
    machine), and show it again, until Esc backs out. This is the same show/act/reshow loop
    the Time Machine and Contacts list use.

    Args:
        ctx: The shared application context (must be running the interactive TUI).
        contact: The contact to detail, or ``None`` for our own node (an identity-and-ledger
            page — we never overhear ourselves, so there is no reception history to show).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services.trace_runner import (
        make_name_key_resolver,
        make_node_resolver,
        make_node_type_resolver,
    )
    from ..services.topology import build_topology, _is_hex
    from ..tools.map import gather_markers
    from .config_editor import show_contact_card
    from .map_screen import basemap_source, open_map
    from .surface import TuiUi
    from .timemachine_screen import open_timemachine_node, open_timemachine_self
    from .trace_screen import _collapse_trace_width, open_trace
    from .widgets import route_graph_style

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the node detail screen is only available in the menu")
    session = ctx.ui.session

    you = contact is None

    # -- our own node, for the "us" label, the location preview centre, and range/bearing.
    try:
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - the page is useful without our own identity/fix
        info = {}
    self_name = str(info.get("name") or "this node")
    self_key = str(info.get("public_key") or "")
    self_lat, self_lon = _as_float(info.get("adv_lat")), _as_float(info.get("adv_lon"))
    if not _located(self_lat, self_lon):
        self_lat = self_lon = None

    # -- the device's contacts (for name/type/key resolution) and the routing hash width.
    try:
        contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - resolution just falls back to stored data
        contacts = []
    try:
        mode = int(await ctx.devstate.path_hash_mode())
        prefix_bytes = (mode + 1) if 0 <= mode <= 3 else 0
        width_bytes = _collapse_trace_width(mode)
    except Exception:  # noqa: BLE001 - optional reads; sane defaults keep the page working
        prefix_bytes, width_bytes = 0, 1

    stored_names = ctx.repo.node_names()
    # One hash→name resolver for the whole page: the route list and the route graph all name
    # their hops through it (contacts first, recorder history behind).
    resolve = make_node_resolver(contacts, stored_names)

    if you:
        node_id = (self_key.lower().removeprefix("0x")[:12]) or ""
        name: Optional[str] = self_name
        key = self_key
        node_type = info.get("adv_type")
        lat, lon = self_lat, self_lon
    else:
        assert contact is not None
        key = contact.public_key or contact.key_prefix or ""
        node_id = key.lower().removeprefix("0x")[:12]
        name = contact.name or None
        node_type = contact.node_type
        lat = contact.lat if _located(contact.lat, contact.lon) else None
        lon = contact.lon if _located(contact.lat, contact.lon) else None

    # -- reception stats + first-heard from the recorder's history.
    heard = {n.node: n for n in ctx.repo.heard_nodes() if n.node}
    hn = heard.get(node_id)
    firsts = {n: when for n, _n, when in ctx.repo.first_seen()}
    first_heard = firsts.get(node_id)
    if you:
        hn = None  # we never overhear ourselves — no reception history to show
    # A node with a fix but no observation-derived location still shows the map — but only
    # when that overheard fix is itself real (a no-GPS/nonsense advert never places it).
    if lat is None and hn is not None and hn.has_location and _located(hn.lat, hn.lon):
        lat, lon = hn.lat, hn.lon

    label = name or node_id or key or "?"

    # -- identity header.
    if you:
        glyph, glyph_style = _SELF_GLYPH
        name_hue = "you"
    else:
        glyph, glyph_style = _NODE_GLYPHS.get(node_type, _DEFAULT_GLYPH)
        name_hue = name_style(name, key) if name else "muted"
    header = Text()
    header.append(f"{glyph} ", style=glyph_style)
    header.append(name or "unknown", style=name_hue)
    type_label = NODE_TYPE_LABELS.get(node_type) if node_type is not None else None
    if you:
        header.append("   your node", style="muted")
    elif type_label:
        header.append(f"   {type_label}", style="muted")

    # -- the observed topology: the suggested best path and the routes to draw.
    target_hash = key.lower().removeprefix("0x")
    target_hash = target_hash if (not you and _is_hex(target_hash)) else None
    topo = build_topology(
        self_id=(self_key.lower().removeprefix("0x")[:12]) or "local",
        contacts=contacts,
        trace_paths=ctx.repo.trace_paths(),
        packet_paths=ctx.repo.packet_paths(),
        neighbour_links=ctx.repo.neighbour_links(),
    )
    device_route: Optional[tuple[str, ...]] = None
    if not you and contact is not None and contact.route_hops is not None:
        device_route = tuple(topo.canonical(h) or h for h in contact.route_hops)
    canonical_target = (
        (topo.canonical(target_hash) or target_hash[:12]) if target_hash else None
    )
    suggested = topo.suggested(canonical_target) if canonical_target else None
    scenarios = (
        topo.scenarios(canonical_target, device_route=device_route)
        if canonical_target
        else []
    )

    # -- the info block (identity + how-heard + where; the routes fold into the Routes tab).
    info_rows: list[tuple[str, Text]] = []
    if key:
        info_rows.append(("key", highlighted_hash(key, prefix_bytes)))
    else:
        info_rows.append(("key", Text("?", style="muted")))
    if not you:
        secs = _age_seconds(hn.last_seen if hn else (contact.last_seen if contact else None))
        heard_val = Text(format_ago(secs), style=_recency_style(secs))
        if first_heard is not None:
            heard_val.append(
                f"  ·  first {first_heard.astimezone():%b %d %Y}", style="muted"
            )
        info_rows.append(("heard", heard_val))
        # A node never overheard reads a faint em-dash, as the contact list draws it.
        packets = Text(str(hn.count)) if hn else Text("—", style="faint")
        info_rows.append(("packets", packets))
        signal = _signal_row(hn)
        if signal is not None:
            info_rows.append(("signal", signal))
    if lat is not None and lon is not None:
        info_rows.append(("where", _range_text(lat, lon, self_lat, self_lon)))

    # -- the location preview (only when the node advertised a fix).
    minimap: Optional[MiniMap] = None
    map_caption: Optional[Text] = None
    markers: list = []
    if lat is not None and lon is not None:
        try:
            markers = await gather_markers(ctx)
        except Exception:  # noqa: BLE001 - a preview of just this node is still useful
            markers = []
        source = basemap_source(ctx)
        try:
            max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
        except Exception:  # noqa: BLE001 - offline: markers on a blank grid
            max_zoom = 14
        minimap = MiniMap(
            session, source, max_zoom,
            center_lat=lat, center_lon=lon, zoom=min(13, max_zoom), markers=markers,
        )
        map_caption = Text(f"{label} · centred here", style="faint")

    # -- the "routes heard" route list + graph (node → us), best-evidence path white.
    routes_view: Optional[_RoutesView] = None
    if not you:
        routes_view = _routes_view(
            topo, scenarios, suggested, device_route, canonical_target, target_hash, width_bytes,
            resolve=resolve,
            type_of=make_node_type_resolver(contacts),
            key_of=make_name_key_resolver(contacts, stored_names),
            style=route_graph_style,
            self_name=self_name,
            node_label=label,
            name_key=key,
        )

    # -- the tabs (Info always; Routes only when there is a routes view) and their actions.
    tabs: list[_Tab] = [_Tab(name="Info", kind="info")]
    if routes_view is not None:
        tabs.append(_Tab(name="Routes", kind="routes"))

    info_actions: list[_Action] = []
    if minimap is not None:
        info_actions.append(_Action("map", "🌍", "", "Open full map"))
    if you:
        info_actions.append(_Action("timemachine", "⏳", "", "Time machine — your activity"))
    elif hn is not None:
        info_actions.append(
            _Action("timemachine", "⏳", "", f"Time machine — {hn.count} receptions")
        )
    # The share card encodes the full 32-byte key; a prefix-only contact (heard but never
    # synced from the device) has nothing scannable to offer, so the row only shows when
    # the whole key is known.
    full_key = key.lower().removeprefix("0x")
    if len(full_key) == 64 and _is_hex(full_key):
        info_actions.append(_Action("share", "📱", "", "Share contact — QR / link"))
    try:
        adv_type = int(node_type) if node_type is not None else 1
    except (TypeError, ValueError):
        adv_type = 1
    # With routes listed, each route row is its own trace entry point (Enter arms it); the
    # dedicated action only stands in when there is no route evidence to list.
    trace_action: Optional[_Action] = None
    if not you and node_id and not (routes_view is not None and routes_view.routes):
        trace_action = _Action("trace", "🎯", "", "Trace — auto route …")
    tail_actions = [_Action("back", "", "", "Back")]

    title = f"Node — {label}" if not you else f"Node — {label} (you)"
    while True:
        screen = NodeDetailScreen(
            title=title,
            header=header,
            info_rows=info_rows,
            tabs=tabs,
            minimap=minimap,
            map_caption=map_caption,
            routes=routes_view,
            info_actions=info_actions,
            trace_action=trace_action,
            tail_actions=tail_actions,
        )
        action = await session.run_screen(screen)
        if action is CANCEL or action is None:
            break
        if action == "trace":
            await open_trace(ctx, name or key, initial_spec=screen.selected_spec())
        elif action == "map":
            if markers:
                # Open centred on this node — the very spot the inline preview showed —
                # not wherever the global map was last left — with the find filter seeded
                # to it, so this node lights among the rest. Seed only when its label
                # actually matches a marker; a needle nothing matches would dim everything.
                # (The map action only exists when the node has a fix, so lat/lon are set.)
                needle = label.casefold()
                await open_map(
                    ctx,
                    markers,
                    focus=(lat, lon),
                    find=label if any(needle in m.label.casefold() for m in markers) else None,
                )
        elif action == "share":
            await show_contact_card(ctx, label, full_key, adv_type)
        elif action == "timemachine":
            if you:
                await open_timemachine_self(ctx)
            else:
                await open_timemachine_node(ctx, node_id, label)
    if minimap is not None:
        # The preview's braille may have smeared the terminal (double-width fallback
        # glyphs prompt_toolkit's diff can't see); force one clean repaint of the list
        # underneath, exactly as the full map does on the way out.
        session.request_full_repaint()


def _signal_row(hn) -> Optional[Text]:  # noqa: ANN001 - Optional[HeardNode]
    """Median/best reception SNR and last RSSI, or ``None`` when nothing was measured."""
    if hn is None:
        return None
    text = Text()
    if hn.median_snr is not None:
        text.append("median ", style="muted")
        text.append(f"{hn.median_snr:+.1f} dB", style=snr_style(hn.median_snr))
        if hn.best_snr is not None:
            text.append("  ·  best ", style="muted")
            text.append(f"{hn.best_snr:+.1f} dB", style=snr_style(hn.best_snr))
    if hn.last_rssi is not None:
        if text.plain:
            text.append("  ·  ", style="muted")
        text.append(f"RSSI {hn.last_rssi:.0f} dBm", style="muted")
    return text if text.plain else None


def _route_line(
    node_label: str,
    name_key: str,
    hops_out: tuple[str, ...],
    tag: str,
    weakest: Optional[float],
    samples: int,
    *,
    resolve,
    self_name: Optional[str],
) -> Text:
    """One route rendered as a full line: contact → relays → us, then its context and tag.

    The whole route reads left to right in the graph's own direction (contact on the left, us
    on the right), so the row and the drawn line cross-read. The line is one
    :class:`~meshterm.ui.pathline.PathLine` — the contact and our own node anchoring the two
    ends in their own hues (the contact's key-derived, us the white ``you``), the relays at a
    1-byte hash width, each ``name (3d)`` — the trace presentation, as powerline chips where
    the terminal can draw them. The bottleneck SNR and sample count trail as muted context,
    and a ``★ best`` / ``device route`` tag marks the winner and the firmware's learned route.
    """
    relays = path_line(
        list(reversed(hops_out)), resolve, prefix_bytes=1, self_name=self_name,
        show_hash=True, hash_bytes=1,
    )
    text = PathLine([
        PathHop(node_label, key=name_key),
        *relays.hops,
        PathHop(self_name or "us", you=True),
    ]).text()
    if weakest is not None:
        text.append("  ·  weakest ", style="muted")
        text.append(f"{weakest:+.1f} dB", style=snr_style(weakest))
    if samples:
        text.append(f"  ·  {samples}×", style="muted")
    if tag == "best":
        text.append("   ★ best", style="brand")
    elif tag == "device":
        text.append("   device route", style="accent")
    return text


def _cluster_presentation(members: tuple[str, ...], type_of) -> _Cluster:  # noqa: ANN001
    """The marker + label a contracted bidirectional cluster draws under.

    A homogeneous cluster (every member the same node type) wears that type's map marker and
    hue — ``3 repeaters`` under a ``▲`` — so it reads as a group of that kind at a glance; a
    mixed one falls back to the plain node dot and ``n nodes``.
    """
    types = {type_of(m) for m in members}
    only = next(iter(types)) if len(types) == 1 else None
    if only is not None:
        glyph, color = _NODE_GLYPHS.get(only, _DEFAULT_GLYPH)
        kind = NODE_TYPE_LABELS.get(only, "node")
    else:
        glyph, color = _DEFAULT_GLYPH
        kind = "node"
    return _Cluster(glyph=glyph, color=color, label=f"{len(members)} {kind}s", rgb=parse_hex(color))


def _contract_bidir_clusters(
    routes: list[_Route], type_of
) -> tuple[list[_Route], dict[str, _Cluster]]:  # noqa: ANN001
    """Fold each 3+ bidirectional cluster in the routes' draw sequences to one super-node.

    A dense knot of mutually-relaying repeaters is a strongly-connected component the flow
    graph can't order (see :func:`~meshterm.ui.pathgraph.bidir_clusters`); left as-is every
    member collapses onto one jammed column. So each such cluster is contracted to a single
    synthetic node: every route's ``draw`` has its members rewritten to the one cluster id
    (consecutive members merged), and the returned map gives each cluster its marker and label.

    Only ``draw`` — the graph geometry — changes. Each route's spelled-out ``row`` and its
    trace ``spec`` keep every member named in order, so the list under the graph still carries
    the exact ordering the single marker can't, and a trace still arms on the real path.

    Returns ``(routes, clusters)`` unchanged (and an empty map) when there is no such knot.
    """
    groups = bidir_clusters([(SRC_NODE, *route.draw, DST_NODE) for route in routes])
    if not groups:
        return routes, {}
    member_of: dict[str, str] = {}
    clusters: dict[str, _Cluster] = {}
    for i, members in enumerate(groups):
        cid = f"{_CLUSTER_NODE}{i}"
        clusters[cid] = _cluster_presentation(members, type_of)
        for member in members:
            member_of[member] = cid
    contracted: list[_Route] = []
    for route in routes:
        draw: list[str] = []
        for hop in route.draw:
            cid = member_of.get(hop, hop)
            if draw and draw[-1] == cid:
                continue  # a run of members through the same cluster is one stop
            draw.append(cid)
        contracted.append(replace(route, draw=tuple(draw)))
    return contracted, clusters


def _routes_view(
    topo,
    scenarios,
    suggested,
    device_route,
    canonical_target,
    target_hash,
    width_bytes,
    *,
    resolve,
    type_of,
    key_of,
    style,
    self_name,
    node_label,
    name_key,
) -> _RoutesView:
    """Build the Routes tab's selectable routes (node → us) + graph callbacks, or a muted note.

    The routes are drawn contact-on-the-left to us-on-the-right — the *inbound* direction the
    packets travelled to reach us, since everything the graph knows was received, not sent.
    That is exactly the orientation the shared :func:`~meshterm.ui.widgets.route_graph_style`
    already draws (it was built for the Message paths view, where traffic arrives *at* us on
    the right), so we hand it the target as its ``source`` — the left endpoint — and use its
    callbacks as they come, only reversing each route's hop order so the drawn line runs from
    the contact inward to us.

    The selectable list, strongest first, folds the old ``route`` / ``suggest`` info rows into
    the tab: the best-evidence route (the observed suggestion, else the firmware's learned
    route, else a bare direct shot), then the firmware's learned route when it is something
    different, then the *good* observed alternatives. Only good alternatives are kept: an
    observed route survives when its evidence is both **fresh** (its stalest hop heard within
    :data:`_PATH_STALE_DAYS`) and **not an outlier** (its score within
    :data:`_PATH_OUTLIER_RATIO` of the strongest observed one) — so the list is the routes
    worth trusting rather than every chain ever heard. With no route evidence at all — no
    learned route, no observed path, not even a direct link — there is nothing honest to draw,
    so a muted note stands in and the tab still offers an auto trace.

    Which route lights white (and which nodes keep their name hue) is the screen's live
    selection, applied per render; this only assembles the routes and the base callbacks.
    """
    from ..services.topology import render_forced_spec

    if canonical_target is None:
        return _RoutesView(note="no key to route to")
    has_evidence = (
        device_route is not None
        or any(s.source == "observed" for s in scenarios)
        or topo.link(topo.self_id, canonical_target) is not None
    )
    if not has_evidence:
        return _RoutesView(note="no route observed yet — trace to discover one")

    best_hops = (
        suggested.hops if suggested is not None
        else device_route if device_route is not None
        else None
    )
    # A node we only ever hear directly (no relays, no learned route) still earns a line —
    # the straight zero-hop shot — so the graph shows the direct link rather than a bare note.
    if best_hops is None and topo.link(topo.self_id, canonical_target) is not None:
        best_hops = ()

    # The ordered, de-duplicated selectable routes (outbound hops us → target).
    entries: list[tuple[tuple[str, ...], str, Optional[float], int]] = []
    seen: set[tuple[str, ...]] = set()

    def add(hops_out: tuple[str, ...], tag: str, weakest: Optional[float], samples: int) -> None:
        key = tuple(hops_out)
        if key in seen:
            return
        seen.add(key)
        entries.append((key, tag, weakest, samples))

    if best_hops is not None:
        if suggested is not None:
            add(best_hops, "best", suggested.weakest_snr, suggested.samples)
        else:
            add(best_hops, "best", None, 0)
    if device_route is not None:
        add(device_route, "device", None, 0)
    for scenario in _good_alternatives(topo, scenarios, canonical_target):
        add(scenario.hops, "", scenario.weakest_snr, scenario.samples)

    routes: list[_Route] = []
    for hops_out, tag, weakest, samples in entries:
        spec = render_forced_spec(hops_out, target_hash, width_bytes) if target_hash else ""
        row = _route_line(
            node_label, name_key, hops_out, tag, weakest, samples,
            resolve=resolve, self_name=self_name,
        )
        routes.append(_Route(draw=tuple(reversed(hops_out)), spec=spec, row=row))

    # The legend explains the typed relay marks, so it is decided on the members' real types —
    # before contraction folds a dense cluster's members behind one synthetic id.
    legend = any(type_of(h) is not None for route in routes for h in route.draw)
    # Fold any 3+ bidirectional cluster (a knot the flow can't order) to one super-node, so it
    # draws as a single marker rather than piling its members onto one column. Each route's row
    # and spec keep the members named in order — only the drawn geometry contracts.
    routes, clusters = _contract_bidir_clusters(routes, type_of)

    glyph_of, byte_label_of, label_rgb_of = style(
        resolve=resolve, self_name=self_name, source=node_label, type_of=type_of, key_of=key_of
    )

    # This tab names every node in the graph, not just the two ends. Where the Message paths
    # graph tags a relay with only its first hash byte, here each relay wears its resolved
    # contact name (its colour is already the name's hue), so the whole route reads as places
    # rather than hex; an unidentified relay keeps the byte, the honest most it can be called.
    # A contracted cluster wears its own count-and-type label. The two endpoints keep
    # route_graph_style's names (target on the left, us on the right).
    def label_of(node: str) -> Optional[str]:
        cluster = clusters.get(node)
        if cluster is not None:
            return cluster.label
        if node in (SRC_NODE, DST_NODE):
            return byte_label_of(node)
        named = resolve(node)
        return named if named and named != node else node[:2]

    base_rgb_of = label_rgb_of

    def cluster_label_rgb_of(node: str) -> RGB:
        cluster = clusters.get(node)
        return cluster.rgb if cluster is not None else base_rgb_of(node)

    # The target wears its own map glyph (▲ repeater, ■ room, ◉ sensor) — the same mark the
    # header and the map give it. route_graph_style draws the far (left) endpoint as a plain
    # dot, since on its home screen (Message paths) that end is an arbitrary message origin;
    # here it is a known contact whose type we can show. A contracted cluster draws its own
    # type mark; everything else keeps the style's glyph.
    base_glyph_of = glyph_of

    def cluster_glyph_of(node: str) -> tuple[str, str]:
        cluster = clusters.get(node)
        return (cluster.glyph, cluster.color) if cluster is not None else base_glyph_of(node)

    glyph_of = _with_target_glyph(
        cluster_glyph_of, _NODE_GLYPHS.get(type_of(canonical_target), _DEFAULT_GLYPH)
    )
    return _RoutesView(
        routes=routes,
        glyph_of=glyph_of,
        label_of=label_of,
        label_rgb_of=cluster_label_rgb_of,
        legend=legend,
    )


def _good_alternatives(topo, scenarios, target) -> list:  # noqa: ANN001
    """The observed alternative routes worth drawing: fresh, evidence-backed, not outliers.

    Trims the raw scenario list down to the alternatives the graph should show as grey
    alternatives:

    * only the **observed** family (the device/direct scenarios are not routes the evidence
      *observed* the node arrive over — the device route rides on its own row, and a bare
      direct line the evidence never saw is not worth a lane);
    * only ones with real evidence behind them (a positive score — an unobserved link scores
      zero, see :meth:`~meshterm.services.topology.MeshTopology._score_route`);
    * only **fresh** ones — every hop heard within :data:`_PATH_STALE_DAYS`, so a route whose
      weakest link has gone quiet drops out rather than lingering as a line that may no longer
      carry;
    * only ones **not far weaker** than the best observed alternative (score within
      :data:`_PATH_OUTLIER_RATIO` of the strongest), so one clearly-best route isn't buried
      under a fan of marginal ones.

    Returns the survivors in the order :meth:`~meshterm.services.topology.MeshTopology.scenarios`
    ranked them (strongest first).
    """
    observed = [s for s in scenarios if s.source == "observed" and s.hops and s.score > 0]
    if not observed:
        return []
    best_score = max(s.score for s in observed)
    now = utcnow()
    return [
        s
        for s in observed
        if s.score >= best_score * _PATH_OUTLIER_RATIO
        and _route_is_fresh(topo, s.hops, target, now)
    ]


def _route_is_fresh(topo, hops: tuple[str, ...], target: str, now: datetime) -> bool:
    """Whether every link along ``us → hops… → target`` was heard within the stale horizon.

    A route is only as current as its stalest hop: if any link on it has not been heard in
    :data:`_PATH_STALE_DAYS`, the chain may no longer carry, so the whole route counts as
    stale. A link with no timestamp (evidence that carries no ``when``, e.g. a firmware route)
    is treated as fresh — we have no age to hold against it.
    """
    chain = [topo.self_id, *hops, target]
    for a, b in zip(chain, chain[1:]):
        link = topo.link(a, b)
        if link is None:
            return False
        last = link.last_seen
        if last is None or getattr(last, "tzinfo", None) is None:
            continue  # undated evidence — no age to judge it stale by
        if (now - last).total_seconds() > _PATH_STALE_DAYS * 86400.0:
            return False
    return True


def _with_target_glyph(glyph_of: GlyphOf, target_glyph: tuple[str, str]) -> GlyphOf:
    """Wrap the graph's glyph callback so the target endpoint draws its own node glyph.

    The graph's left endpoint (``SRC_NODE``) is the node this page is about, and here we know
    its type — so it draws the map's own mark for that type (``▲`` repeater, ``■`` room,
    ``◉`` sensor, ``●`` plain), matching the identity header and the location preview, rather
    than ``route_graph_style``'s generic origin dot. Every other node passes through
    untouched (our own star on the right endpoint keeps the style's ``you`` mark).
    """
    def wrapped(node: str) -> tuple[str, str]:
        return target_glyph if node == SRC_NODE else glyph_of(node)

    return wrapped
