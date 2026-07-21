"""The Node detail screen: one node's whole story on a single page, and the ways into it.

Reached by pressing Enter on any contact in the Contacts list (see
:mod:`~meshterm.ui.contacts_screen`). Where the list is one aligned row per node, this is
the node itself, in full:

* **who it is** — its name in the node's own hue, its key with the routing hash lit, its
  type, when it was first and last heard, how many packets we've overheard, its reception
  SNR (median and best) and last RSSI, and where it sits;
* **where it is** — a small static basemap preview (see :class:`~meshterm.ui.minimap.MiniMap`)
  centred on the node with the rest of the mesh around it, when the node has advertised a
  location;
* **how we hear it** — the routes we've actually heard it arrive over, drawn on the shared
  route graph (:mod:`~meshterm.ui.pathgraph`) node→us (the inbound direction the packets
  travelled, contact on the left, us on the right), the best-evidence one lit white over the
  alternatives. Only *good* routes are drawn — stale evidence and far-weaker outliers are
  dropped, so the graph shows the routes worth trusting rather than every chain ever heard;
* **the ways in** — an action list: *Trace target* (armed with the suggested best path, so
  Enter walks straight to it), *Open full map*, and *Time machine* (only when the recorder
  actually holds history for the node). ↑/↓ move the cursor, Enter commits, Esc backs to the
  list.

The screen is a pure read-and-route view: it renders already-resolved display data and
resolves an action token; :func:`open_node_detail` owns the data-gathering and runs the
sub-flows each action opens, then re-shows the page — the same loop the Time Machine and
Contacts list use. Nothing here transmits.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from rich.text import Text

from ..core.geo import EARTH_RADIUS_KM, usable_fix
from ..core.models import NODE_TYPE_LABELS, Contact, utcnow
from .minimap import MiniMap
from .pathgraph import (
    DST_NODE,
    SRC_NODE,
    GlyphOf,
    LabelOf,
    LabelRgbOf,
    PathLayer,
    render_path_graph,
)
from .theme import name_style, snr_style
from .tui.render import render_hanging, render_lines, render_to_ansi
from .tui.screen import CANCEL, Screen
from .widgets import (
    _DEFAULT_GLYPH,
    _NODE_GLYPHS,
    _age_seconds,
    _recency_style,
    format_ago,
    highlighted_hash,
    node_type_legend,
    path_text,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext

#: Our own node's marker glyph and hue, matching the map/atlas star.
_SELF_GLYPH = ("★", "#facc15")

#: How many character rows the inline location preview draws.
_MAP_ROWS = 7

#: Route-graph tuning for this page. It draws the contact on the left and us on the right
#: (node → us, so the graph reads left to right as the inbound direction its packets
#: travelled to reach us). A well-connected node offers several candidate paths at once, and
#: cramming them into a short box overlaps the lanes into an unreadable tangle — so this page
#: gives the fan a generous lane step and row budget, letting each path spread into its own
#: clearly separated track. The height is adaptive: it grows only with the number of distinct
#: lanes, so a node with one or two routes still draws compact while a busy one earns the room
#: it needs. The page scrolls (PgUp/PgDn), so a tall graph never crowds the action rows off.
_PATH_LANE_STEP = 15
_PATH_MAX_ROWS = 22

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


@dataclass(slots=True)
class _Action:
    """One action row at the foot of the page.

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
class _PathView:
    """The route-graph layers and their shared per-node draw callbacks, or a bare note.

    Attributes:
        layers: The paths to draw (best-evidence one highest priority), or empty when
            there is no route evidence at all — then ``note`` carries the muted stand-in.
        glyph_of: Per-node marker callback for the graph.
        label_of: Per-node label callback for the graph.
        label_rgb_of: Per-node label-colour callback for the graph.
        legend: Whether to draw the node-type key beneath the graph (a typed relay showed).
        note: The muted line shown instead of a graph when there is no evidence.
    """

    layers: list[PathLayer] = field(default_factory=list)
    glyph_of: Optional[GlyphOf] = None
    label_of: Optional[LabelOf] = None
    label_rgb_of: Optional[LabelRgbOf] = None
    legend: bool = False
    note: str = ""


