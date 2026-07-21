"""Node detail screen tests: the mini-map, the page render/navigation, and its helpers.

The screen is a pure render-and-route view, so these drive it headless — display data is
built by hand (the way :func:`~meshterm.ui.node_detail_screen.open_node_detail` assembles
it) and rendered/keyed against a fake session, the same approach as the trace and time
machine screen tests. The topology-driven helpers are exercised against a real (in-memory)
evidence graph.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from rich.text import Text

from meshterm.core.models import Contact, utcnow
from meshterm.services.topology import build_topology
from meshterm.services.trace_runner import (
    make_name_key_resolver,
    make_node_resolver,
    make_node_type_resolver,
)
from meshterm.ui.map_render import MapMarker
from meshterm.ui.minimap import MiniMap
from meshterm.ui.node_detail_screen import (
    NodeDetailScreen,
    _Action,
    _PathView,
    _bearing,
    _distance_km,
    _path_view,
    _range_text,
    _route_row,
    _signal_row,
    _suggest_row,
)
from meshterm.ui.tui.screen import CANCEL
from meshterm.ui.widgets import highlighted_hash, route_graph_style

US = "aaaaaaaaaaaa"
HUB = Contact(name="Hub", public_key="3d63c6429436" + "0" * 52, key_prefix="3d63c6429436",
              node_type=2)
FAR = Contact(name="Far", public_key="f2c24f54551e" + "0" * 52, key_prefix="f2c24f54551e")


def _plain(lines: list[str]) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines))


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


class _OfflineSource:
    """A tile source with no basemap, so the mini-map plots markers on a blank grid."""

    available = False

    def load_tile(self, z: int, x: int, y: int):  # noqa: ANN201
        return None


# --- the mini-map ---------------------------------------------------------------


def test_minimap_offline_renders_markers_on_a_blank_grid() -> None:
    """With no basemap the preview still fills its box and plots the node's marker."""
    mini = MiniMap(
        _FakeSession(), _OfflineSource(), 14,
        center_lat=45.5, center_lon=-73.6, zoom=13,
        markers=[MapMarker(label="Hub", lat=45.5, lon=-73.6, is_repeater=True, key="3d63aa")],
    )
    lines = mini.render(40, 6)
    assert len(lines) == 6  # the canvas fills exactly the requested row box
    assert mini.pending == 0 and mini.has_basemap is False  # offline: no fetches scheduled
    assert any(ch != " " for ch in _plain(lines))  # the marker drew something


def test_minimap_clamps_zoom_to_the_source_ceiling() -> None:
    """A zoom past the source's max (plus overzoom) is clamped, never runs away."""
    mini = MiniMap(
        _FakeSession(), _OfflineSource(), 10,
        center_lat=0.0, center_lon=0.0, zoom=99, markers=[],
    )
    assert mini._zoom == 12  # max_tile_zoom (10) + overzoom (2)


# --- geographic helpers ---------------------------------------------------------


def test_distance_and_bearing_are_sane() -> None:
    """Range in km and an 8-point compass bearing between two nearby points."""
    # ~1.11 km due north (0.01° latitude), so bearing reads N.
    assert 1.0 < _distance_km(45.5, -73.6, 45.51, -73.6) < 1.2
    assert _bearing(45.5, -73.6, 45.51, -73.6) == "N"
    assert _bearing(45.5, -73.6, 45.5, -73.59) == "E"  # due east


def test_range_text_omits_bearing_without_our_own_fix() -> None:
    """The location value carries range+bearing only when we know where we are."""
    placed = _plain([_range_text(45.5, -73.6, 45.4, -73.5).plain])
    assert "45.5000, -73.6000" in placed and "km" in placed
    bare = _range_text(45.5, -73.6, None, None)
    assert "km" not in bare.plain and "45.5000" in bare.plain


def test_signal_row_summarizes_snr_and_rssi() -> None:
    """The signal row folds median/best SNR and last RSSI, or is dropped when unmeasured."""
    row = _signal_row(SimpleNamespace(median_snr=3.2, best_snr=7.0, last_rssi=-92.0))
    assert row is not None
    assert "median" in row.plain and "best" in row.plain and "RSSI -92" in row.plain
    # A node with nothing measured (or our own node) contributes no row at all.
    assert _signal_row(SimpleNamespace(median_snr=None, best_snr=None, last_rssi=None)) is None
    assert _signal_row(None) is None


# --- the topology-driven rows ---------------------------------------------------


