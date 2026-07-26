"""Node detail screen tests: the mini-map, the tabbed page, and its route/topology helpers.

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
    _Route,
    _RoutesView,
    _Tab,
    _bearing,
    _distance_km,
    _range_text,
    _route_line,
    _routes_view,
    _signal_row,
)
from meshterm.ui.pathgraph import DST_NODE, SRC_NODE
from meshterm.ui.tui.screen import CANCEL
from meshterm.ui.widgets import highlighted_hash, route_graph_style, tab_strip

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


# --- the tab strip --------------------------------------------------------------


def test_tab_strip_lights_the_active_tab_and_mutes_the_rest() -> None:
    """The active view reads as an accent section heading; the others sit beside it plain."""
    strip = tab_strip(["Map", "Routes"], 1)
    assert strip.plain == "Map    ── Routes ──"  # active tab bracketed, inactive bare
    lone = tab_strip(["Routes"], 0)
    assert lone.plain == "── Routes ──"  # a single tab collapses to a plain heading


# --- the folded-in route line ---------------------------------------------------


def test_route_line_reads_contact_to_us_with_relays_named() -> None:
    """A route row runs contact → relays → us, each relay named and hash-tagged, tag trailing."""
    resolve = make_node_resolver([HUB, FAR])
    line = _route_line(
        "Far", FAR.public_key, ("3d63c6429436",), "device", None, 0,
        resolve=resolve, self_name="Us",
    ).plain
    assert line.startswith("Far")  # the contact anchors the left
    assert "Hub" in line and "(3d)" in line  # the relay named + its first-byte tag
    assert line.rstrip().endswith("device route")  # the firmware-route tag trails
    assert "Us" in line  # our own node anchors the right


def test_route_line_marks_the_best_route_and_its_context() -> None:
    """The winner wears ★ best and trails its bottleneck SNR and sample count."""
    resolve = make_node_resolver([HUB, FAR])
    line = _route_line(
        "Far", FAR.public_key, ("3d63c6429436",), "best", 6.5, 4,
        resolve=resolve, self_name="Us",
    ).plain
    assert "★ best" in line and "weakest" in line and "6.5" in line and "4×" in line


def test_route_line_direct_route_has_no_relay() -> None:
    """A zero-hop route reads contact → us with nothing between them."""
    line = _route_line(
        "Far", FAR.public_key, (), "best", None, 0,
        resolve=make_node_resolver([FAR]), self_name="Us",
    ).plain
    assert "Far" in line and "Us" in line and "→" in line


# --- the routes view (list + graph callbacks) -----------------------------------


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


def _view(topo, suggested, device_route, target, node_label, contacts, name_key=None):  # noqa: ANN001
    """Build a routes view the way :func:`open_node_detail` does, over the given evidence."""
    return _routes_view(
        topo, topo.scenarios(target, device_route=device_route), suggested, device_route,
        target, target, 1,
        resolve=make_node_resolver(contacts),
        type_of=make_node_type_resolver(contacts),
        key_of=make_name_key_resolver(contacts),
        style=route_graph_style, self_name="Us", node_label=node_label,
        name_key=name_key or (target + "0" * 52),
    )


def test_routes_view_draws_evidence_and_notes_its_absence() -> None:
    """With route evidence the view carries selectable routes; without any, a muted note stands in."""
    topo = _topo_with_route()
    view = _view(topo, topo.suggested("f2c24f54551e"), ("3d63c6429436",),
                 "f2c24f54551e", "Far", [HUB, FAR])
    assert view.routes and view.glyph_of is not None  # a graph, not a note
    assert view.legend is True  # the Hub is a repeater, so the type legend is earned

    empty = build_topology(self_id=US + "0" * 52, contacts=[FAR],
                           trace_paths=[], packet_paths=[], neighbour_links=[])
    note = _routes_view(
        empty, empty.scenarios("f2c24f54551e"), None, None, "f2c24f54551e", "f2c24f54551e", 1,
        resolve=make_node_resolver([FAR]), type_of=make_node_type_resolver([FAR]),
        key_of=make_name_key_resolver([FAR]), style=route_graph_style, self_name="Us",
        node_label="Far", name_key=FAR.public_key,
    )
    assert not note.routes and "no route observed" in note.note


def test_routes_view_puts_the_contact_on_the_left_and_us_on_the_right() -> None:
    """The graph reads node → us (the inbound direction): the contact on the left, our star right."""
    topo = _topo_with_route()
    view = _view(topo, topo.suggested("f2c24f54551e"), ("3d63c6429436",),
                 "f2c24f54551e", "Far", [HUB, FAR])
    assert view.label_of(SRC_NODE) == "Far"   # the left endpoint is the target contact
    assert view.glyph_of(DST_NODE)[0] == "★"  # the right endpoint is our own star
    assert view.label_of(DST_NODE) == "Us"    # …labelled as us
    # This page names every node: a known relay reads by its contact name, not its hash byte.
    assert view.label_of("3d63c6429436") == "Hub"


def test_routes_view_relay_falls_back_to_the_hash_byte_when_unnamed() -> None:
    """A relay no contact can name keeps the honest first-byte tag rather than a blank."""
    topo = _topo_with_route()
    view = _view(topo, None, ("abcd1234ef56",), "f2c24f54551e", "Far", [FAR])
    assert view.label_of("abcd1234ef56") == "ab"  # unnamed → its first hash byte


def test_routes_view_target_wears_its_node_type_glyph() -> None:
    """The left endpoint draws the target's own map mark (a repeater ▲), not a plain dot."""
    from meshterm.persistence.repository import TracedPath

    leaf = Contact(name="Leaf", public_key="27d4396a2967" + "0" * 52, key_prefix="27d4396a2967")
    # Reach the Hub (a repeater) through the Leaf, so the Hub is the drawn left endpoint.
    walks = [
        TracedPath(when=utcnow(), hops=[("27d4", 8.0), ("3d", 6.0), ("27d4", 6.0), (None, 8.0)])
        for _ in range(3)
    ]
    topo = build_topology(self_id=US + "0" * 52, contacts=[HUB, leaf],
                          trace_paths=walks, packet_paths=[], neighbour_links=[])
    view = _view(topo, topo.suggested("3d63c6429436"), None, "3d63c6429436", "Hub",
                 [HUB, leaf], name_key=HUB.public_key)
    assert view.glyph_of(SRC_NODE)[0] == "▲"  # the repeater target keeps its own glyph