class NodeDetailScreen(Screen):
    """A full-screen page for one node: identity, location, routes, and the ways in.

    A read-and-route view. It renders already-resolved display data (see
    :func:`open_node_detail`, which assembles it) and, on Enter, resolves the highlighted
    action's token for the opener to act on; Esc resolves :data:`CANCEL` to leave. ↑/↓ move
    the action cursor (its row is kept in view while navigating); PgUp/PgDn/Home/End scroll
    the whole page, so the location preview and route graph can be read on a short terminal
    without losing the controls.
    """

    floating = False

    def __init__(
        self,
        *,
        title: str,
        header: Text,
        info_rows: list[tuple[str, Text]],
        actions: list[_Action],
        minimap: Optional[MiniMap] = None,
        map_caption: Optional[Text] = None,
        path: Optional[_PathView] = None,
    ) -> None:
        """Build the page over resolved display data.

        Args:
            title: The screen heading (``Node — <name>``).
            header: The identity line: type glyph, the coloured name, its type label.
            info_rows: ``(label, value)`` pairs for the fixed info block; each renders as
                a muted label lane with the value hanging under itself when it wraps.
            actions: The action rows, in display order (a ``back`` row set apart at the end).
            minimap: The inline location preview, or ``None`` when the node has no location.
            map_caption: A faint line under the preview (its centre/scale), when a map shows.
            path: The route-graph view (layers + callbacks, or a muted note), or ``None`` to
                omit the whole "Routes heard" section (we never overhear our own node).
        """
        super().__init__()
        self.title = title
        self._header = header
        self._info_rows = info_rows
        self._actions = actions
        self._minimap = minimap
        self._map_caption = map_caption
        self._path = path
        self._index = 0
        self._pin_cursor = False  # open showing the top; only pin once ↑/↓ are used
        self._cursor: Optional[int] = None

    # --- input -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Navigation, commit, scroll, then Esc last."""
        return "↑↓ actions · Enter open · PgUp/PgDn scroll · Esc back"

    def consume_edge_scrub(self) -> int:
        """Scrub the panel's right edge while a braille map shows (the same smear the map has)."""
        return 2 if self._minimap is not None else 0

    def handle(self, action: str, data: str = "") -> None:
        """Move the action cursor, commit it, scroll the page, or leave."""
        if action == "enter":
            self.resolve(self._actions[self._index].key)
        elif action == "up":
            self._index = (self._index - 1) % len(self._actions)
            self._pin_cursor = True
        elif action == "down":
            self._index = (self._index + 1) % len(self._actions)
            self._pin_cursor = True
        elif action == "pageup":
            self._pin_cursor = False
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self._pin_cursor = False
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self._pin_cursor = False
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self._pin_cursor = False
            self.scroll_to_bottom()
        elif action == "escape":
            self.resolve(CANCEL)

    def cursor_line(self) -> Optional[int]:
        """Keep the highlighted action visible while ↑/↓ are in use; free scroll otherwise."""
        return self._cursor if self._pin_cursor else None

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the identity block, location preview, route graph, and action list."""
        lines: list[str] = []
        lines.extend(render_lines(self._header, width))
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
            lines.extend(render_lines(Text("Location", style="accent"), width))
            lines.extend(self._minimap.render(width, _MAP_ROWS))
            if self._map_caption is not None:
                lines.extend(render_lines(self._map_caption, width, no_wrap=True))
        if self._path is not None:
            lines.append("")
            lines.extend(render_lines(Text("Routes heard", style="accent"), width))
            lines.extend(self._path_lines(width))
        lines.append("")
        self._cursor = None
        for i, action in enumerate(self._actions):
            if action.key == "back":
                lines.append("")  # set the exit row apart, as the menus do
            selected = i == self._index
            text = Text("❯ " if selected else "  ", style="brand" if selected else "")
            if action.glyph:
                text.append(f"{action.glyph} ", style=action.glyph_style)
            text.append(action.label)
            if selected:
                text.style = "brand"
                self._cursor = len(lines)
            text.no_wrap = True
            text.truncate(width, overflow="ellipsis")
            lines.append(render_to_ansi(text, width))
        self._scroll_total = max(1, len(lines))
        return lines

    def _path_lines(self, width: int) -> list[str]:
        """The route graph (best-evidence path white over the rest), or the muted note."""
        path = self._path
        assert path is not None  # only called when a path section exists
        if not path.layers or path.glyph_of is None:
            return render_lines(Text(path.note or "no route observed yet", style="muted"), width)
        lines = render_path_graph(
            path.layers,
            width,
            glyph_of=path.glyph_of,
            label_of=path.label_of,  # type: ignore[arg-type]
            label_rgb_of=path.label_rgb_of,  # type: ignore[arg-type]
            max_rows=_PATH_MAX_ROWS,
            lane_step=_PATH_LANE_STEP,
        )
        caption = Text("node → you, as heard  ·  white = best route", style="faint")
        lines.extend(render_lines(caption, width, no_wrap=True))
        if path.legend:
            lines.extend(render_lines(node_type_legend(), width, no_wrap=True))
        return lines


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
    from ..services.topology import build_topology, render_forced_spec, _is_hex
    from ..tools.map import gather_markers
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
    # One hash→name resolver for the whole page: the route/suggest rows and the route
    # graph all name their hops through it (contacts first, recorder history behind).
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
    # The spec the Trace action arms on: the observed best path, else the firmware's
    # learned route, else nothing (let the trace screen auto-resolve).
    if target_hash and suggested is not None:
        suggested_spec = render_forced_spec(suggested.hops, target_hash, width_bytes)
    elif target_hash and device_route is not None:
        suggested_spec = render_forced_spec(device_route, target_hash, width_bytes)
    else:
        suggested_spec = ""

    # -- the info block.
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
    if not you:
        info_rows.append(("route", _route_row(device_route, resolve=resolve, self_name=self_name)))
        info_rows.append(("suggest", _suggest_row(suggested, resolve=resolve, self_name=self_name)))

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

    # -- the "routes heard" route graph (node → us), best-evidence path white.
    path: Optional[_PathView] = None
    if not you:
        path = _path_view(
            topo, scenarios, suggested, device_route, canonical_target,
            resolve=resolve,
            type_of=make_node_type_resolver(contacts),
            key_of=make_name_key_resolver(contacts, stored_names),
            style=route_graph_style,
            self_name=self_name,
            node_label=label,
        )

    # -- the action rows.
    def build_actions() -> list[_Action]:
        rows: list[_Action] = []
        if not you and node_id:
            rows.append(_Action("trace", "🎯", "", _trace_label(suggested_spec)))
        if minimap is not None:
            rows.append(_Action("map", "🌍", "", "Open full map"))
        if you:
            rows.append(_Action("timemachine", "⏳", "", "Time machine — your activity"))
        elif hn is not None:
            rows.append(_Action("timemachine", "⏳", "", f"Time machine — {hn.count} receptions"))
        rows.append(_Action("back", "", "", "Back"))
        return rows

    title = f"Node — {label}" if not you else f"Node — {label} (you)"
    while True:
        screen = NodeDetailScreen(
            title=title,
            header=header,
            info_rows=info_rows,
            actions=build_actions(),
            minimap=minimap,
            map_caption=map_caption,
            path=path,
        )
        action = await session.run_screen(screen)
        if action is CANCEL or action is None:
            break
        if action == "trace":
            await open_trace(ctx, name or key, initial_spec=suggested_spec)
        elif action == "map":
            if markers:
                await open_map(ctx, markers)
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


def _route_row(
    device_route: Optional[tuple[str, ...]], *, resolve, self_name: Optional[str]
) -> Text:
    """The firmware's learned route to the node, or a note that it floods.

    The hops render through THE path widget (:func:`~meshterm.ui.widgets.path_text`) at a
    1-byte hash width, so each reads ``name (3d)`` — the same name the route graph prints
    beside its markers, plus the short hash it travels under, so row and graph cross-read.
    """
    if device_route is None:
        return Text("no learned route — floods", style="muted")
    if not device_route:
        return Text("direct neighbour (zero-hop route)", style="")
    text = Text("device route", style="accent")
    text.append("  via ", style="muted")
    text.append_text(_hop_path(device_route, resolve, self_name))
    return text


def _suggest_row(suggested, *, resolve, self_name: Optional[str]) -> Text:  # noqa: ANN001
    """The observed best path (the "suggest best path" answer), or a muted stand-in.

    Like :func:`_route_row`, the hops render through THE path widget named, with a 1-byte
    hash in parentheses (the short id each hop travels under); the bottleneck SNR and sample
    count trail behind as context.
    """
    if suggested is None:
        return Text("none observed yet — trace to learn one", style="muted")
    text = (
        _hop_path(suggested.hops, resolve, self_name)
        if suggested.hops
        else Text("direct", style="brand")
    )
    if suggested.weakest_snr is not None:
        text.append("  ·  weakest ", style="muted")
        text.append(f"{suggested.weakest_snr:+.1f} dB", style=snr_style(suggested.weakest_snr))
    if suggested.samples:
        text.append(f"  ·  {suggested.samples}×", style="muted")
    return text


def _hop_path(hops: tuple[str, ...], resolve, self_name: Optional[str]) -> Text:
    """A hop sequence rendered through THE path widget, each hop tagged with its first byte."""
    return path_text(
        list(hops), resolve, prefix_bytes=1, self_name=self_name,
        show_hash=True, hash_bytes=1,
    )


def _trace_label(suggested_spec: str) -> str:
    """The Trace action's row label — naming whether it arms on a suggested path or auto."""
    return "Trace target — suggested path" if suggested_spec else "Trace target — auto route"