def _topo_with_route():  # noqa: ANN202
    """A graph where Far is reached through the Hub, from repeated trace evidence."""
    from meshterm.persistence.repository import TracedPath

    walks = [
        TracedPath(when=utcnow(), hops=[("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0)])
        for _ in range(4)
    ]
    return build_topology(
        self_id=US + "0" * 52, contacts=[HUB, FAR],
        trace_paths=walks, packet_paths=[], neighbour_links=[],
    )


def test_route_and_suggest_rows_speak_the_evidence() -> None:
    """The route row names the learned route; the suggest row names the observed best path.

    Both render their hops through THE path widget at a 1-byte hash width, so each hop reads
    ``name (3d)`` — the first-byte tag the route graph prints beside its markers.
    """
    topo = _topo_with_route()
    resolve = make_node_resolver([HUB, FAR])
    learned = _plain([_route_row(("3d63c6429436",), resolve=resolve, self_name="Us").plain])
    assert "device route" in learned and "Hub" in learned
    assert "(3d)" in learned  # the row tags each hop with its first hash byte
    assert "floods" in _route_row(None, resolve=resolve, self_name="Us").plain  # no route

    best = topo.suggested("f2c24f54551e")
    suggest = _suggest_row(best, resolve=resolve, self_name="Us")
    assert "Hub" in suggest.plain and "(3d)" in suggest.plain and "×" in suggest.plain
    assert "none observed" in _suggest_row(None, resolve=resolve, self_name="Us").plain


def test_path_view_draws_evidence_and_notes_its_absence() -> None:
    """With route evidence the graph draws node→us; without any, a muted note stands in."""
    topo = _topo_with_route()
    style_args = dict(
        resolve=make_node_resolver([HUB, FAR]),
        type_of=make_node_type_resolver([HUB, FAR]),
        key_of=make_name_key_resolver([HUB, FAR]),
        style=route_graph_style,
        self_name="Us",
        node_label="Far",
    )
    view = _path_view(
        topo, topo.scenarios("f2c24f54551e", device_route=("3d63c6429436",)),
        topo.suggested("f2c24f54551e"), ("3d63c6429436",), "f2c24f54551e", **style_args,
    )
    assert view.layers and view.glyph_of is not None  # a graph, not a note
    # The Hub is a repeater, so the graph earns its node-type legend.
    assert view.legend is True

    empty = build_topology(self_id=US + "0" * 52, contacts=[FAR],
                           trace_paths=[], packet_paths=[], neighbour_links=[])
    note = _path_view(
        empty, empty.scenarios("f2c24f54551e"), None, None, "f2c24f54551e", **style_args
    )
    assert not note.layers and "no route observed" in note.note


def test_path_view_puts_the_contact_on_the_left_and_us_on_the_right() -> None:
    """The graph reads node → us (the inbound direction): the contact on the left, our star right."""
    from meshterm.ui.pathgraph import DST_NODE, SRC_NODE

    topo = _topo_with_route()
    view = _path_view(
        topo, topo.scenarios("f2c24f54551e", device_route=("3d63c6429436",)),
        topo.suggested("f2c24f54551e"), ("3d63c6429436",), "f2c24f54551e",
        resolve=make_node_resolver([HUB, FAR]),
        type_of=make_node_type_resolver([HUB, FAR]),
        key_of=make_name_key_resolver([HUB, FAR]),
        style=route_graph_style, self_name="Us", node_label="Far",
    )
    assert view.label_of(SRC_NODE) == "Far"   # the left endpoint is the target contact
    assert view.glyph_of(DST_NODE)[0] == "★"  # the right endpoint is our own star
    assert view.label_of(DST_NODE) == "Us"    # …labelled as us
    # This page names every node: a known relay reads by its contact name, not its hash byte.
    assert view.label_of("3d63c6429436") == "Hub"


def test_path_view_relay_falls_back_to_the_hash_byte_when_unnamed() -> None:
    """A relay no contact can name keeps the honest first-byte tag rather than a blank."""
    topo = _topo_with_route()
    view = _path_view(
        topo, topo.scenarios("f2c24f54551e", device_route=("abcd1234ef56",)),
        None, ("abcd1234ef56",), "f2c24f54551e",
        resolve=make_node_resolver([FAR]),  # the relay abcd… is not among the contacts
        type_of=make_node_type_resolver([FAR]),
        key_of=make_name_key_resolver([FAR]),
        style=route_graph_style, self_name="Us", node_label="Far",
    )
    assert view.label_of("abcd1234ef56") == "ab"  # unnamed → its first hash byte


def test_path_view_target_wears_its_node_type_glyph() -> None:
    """The left endpoint draws the target's own map mark (a repeater ▲), not a plain dot."""
    from meshterm.persistence.repository import TracedPath
    from meshterm.ui.pathgraph import SRC_NODE

    leaf = Contact(name="Leaf", public_key="27d4396a2967" + "0" * 52, key_prefix="27d4396a2967")
    # Reach the Hub (a repeater) through the Leaf, so the Hub is the drawn left endpoint.
    walks = [
        TracedPath(when=utcnow(), hops=[("27d4", 8.0), ("3d", 6.0), ("27d4", 6.0), (None, 8.0)])
        for _ in range(3)
    ]
    topo = build_topology(self_id=US + "0" * 52, contacts=[HUB, leaf],
                          trace_paths=walks, packet_paths=[], neighbour_links=[])
    view = _path_view(
        topo, topo.scenarios("3d63c6429436"), topo.suggested("3d63c6429436"),
        None, "3d63c6429436",
        resolve=make_node_resolver([HUB, leaf]),
        type_of=make_node_type_resolver([HUB, leaf]),
        key_of=make_name_key_resolver([HUB, leaf]),
        style=route_graph_style, self_name="Us", node_label="Hub",
    )
    assert view.glyph_of(SRC_NODE)[0] == "▲"  # the repeater target keeps its own glyph


def test_path_view_draws_routes_inbound_reversing_the_hop_order() -> None:
    """A drawn route runs node → us: the outbound (us-outward) hops reverse into inbound order."""
    r1 = Contact(name="R1", public_key="111111111111" + "0" * 52, key_prefix="111111111111")
    r2 = Contact(name="R2", public_key="222222222222" + "0" * 52, key_prefix="222222222222")
    topo = build_topology(self_id=US + "0" * 52, contacts=[r1, r2, FAR],
                          trace_paths=[], packet_paths=[], neighbour_links=[])
    route = ("111111111111", "222222222222")  # us → r1 → r2 → target, outward order
    view = _path_view(
        topo, topo.scenarios("f2c24f54551e", device_route=route), None, route, "f2c24f54551e",
        resolve=make_node_resolver([r1, r2, FAR]),
        type_of=make_node_type_resolver([r1, r2, FAR]),
        key_of=make_name_key_resolver([r1, r2, FAR]),
        style=route_graph_style, self_name="Us", node_label="Far",
    )
    # Drawn contact→us, so the relay nearest the target leads and the one nearest us trails.
    assert view.layers[0].hops == ("222222222222", "111111111111")


def _topo_two_alternatives():  # noqa: ANN202
    """A graph reaching Far two ways: a strong route via Hub and a far weaker one via Alt."""
    from meshterm.persistence.repository import TracedPath

    alt = Contact(name="Alt", public_key="a1a1a1a1a1a1" + "0" * 52, key_prefix="a1a1a1a1a1a1")
    now = utcnow()
    strong = [
        TracedPath(when=now, hops=[("3d", 12.0), ("f2", 10.0), ("3d", 10.0), (None, 12.0)])
        for _ in range(8)
    ]
    weak = [TracedPath(when=now, hops=[("a1", -14.0), ("f2", -14.0), ("a1", -14.0), (None, -14.0)])]
    topo = build_topology(
        self_id=US + "0" * 52, contacts=[HUB, alt, FAR],
        trace_paths=strong + weak, packet_paths=[], neighbour_links=[],
    )
    return topo


def test_good_alternatives_keeps_observed_routes_and_drops_outliers() -> None:
    """The grey alternatives are the observed routes worth trusting — outliers and the bare
    direct/device families don't earn a lane."""
    from meshterm.ui.node_detail_screen import _good_alternatives

    topo = _topo_two_alternatives()
    scenarios = topo.scenarios("f2c24f54551e")
    kept = {s.hops for s in _good_alternatives(topo, scenarios, "f2c24f54551e")}
    assert ("3d63c6429436",) in kept          # the strong observed route survives
    assert ("a1a1a1a1a1a1",) not in kept      # the far weaker one is trimmed as an outlier
    assert () not in kept                     # the bare direct family is never a grey lane


def test_path_view_draws_a_direct_line_for_a_bare_neighbour() -> None:
    """A node only ever heard directly still draws its zero-hop line, not a muted note."""
    from meshterm.persistence.repository import PacketPath

    # A single overheard frame straight from Far to us — a direct link, no relays, no route.
    topo = build_topology(
        self_id=US + "0" * 52, contacts=[FAR],
        trace_paths=[],
        packet_paths=[PacketPath(when=utcnow(), origin="f2c24f54551e", hops=[], snr=6.0)],
        neighbour_links=[],
    )
    view = _path_view(
        topo, topo.scenarios("f2c24f54551e"), topo.suggested("f2c24f54551e"),
        None, "f2c24f54551e",
        resolve=make_node_resolver([FAR]),
        type_of=make_node_type_resolver([FAR]),
        key_of=make_name_key_resolver([FAR]),
        style=route_graph_style, self_name="Us", node_label="Far",
    )
    assert view.layers and view.layers[0].hops == ()  # a straight endpoint-to-endpoint line


def test_route_freshness_drops_a_route_with_a_long_quiet_hop() -> None:
    """A route counts as stale — and is dropped — once its stalest hop goes quiet past the horizon."""
    from datetime import timedelta

    from meshterm.persistence.repository import TracedPath
    from meshterm.ui.node_detail_screen import _route_is_fresh

    now = utcnow()
    hops = [("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0)]
    fresh = build_topology(
        self_id=US + "0" * 52, contacts=[HUB, FAR],
        trace_paths=[TracedPath(when=now, hops=hops)], packet_paths=[], neighbour_links=[],
    )
    assert _route_is_fresh(fresh, ("3d63c6429436",), "f2c24f54551e", now) is True

    stale = build_topology(
        self_id=US + "0" * 52, contacts=[HUB, FAR],
        trace_paths=[TracedPath(when=now - timedelta(days=60), hops=hops)],
        packet_paths=[], neighbour_links=[],
    )
    assert _route_is_fresh(stale, ("3d63c6429436",), "f2c24f54551e", now) is False


def test_located_accepts_real_fixes_and_rejects_junk() -> None:
    """The location preview is gated on a real fix — no null island, no out-of-range advert."""
    from meshterm.ui.node_detail_screen import _located

    assert _located(45.5, -73.6) is True
    assert _located(None, -73.6) is False  # missing coordinate
    assert _located(0.0, 0.0) is False  # null island (no-GPS sentinel)
    assert _located(-97.0, -1041.97) is False  # the Homestead out-of-range advert


# --- the screen -----------------------------------------------------------------


def _screen(**over) -> NodeDetailScreen:
    """A node detail screen over hand-built display data (no map, a bare path note)."""
    header = Text("▲ ", style="#a78bfa")
    header.append("Hub", style="accent")
    header.append("   repeater", style="muted")
    kwargs = dict(
        title="Node — Hub",
        header=header,
        info_rows=[
            ("key", highlighted_hash("3d63c6429436" + "0" * 52, 1)),
            ("heard", Text("5m ago")),
            ("packets", Text("42")),
        ],
        actions=[
            _Action("trace", "🎯", "", "Trace target — suggested path"),
            _Action("timemachine", "⏳", "", "Time machine — 42 receptions"),
            _Action("back", "", "", "Back"),
        ],
        minimap=None,
        map_caption=None,
        path=_PathView(note="no route observed yet — trace to discover one"),
    )
    kwargs.update(over)
    return NodeDetailScreen(**kwargs)


def test_node_detail_screen_renders_its_sections() -> None:
    """The page shows identity, info, the path section, and the action rows."""
    screen = _screen()
    body = _plain(screen.render_body(72))
    assert "Hub" in body and "repeater" in body  # identity
    assert "42" in body  # a packet tally from the info block
    assert "Routes heard" in body and "no route observed yet" in body
    assert "Trace target" in body and "Time machine" in body and "Back" in body
    # Every rendered line fits the 72-column standard.
    for line in screen.render_body(72):
        assert len(_plain([line])) <= 72
    assert len(screen.footer_hint) <= 72


def test_node_detail_screen_cursor_and_commit() -> None:
    """↑/↓ move the action cursor (pinned in view); Enter resolves its key, Esc cancels."""
    screen = _screen()
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]

    # Opens un-pinned (top visible) with the cursor on the first action.
    assert screen.cursor_line() is None
    screen.render_body(72)
    screen.handle("down")  # onto "Time machine"
    assert screen._pin_cursor and screen.cursor_line() is not None
    screen.render_body(72)
    screen.handle("enter")
    assert resolved == ["timemachine"]

    screen.handle("escape")
    assert resolved == ["timemachine", CANCEL]


def test_node_detail_screen_includes_the_location_section_with_a_map() -> None:
    """A located node draws the Location heading and the inline preview under it."""
    mini = MiniMap(
        _FakeSession(), _OfflineSource(), 14,
        center_lat=45.5, center_lon=-73.6, zoom=13,
        markers=[MapMarker(label="Hub", lat=45.5, lon=-73.6, key="3d63aa")],
    )
    screen = _screen(minimap=mini, map_caption=Text("Hub · centred here", style="faint"))
    body = _plain(screen.render_body(72))
    assert "Location" in body and "centred here" in body
    assert screen.consume_edge_scrub() == 2  # the braille edge gets scrubbed