def test_routes_view_draws_routes_inbound_reversing_the_hop_order() -> None:
    """A drawn route runs node → us: the outbound (us-outward) hops reverse into inbound order."""
    r1 = Contact(name="R1", public_key="111111111111" + "0" * 52, key_prefix="111111111111")
    r2 = Contact(name="R2", public_key="222222222222" + "0" * 52, key_prefix="222222222222")
    topo = build_topology(self_id=US + "0" * 52, contacts=[r1, r2, FAR],
                          trace_paths=[], packet_paths=[], neighbour_links=[])
    route = ("111111111111", "222222222222")  # us → r1 → r2 → target, outward order
    view = _view(topo, None, route, "f2c24f54551e", "Far", [r1, r2, FAR])
    # Drawn contact→us, so the relay nearest the target leads and the one nearest us trails.
    assert view.routes[0].draw == ("222222222222", "111111111111")


def test_routes_view_draws_a_direct_line_for_a_bare_neighbour() -> None:
    """A node only ever heard directly still draws its zero-hop line, not a muted note."""
    from meshterm.persistence.repository import PacketPath

    # A single overheard frame straight from Far to us — a direct link, no relays, no route.
    topo = build_topology(
        self_id=US + "0" * 52, contacts=[FAR],
        trace_paths=[],
        packet_paths=[PacketPath(when=utcnow(), origin="f2c24f54551e", hops=[], snr=6.0)],
        neighbour_links=[],
    )
    view = _view(topo, topo.suggested("f2c24f54551e"), None, "f2c24f54551e", "Far", [FAR])
    assert view.routes and view.routes[0].draw == ()  # a straight endpoint-to-endpoint line


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


def test_contract_bidir_clusters_folds_a_knot_but_keeps_the_rows() -> None:
    """A 3+ bidirectional knot contracts to one super-node in the graph draw, while each route
    row and its trace spec keep every member named in order."""
    from meshterm.ui.node_detail_screen import _contract_bidir_clusters

    routes = [
        _Route(draw=("aa", "bb", "cc"), spec="s1", row=Text("A B C")),
        _Route(draw=("cc", "bb", "aa"), spec="s2", row=Text("C B A")),
    ]
    new, clusters = _contract_bidir_clusters(routes, lambda _n: 2)  # all repeaters
    (cid, cluster), = clusters.items()
    assert cluster.label == "3 repeaters" and cluster.glyph == "▲"
    assert [r.draw for r in new] == [(cid,), (cid,)]  # members folded to the one cluster stop
    assert [r.spec for r in new] == ["s1", "s2"]  # traces still arm on the real path
    assert [r.row.plain for r in new] == ["A B C", "C B A"]  # the list keeps the full order


def test_contract_leaves_a_two_node_pair_and_unclustered_routes_alone() -> None:
    """A tidy two-way pair keeps its own markers (the display JP likes); nothing contracts."""
    from meshterm.ui.node_detail_screen import _contract_bidir_clusters

    routes = [
        _Route(draw=("aa", "bb"), spec="s1", row=Text("A B")),
        _Route(draw=("bb", "aa"), spec="s2", row=Text("B A")),
    ]
    new, clusters = _contract_bidir_clusters(routes, lambda _n: 2)
    assert clusters == {}
    assert [r.draw for r in new] == [("aa", "bb"), ("bb", "aa")]