def _path_view(
    topo,
    scenarios,
    suggested,
    device_route,
    canonical_target,
    *,
    resolve,
    type_of,
    key_of,
    style,
    self_name,
    node_label,
) -> _PathView:
    """Build the route-graph layers (node → us) and their draw callbacks, or a muted note.

    The routes are drawn contact-on-the-left to us-on-the-right — the *inbound* direction the
    packets travelled to reach us, since everything the graph knows was received, not sent.
    That is exactly the orientation the shared :func:`~meshterm.ui.widgets.route_graph_style`
    already draws (it was built for the Message paths view, where traffic arrives *at* us on
    the right), so we hand it the target as its ``source`` — the left endpoint — and use its
    callbacks as they come, only reversing each route's hop order so the drawn line runs from
    the contact inward to us. The best-evidence route (the observed suggestion, else the
    firmware's learned route) is lit white over the alternatives' grey.

    Only *good* alternatives are drawn: an observed route is kept as grey when its evidence is
    both **fresh** (its stalest hop heard within :data:`_PATH_STALE_DAYS`) and **not an
    outlier** (its score within :data:`_PATH_OUTLIER_RATIO` of the strongest observed one) —
    so the graph shows the routes worth trusting rather than every chain ever heard. The
    best-evidence route is always drawn, stale or not: it is the page's answer to how we'd
    reach the node, not one of the alternatives being weighed. With no route evidence at all —
    no learned route, no observed path, not even a direct link — there is nothing honest to
    draw, so a muted note stands in.
    """
    if canonical_target is None:
        return _PathView(note="no key to route to")
    has_evidence = (
        device_route is not None
        or any(s.source == "observed" for s in scenarios)
        or topo.link(topo.self_id, canonical_target) is not None
    )
    if not has_evidence:
        return _PathView(note="no route observed yet — trace to discover one")

    white, grey = (255, 255, 255), (120, 120, 120)
    best_hops = (
        suggested.hops if suggested is not None
        else device_route if device_route is not None
        else None
    )
    # A node we only ever hear directly (no relays, no learned route) still earns a line —
    # the straight zero-hop shot — so the graph shows the direct link rather than a bare note.
    if best_hops is None and topo.link(topo.self_id, canonical_target) is not None:
        best_hops = ()
    layers: list[PathLayer] = []
    seen: set[tuple[str, ...]] = set()

    def add(hops: tuple[str, ...], priority: int, color: tuple[int, int, int]) -> None:
        # Reverse the outbound (us-outward) hop order into inbound (node→us) draw order, so
        # the contact lands on the left endpoint and our star on the right.
        drawn = tuple(reversed(hops))
        if drawn in seen:
            return
        seen.add(drawn)
        layers.append(PathLayer(hops=drawn, color=color, priority=priority))

    if best_hops is not None:
        add(best_hops, 3, white)
    for scenario in _good_alternatives(topo, scenarios, canonical_target):
        add(scenario.hops, 2, grey)

    glyph_of, byte_label_of, label_rgb_of = style(
        resolve=resolve, self_name=self_name, source=node_label, type_of=type_of, key_of=key_of
    )

    # This page names every node, not just the two ends. Where the Message paths graph tags
    # a relay with only its first hash byte, here each relay wears its resolved contact name
    # (its colour is already the name's hue), so the whole route reads as places rather than
    # hex; an unidentified relay keeps the byte, the honest most it can be called. The two
    # endpoints keep route_graph_style's names (target on the left, us on the right).
    def label_of(node: str) -> Optional[str]:
        if node in (SRC_NODE, DST_NODE):
            return byte_label_of(node)
        named = resolve(node)
        return named if named and named != node else node[:2]

    # The target wears its own map glyph (▲ repeater, ■ room, ◉ sensor) — the same mark the
    # header and the map give it. route_graph_style draws the far (left) endpoint as a plain
    # dot, since on its home screen (Message paths) that end is an arbitrary message origin;
    # here it is a known contact whose type we can show.
    glyph_of = _with_target_glyph(
        glyph_of, _NODE_GLYPHS.get(type_of(canonical_target), _DEFAULT_GLYPH)
    )
    legend = any(type_of(h) is not None for layer in layers for h in layer.hops)
    return _PathView(
        layers=layers,
        glyph_of=glyph_of,
        label_of=label_of,
        label_rgb_of=label_rgb_of,
        legend=legend,
    )


def _good_alternatives(topo, scenarios, target) -> list:  # noqa: ANN001
    """The observed alternative routes worth drawing: fresh, evidence-backed, not outliers.

    Trims the raw scenario list down to the alternatives the graph should show in grey:

    * only the **observed** family (the device/direct scenarios are not routes the evidence
      *observed* the node arrive over — the device route rides in white on its own when it is
      the best, and a bare direct line the evidence never saw is not worth a lane);
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