def test_contract_labels_a_mixed_cluster_generically() -> None:
    """A knot whose members are different node types can't wear one type mark — it reads
    ``n nodes`` under the plain dot."""
    from meshterm.ui.node_detail_screen import _contract_bidir_clusters

    routes = [
        _Route(draw=("aa", "bb", "cc"), spec="", row=Text("")),
        _Route(draw=("cc", "bb", "aa"), spec="", row=Text("")),
    ]
    _new, clusters = _contract_bidir_clusters(routes, lambda n: {"aa": 2, "bb": 3, "cc": 4}[n[:2]])
    (_cid, cluster), = clusters.items()
    assert cluster.label == "3 nodes" and cluster.glyph == "●"


def test_located_accepts_real_fixes_and_rejects_junk() -> None:
    """The location preview is gated on a real fix — no null island, no out-of-range advert."""
    from meshterm.ui.node_detail_screen import _located

    assert _located(45.5, -73.6) is True
    assert _located(None, -73.6) is False  # missing coordinate
    assert _located(0.0, 0.0) is False  # null island (no-GPS sentinel)
    assert _located(-97.0, -1041.97) is False  # the Homestead out-of-range advert


# --- the screen -----------------------------------------------------------------


def _header() -> Text:
    header = Text("▲ ", style="#a78bfa")
    header.append("Hub", style="accent")
    header.append("   repeater", style="muted")
    return header


def _screen(**over) -> NodeDetailScreen:
    """A node detail screen over hand-built display data (Info + a bare-note Routes tab)."""
    kwargs = dict(
        title="Node — Hub",
        header=_header(),
        info_rows=[
            ("key", highlighted_hash("3d63c6429436" + "0" * 52, 1)),
            ("heard", Text("5m ago")),
            ("packets", Text("42")),
        ],
        tabs=[_Tab("Info", "info"), _Tab("Routes", "routes")],
        minimap=None,
        map_caption=None,
        routes=_RoutesView(note="no route observed yet — trace to discover one"),
        info_actions=[_Action("timemachine", "⏳", "", "Time machine — 42 receptions")],
        trace_action=_Action("trace", "🎯", "", "Trace — auto route …"),
        tail_actions=[_Action("back", "", "", "Back")],
    )
    kwargs.update(over)
    return NodeDetailScreen(**kwargs)


def test_node_detail_screen_renders_its_sections() -> None:
    """The Info tab carries the vitals and its actions; Routes carries the stage + Trace."""
    screen = _screen()
    screen.note_viewport(30)
    body = _plain(screen.render_body(72))
    assert "Hub" in body and "repeater" in body  # the pinned identity header
    assert "── Info ──" in body and "42" in body  # the vitals moved into the Info tab
    assert "Time machine" in body and "Back" in body  # the Info actions + shared tail
    assert "────────" in body  # the faint rule closing the stage
    assert "no route observed yet" not in body  # the Routes stage waits on its own tab

    screen.handle("right")
    body = _plain(screen.render_body(72))
    assert "── Routes ──" in body and "no route observed yet" in body
    assert "Trace" in body and "Back" in body
    assert "packets" not in body  # the vitals stay on the Info tab
    # Every rendered line fits the 72-column standard.
    for line in screen.render_body(72):
        assert len(_plain([line])) <= 72
    assert len(screen.footer_hint) <= 72


def test_node_detail_screen_cursor_and_commit() -> None:
    """↑/↓ move the cursor (kept in view); Enter resolves its key, Esc cancels."""
    screen = _screen()
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]

    assert screen.cursor_line() is None  # nothing rendered yet
    screen.note_viewport(30)
    screen.render_body(72)
    # The cursor opens on the Info tab's first row (Time machine) and is always reported,
    # so the frame can keep it visible on a terminal too short for the pinned layout.
    assert screen.cursor_line() is not None
    screen.handle("down")  # onto Back
    screen.handle("up")  # and back onto Time machine
    screen.render_body(72)
    screen.handle("enter")
    assert resolved == ["timemachine"]

    screen.handle("escape")
    assert resolved == ["timemachine", CANCEL]


def test_node_detail_back_row_leaves_like_escape() -> None:
    """Committing the Back row resolves CANCEL — the same leave Esc does — not a stray token
    the opener's loop would ignore and re-show the page over."""
    screen = _screen()
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]

    screen.render_body(72)
    screen.handle("down")  # Time machine -> Back
    screen.handle("enter")
    assert resolved == [CANCEL]


def _two_routes() -> _RoutesView:
    """A routes view with two selectable routes, the way the assembly hands them over."""
    return _RoutesView(
        routes=[
            _Route(draw=("3d63c6429436",), spec="3d,f2,3d", row=Text("via Hub")),
            _Route(draw=("a1a1a1a1a1a1",), spec="a1,f2,a1", row=Text("via Alt")),
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )


def test_node_detail_screen_route_selection_arms_the_trace() -> None:
    """↑/↓ over the route rows moves the graph highlight and the spec a trace would arm on."""
    screen = _screen(routes=_two_routes(), tabs=[_Tab("Routes", "routes")])
    # Focusables: route 0, route 1, Time machine, Back — with routes listed, the rows are
    # the trace entry points, so no dedicated Trace action row renders.
    assert screen.selected_spec() == "3d,f2,3d"
    screen.handle("down")  # onto route 1
    assert screen._route_sel == 1 and screen.selected_spec() == "a1,f2,a1"
    screen.handle("down")  # onto Time machine — the pick holds
    assert screen.selected_spec() == "a1,f2,a1"
    screen.note_viewport(30)
    body = _plain(screen.render_body(72))
    assert "via Hub …" in body and "via Alt …" in body  # both rows drew, …-marked as openers
    assert "Trace" not in body  # the auto-route stand-in only shows with no routes to list


def test_node_detail_enter_on_a_route_row_opens_its_trace() -> None:
    """Enter on any route row arms a trace on that route — no separate Trace row needed."""
    screen = _screen(routes=_two_routes(), tabs=[_Tab("Routes", "routes")])
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    screen.handle("down")  # onto route 1
    screen.handle("enter")
    assert resolved == ["trace"] and screen.selected_spec() == "a1,f2,a1"


def test_node_detail_route_list_windows_inside_the_page() -> None:
    """With more routes than fit, the list windows with edge markers — the graph and the
    action rows never leave the screen, however many routes a busy node has."""
    routes = _RoutesView(
        routes=[
            _Route(draw=(f"{i:x}{i:x}" * 6,), spec=f"s{i}", row=Text(f"route {i}"))
            for i in range(12)
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    lines = screen.render_body(72)
    assert len(lines) <= 30  # the body fits the viewport — nothing scrolls off
    body = _plain(lines)
    assert "↓" in body and "more" in body  # the edge marker counts the hidden routes
    assert "Back" in body  # the pinned tail never leaves
    assert "PgUp/PgDn scroll" in screen.footer_hint  # paging advertised only when needed

    # Walking the cursor to the last route slides the window down to keep it visible.
    for _ in range(11):
        screen.handle("down")
    lines = screen.render_body(72)
    assert len(lines) <= 30
    assert "route 11" in _plain(lines) and screen.cursor_line() is not None


def test_fit_blocks_walks_wrapped_rows_into_view() -> None:
    """The variable-height fit keeps whole blocks, spends marker lines only when rows hide,
    and walks the window down to the cursor's row."""
    screen = _screen()
    top, count = screen._fit_blocks([2, 2, 2, 2], 5, 3)  # cursor on the last 2-line row
    assert top + count == 4 and top == 2  # slid to the tail; the last two rows fit
    top, count = screen._fit_blocks([1, 1], 5, 0)
    assert (top, count) == (0, 2)  # everything fits: no window, no markers


def test_node_detail_screen_tabs_switch_the_stage() -> None:
    """←→ (and Tab) swap which view fills the stage; the footer offers the switch only then."""
    mini = MiniMap(
        _FakeSession(), _OfflineSource(), 14,
        center_lat=45.5, center_lon=-73.6, zoom=13,
        markers=[MapMarker(label="Hub", lat=45.5, lon=-73.6, key="3d63aa")],
    )
    screen = _screen(
        minimap=mini,
        map_caption=Text("Hub · centred here", style="faint"),
    )
    screen.note_viewport(30)
    assert "←→ tab" in screen.footer_hint  # two tabs, so the switch is advertised
    # Opens on the Info tab: the vitals, the located preview's caption, and its braille
    # edge scrub all belong to it.
    body = _plain(screen.render_body(72))
    assert "── Info ──" in body and "centred here" in body
    assert screen.consume_edge_scrub() == 2

    screen.handle("right")  # switch to the Routes tab
    body = _plain(screen.render_body(72))
    assert "── Routes ──" in body and "no route observed yet" in body
    assert screen.consume_edge_scrub() == 0  # the braille preview isn't showing now


def test_node_detail_single_tab_hides_the_switch_hint() -> None:
    """A page with only one view drops the ←→ tab atom from its footer."""
    screen = _screen(tabs=[_Tab("Info", "info")])  # our own node: no Routes tab
    assert "←→ tab" not in screen.footer_hint
    assert "↑↓ move" in screen.footer_hint and screen.footer_hint.endswith("Esc back")
