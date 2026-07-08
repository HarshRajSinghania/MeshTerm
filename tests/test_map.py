"""Tests for the node map: MVT decoding, projection, the braille canvas, and the tool.

The vector-tile decoder and geometry are exercised against a real cached tile fixture
(``tests/fixtures/tile_14_4843_5861.mvt``, central Montréal) so the parsing is verified end
to end without the network; the projection, canvas, and compositor are pure; the tool runs
against the :class:`MockDevice` simulator with the basemap disabled (no network in tests).
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.core.geo import BBox, Viewport, haversine_km, lonlat_to_world, world_to_lonlat
from meshterm.core.models import (
    NODE_TYPE_REPEATER,
    Observation,
    utcnow,
)
from meshterm.core.mvt import GEOM_LINE, GEOM_POLYGON, decode_tile
from meshterm.tools.map import MapTool
from meshterm.ui.mapcanvas import MapCanvas, parse_hex

_FIXTURE = Path(__file__).parent / "fixtures" / "tile_14_4843_5861.mvt"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(lines: list[str]) -> str:
    """Strip ANSI colour from rendered lines for content assertions."""
    return "\n".join(_ANSI.sub("", line) for line in lines)


# -- MVT decoding -------------------------------------------------------------


def test_decode_tile_reads_expected_layers() -> None:
    """The real fixture decodes into the OpenMapTiles layers the map relies on."""
    layers = {layer.name: layer for layer in decode_tile(_FIXTURE.read_bytes())}
    for expected in ("transportation", "transportation_name", "water", "waterway", "place"):
        assert expected in layers, expected
    assert layers["transportation"].extent == 4096
    assert layers["transportation"].features  # roads were decoded


def test_decode_tile_resolves_names_and_geometry() -> None:
    """Street and place features carry resolved names and non-trivial geometry."""
    layers = {layer.name: layer for layer in decode_tile(_FIXTURE.read_bytes())}
    streets = [f.name for f in layers["transportation_name"].features if f.name]
    assert any("Rue" in s or "Avenue" in s or "Av." in s for s in streets)

    places = {f.name: f.get("class") for f in layers["place"].features if f.name}
    assert "Montréal" in places and places["Montréal"] == "city"

    # A road is a line with at least two points; water is a polygon.
    assert any(
        f.geom_type == GEOM_LINE and len(f.rings[0]) >= 2
        for f in layers["transportation"].features
    )
    assert any(f.geom_type == GEOM_POLYGON for f in layers["water"].features)


def test_decode_tile_handles_empty_input() -> None:
    """An empty byte string decodes to no layers rather than raising."""
    assert decode_tile(b"") == []


# -- projection / viewport ----------------------------------------------------


def test_world_projection_round_trips() -> None:
    """lon/lat -> world pixels -> lon/lat recovers the original coordinate."""
    for lat, lon, z in [(45.5, -73.6, 14), (0.0, 0.0, 3), (-33.9, 151.2, 12)]:
        wx, wy = lonlat_to_world(lat, lon, z)
        rlat, rlon = world_to_lonlat(wx, wy, z)
        assert rlat == pytest.approx(lat, abs=1e-6)
        assert rlon == pytest.approx(lon, abs=1e-6)


def test_haversine_known_distance() -> None:
    """One degree of longitude at the equator is ~111 km; identical points are 0."""
    assert haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.19, abs=0.5)
    assert haversine_km(45.0, -73.0, 45.0, -73.0) == 0.0


def test_viewport_center_projects_to_the_middle() -> None:
    """The viewport's centre lands at the middle dot; north is up."""
    vp = Viewport(45.5, -73.6, 13, 200, 120)
    cx, cy = vp.lonlat_to_dot(45.5, -73.6)
    assert cx == pytest.approx(100, abs=0.5) and cy == pytest.approx(60, abs=0.5)
    # A point due north projects above the centre (smaller y).
    _, ny = vp.lonlat_to_dot(45.6, -73.6)
    assert ny < cy


def test_viewport_fit_frames_all_points() -> None:
    """A fitted viewport is centred on the points and zoomed so they share a tile scale."""
    pts = [(45.5019, -73.5674), (45.4768, -73.5990)]
    vp = Viewport.fit(pts, 200, 120, max_zoom=14)
    assert 8 <= vp.zoom <= 14
    for lat, lon in pts:
        x, y = vp.lonlat_to_dot(lat, lon)
        assert 0 <= x < vp.dot_w and 0 <= y < vp.dot_h  # every point is on-canvas


def test_viewport_fit_fraction_ignores_outliers() -> None:
    """Framing half the nodes zooms to the dense core, letting far outliers fall off-canvas."""
    # A tight downtown cluster of five nodes (~50 m across) plus one distant outlier.
    core = [
        (45.5000, -73.5600), (45.5003, -73.5602), (45.4998, -73.5598),
        (45.5001, -73.5599), (45.4999, -73.5601),
    ]
    outlier = (46.80, -71.20)
    pts = core + [outlier]

    full = Viewport.fit(pts, 200, 120, max_zoom=16)
    half = Viewport.fit(pts, 200, 120, max_zoom=16, fraction=0.5)

    # The core-only frame is zoomed in tighter than the frame that must hold the outlier.
    assert half.zoom > full.zoom
    # Every clustered node stays on-canvas; the outlier is pushed off.
    for lat, lon in core:
        x, y = half.lonlat_to_dot(lat, lon)
        assert 0 <= x < half.dot_w and 0 <= y < half.dot_h
    ox, oy = half.lonlat_to_dot(*outlier)
    assert not (0 <= ox < half.dot_w and 0 <= oy < half.dot_h)


def test_viewport_fit_fraction_keeps_small_sets_whole() -> None:
    """With only two nodes there is no core to isolate — both are still framed."""
    pts = [(45.5019, -73.5674), (45.4768, -73.5990)]
    half = Viewport.fit(pts, 200, 120, max_zoom=14, fraction=0.5)
    full = Viewport.fit(pts, 200, 120, max_zoom=14)
    assert (half.center_lat, half.center_lon, half.zoom) == (
        full.center_lat, full.center_lon, full.zoom
    )


def test_viewport_zoom_and_pan() -> None:
    """Zooming and panning return new viewports; zoom clamps to its bounds."""
    vp = Viewport(45.5, -73.6, 10, 200, 120)
    assert vp.zoomed(2).zoom == 12
    assert vp.zoomed(50, max_zoom=16).zoom == 16  # clamped
    assert vp.zoomed(-50, min_zoom=3).zoom == 3
    east = vp.panned(0.5, 0)
    assert east.center_lon > vp.center_lon  # panning east increases longitude
    north = vp.panned(0, -0.5)
    assert north.center_lat > vp.center_lat  # panning north increases latitude


def test_viewport_tiles_cover_the_view() -> None:
    """The viewport reports the handful of tiles overlapping it at the tile zoom."""
    vp = Viewport(45.5019, -73.5674, 14, 200, 120)
    tiles = vp.tiles(14)
    assert tiles and all(z == 14 for z, _, _ in tiles)
    # The fixture tile for this centre must be among them.
    assert (14, 4843, 5861) in tiles


# -- braille canvas -----------------------------------------------------------


def test_canvas_plots_dots_as_braille() -> None:
    """A single plotted dot renders as a braille glyph in the top-left cell."""
    canvas = MapCanvas(4, 2)
    canvas.plot(0, 0, (255, 0, 0), 1)
    out = _plain(canvas.to_ansi_lines())
    assert out.splitlines()[0][0] == "⠁"  # braille dot-1


def test_canvas_priority_decides_cell_colour() -> None:
    """When dots from two features share a cell, the higher priority sets its colour."""
    canvas = MapCanvas(1, 1)
    canvas.plot(0, 0, (10, 20, 30), 1)  # low priority
    canvas.plot(1, 0, (200, 100, 50), 9)  # high priority, same cell
    ansi = "".join(canvas.to_ansi_lines())
    assert "38;2;200;100;50" in ansi  # the high-priority colour won
    assert "38;2;10;20;30" not in ansi


def test_canvas_marker_and_label_do_not_overprint() -> None:
    """A marker keeps its glyph and places its label in a neighbouring cell."""
    canvas = MapCanvas(20, 3)
    canvas.marker(4, 4, "★", (255, 255, 255))
    assert canvas.marker_label(4, 4, "me", (255, 255, 255))
    out = _plain(canvas.to_ansi_lines())
    assert "★" in out and "me" in out
    row = out.splitlines()[1]
    assert "★me" not in row  # a gap sits between the marker and its label


def test_canvas_label_collision_is_avoided() -> None:
    """A checked label is skipped when it would overlap an already-placed one."""
    canvas = MapCanvas(20, 1)
    assert canvas.place_label(10, 0, "First", (200, 200, 200))
    assert not canvas.place_label(10, 0, "Second", (200, 200, 200))  # overlaps → skipped


def test_canvas_labels_keep_a_vertical_gap() -> None:
    """A checked label is skipped when it would sit flush above/below existing text."""
    canvas = MapCanvas(20, 3)
    assert canvas.place_label(20, 4, "Row1", (200, 200, 200))  # dot y=4 → cell row 1
    # Directly below (cell row 2) with overlapping columns: rejected for lack of a gap.
    assert not canvas.place_label(20, 8, "Row2", (200, 200, 200))
    # Same row but clear of the horizontal span still fits.
    assert canvas.place_label(2, 4, "Far", (200, 200, 200))


def test_parse_hex() -> None:
    """Hex colours parse to RGB triples, with or without the leading hash."""
    assert parse_hex("#38bdf8") == (0x38, 0xBD, 0xF8)
    assert parse_hex("ffffff") == (255, 255, 255)


# -- compositor over a real tile ----------------------------------------------


def test_render_map_draws_basemap_streets_and_labels() -> None:
    """Rendering the real fixture yields braille geometry and a place label."""
    from meshterm.ui.map_render import MapMarker, render_map

    vp = Viewport(45.5019, -73.5674, 14, 180, 120)
    layers = decode_tile(_FIXTURE.read_bytes())
    tiles = {(14, 4843, 5861): layers}
    markers = [MapMarker("Yagi", 45.5019, -73.5674, is_repeater=True)]
    out = _plain(render_map(vp, tiles, markers))
    assert any(0x2800 <= ord(ch) <= 0x28FF for ch in out)  # braille was drawn
    assert "▲" in out and "Yagi" in out  # the repeater marker + label
    assert "Montréal" in out  # a place label from the tile


def test_render_map_prioritises_repeater_glyph() -> None:
    """A repeater and a node at the same spot resolve to the repeater's marker on top."""
    from meshterm.ui.map_render import MapMarker, render_map

    vp = Viewport(45.50, -73.57, 14, 60, 40)
    markers = [
        MapMarker("node", 45.50, -73.57),
        MapMarker("rptr", 45.50, -73.57, is_repeater=True),
    ]
    out = _plain(render_map(vp, {}, markers))
    assert "▲" in out and "●" not in out


def test_render_map_drops_crowded_labels_favouring_repeaters() -> None:
    """When labels can't all fit, the repeater's wins and a crowded node's is dropped."""
    from meshterm.ui.map_render import MapMarker, render_map

    # Three nodes stacked on one spot: only the two sides (left/right) can hold a label,
    # so one of the three must show as a bare marker — and the repeater must not be it.
    vp = Viewport(45.50, -73.57, 14, 40, 8)
    markers = [
        MapMarker("NODEONE", 45.50, -73.57),
        MapMarker("NODETWO", 45.50, -73.57),
        MapMarker("REPEATER", 45.50, -73.57, is_repeater=True),
    ]
    out = _plain(render_map(vp, {}, markers))
    assert "▲" in out  # the repeater's glyph is on top
    assert "REPEATER" in out  # and it keeps its label (placed first)
    # Only one of the two leaf nodes could fit a label; the other is a bare marker.
    assert ("NODEONE" in out) != ("NODETWO" in out)


def _glyph_color(lines: list[str], glyph: str) -> tuple[int, int, int]:
    """Extract the truecolour ``(r, g, b)`` the given glyph was rendered with."""
    m = re.search(
        r"38;2;(\d+);(\d+);(\d+)m(?:\x1b\[1m)?" + re.escape(glyph), "".join(lines)
    )
    assert m, f"glyph {glyph!r} not found with a colour"
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def test_render_map_brightens_piled_markers() -> None:
    """A cell many nodes share renders its glyph brighter than a lone node's."""
    from meshterm.ui.map_render import _NODE, MapMarker, render_map

    base = parse_hex(_NODE[1])
    vp = Viewport(45.50, -73.57, 14, 60, 40)

    lone = render_map(vp, {}, [MapMarker("solo", 45.50, -73.57)])
    assert _glyph_color(lone, "●") == base  # a single node keeps its base colour

    crowd = [MapMarker(f"n{i}", 45.50, -73.57) for i in range(8)]
    piled = _glyph_color(render_map(vp, {}, crowd), "●")
    # Washed toward white: every channel is brighter than the base cyan.
    assert all(p > b for p, b in zip(piled, base))
    assert piled != base


def test_render_map_works_without_basemap() -> None:
    """With no tiles the nodes still render on a blank grid (the offline fallback)."""
    from meshterm.ui.map_render import MapMarker, render_map

    vp = Viewport.fit([(45.5, -73.6), (45.4, -73.5)], 120, 80, max_zoom=14)
    markers = [
        MapMarker("A", 45.5, -73.6, is_self=True),
        MapMarker("B", 45.4, -73.5, is_repeater=True),
    ]
    out = _plain(render_map(vp, {}, markers))
    assert "★" in out and "▲" in out


# -- basemap source (offline behaviour) ---------------------------------------


def test_basemap_source_offline_is_graceful(tmp_path: Path) -> None:
    """An unreachable source reports unavailable and returns no tiles, never raising."""
    from meshterm.services.basemap import BasemapSource

    src = BasemapSource(tmp_path / "cache", tilejson_url="http://127.0.0.1:1/none", timeout=0.2)
    assert src.available is False
    assert src.max_zoom == 14  # sensible default when the TileJSON can't be fetched
    assert src.load_tile(14, 4843, 5861) is None


def test_basemap_source_reads_cached_tile(tmp_path: Path) -> None:
    """A tile already on disk is decoded without any network access."""
    from meshterm.services.basemap import BasemapSource

    cache = tmp_path / "cache"
    dest = cache / "tiles" / "14" / "4843"
    dest.mkdir(parents=True)
    (dest / "5861.pbf").write_bytes(_FIXTURE.read_bytes())
    src = BasemapSource(cache, tilejson_url="http://127.0.0.1:1/none", timeout=0.2)
    layers = src.load_tile(14, 4843, 5861)
    assert layers is not None
    assert any(layer.name == "transportation" for layer in layers)


# -- the tool -----------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path):
    """A mock-backed application context with the plain (console) UI surface."""
    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
    from meshterm.core.config import Settings
    from meshterm.core.device_store import DeviceStore
    from meshterm.persistence.repository import Repository
    from meshterm.ui.theme import MESH_THEME

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "map.db")
    context = AppContext(
        console=Console(file=io.StringIO(), theme=MESH_THEME),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


async def test_map_tool_static_render_plots_contacts(ctx) -> None:
    """The CLI path plots the device's located contacts and reports the breakdown.

    The mock companion's contacts carry two located repeaters and one located leaf node
    (a fourth, Alice, has no fix), so the contact list — not the observation history — is
    what fills the map.
    """
    # Seed an observation whose key matches a located contact, so its reception detail is
    # merged onto that contact's marker.
    run_id = ctx.repo.start_run("monitor", {})
    ctx.repo.record_observation(
        run_id,
        Observation(node="a1b2c3d4", name="Yagi-Repeater", node_type=NODE_TYPE_REPEATER,
                    snr=6.0, observed_at=utcnow()),
    )
    result = await MapTool().run(ctx, {"static": True, "basemap": False})
    assert result.summary["located"] == 3
    assert result.summary["repeaters"] == 2
    assert result.summary["nodes"] == 1
    assert result.summary["self_located"] is False
    out = ctx.console.file.getvalue()
    assert out != ""
    assert "1 pkts" in out  # the seeded observation's detail merged onto the contact


def test_repository_round_trips_map_view(ctx) -> None:
    """The saved map viewport persists and reads back; absent by default."""
    assert ctx.repo.get_map_view() is None
    ctx.repo.set_map_view(45.51, -73.57, 13)
    lat, lon, zoom = ctx.repo.get_map_view()
    assert (lat, lon) == pytest.approx((45.51, -73.57))
    assert zoom == 13
    # A later save overwrites the single stored view.
    ctx.repo.set_map_view(40.0, -74.0, 9)
    assert ctx.repo.get_map_view() == pytest.approx((40.0, -74.0, 9))


async def test_map_tool_reports_nothing_to_plot(ctx, monkeypatch) -> None:
    """With no located contacts and no located history the tool returns cleanly."""
    async def _no_contacts(_ctx):
        return []

    monkeypatch.setattr(MapTool, "_contacts", staticmethod(_no_contacts))
    result = await MapTool().run(ctx, {"static": True, "basemap": False})
    assert result.summary == {"located": 0}


class _StubSession:
    """Minimal stand-in for :class:`TuiSession` for driving :class:`MapScreen`."""

    def __init__(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        self.invalidated = 0

    def base_body_size(self) -> tuple[int, int]:
        return self._cols, self._rows

    def invalidate(self) -> None:
        self.invalidated += 1


class _StubSource:
    """An always-offline tile source, so the screen renders nodes only (no network)."""

    available = False
    max_zoom = 14

    def load_tile(self, z: int, x: int, y: int):
        return None


def test_map_screen_renders_pans_zooms_and_resets() -> None:
    """The interactive screen fills its body, and wasd/zoom/reset move the viewport."""
    from meshterm.core.geo import Viewport
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    session = _StubSession(80, 24)
    markers = [
        MapMarker("A", 45.50, -73.60, is_repeater=True),
        MapMarker("B", 45.40, -73.50),
    ]
    screen = MapScreen(session, markers, _StubSource(), 14)

    lines = screen.render_body(80)
    assert len(lines) == 24  # fills the body height reported by the session
    start = screen._viewport

    screen.handle("text", "d")  # pan east → longitude increases
    assert screen._viewport.center_lon > start.center_lon
    screen.handle("text", "w")  # pan north → latitude increases
    assert screen._viewport.center_lat > start.center_lat

    z = screen._viewport.zoom
    screen.handle("text", "=")
    assert screen._viewport.zoom == z + 1
    screen.handle("text", "-")
    assert screen._viewport.zoom == z

    screen.handle("text", "r")  # reset refits to the nodes
    refit = Viewport.fit([(m.lat, m.lon) for m in markers], start.dot_w, start.dot_h, max_zoom=14)
    assert screen._viewport.zoom == refit.zoom
    assert screen._viewport.center_lat == pytest.approx(refit.center_lat)

    # Offline source: nodes still render and both markers are present.
    assert "▲" in _plain(screen.render_body(80)) and "●" in _plain(screen.render_body(80))
    assert "offline" in screen.footer_hint


def test_map_screen_shift_pans_by_a_single_cell() -> None:
    """Holding Shift (Shift+arrow, or uppercase WASD) pans finely, by one character cell."""
    from meshterm.core.geo import lonlat_to_world, world_to_lonlat
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    markers = [MapMarker("A", 45.50, -73.60), MapMarker("B", 45.40, -73.50)]
    screen = MapScreen(_StubSession(80, 24), markers, _StubSource(), 14)
    screen.render_body(80)
    start = screen._viewport

    # A coarse east pan (30% of the view) moves much further than a fine one.
    screen.handle("right")
    coarse = screen._viewport.center_lon - start.center_lon

    screen._viewport = start
    screen.handle("shift_right")
    fine = screen._viewport.center_lon - start.center_lon
    assert 0 < fine < coarse

    # The fine step is exactly one cell (2 dots) east at the current zoom.
    cx, cy = lonlat_to_world(start.center_lat, start.center_lon, start.zoom)
    _, expected_lon = world_to_lonlat(cx + 2, cy, start.zoom)
    assert screen._viewport.center_lon == pytest.approx(expected_lon)

    # Uppercase WASD is the same fine step via the text path.
    screen._viewport = start
    screen.handle("text", "D")
    assert screen._viewport.center_lon == pytest.approx(start.center_lon + fine)


def test_map_screen_restores_and_persists_view() -> None:
    """The screen reopens on a saved view and reports every centre/zoom change."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    saved: list[tuple[float, float, int]] = []
    markers = [MapMarker("A", 45.50, -73.60), MapMarker("B", 45.40, -73.50)]
    screen = MapScreen(
        _StubSession(80, 24), markers, _StubSource(), 14,
        saved_view=(46.80, -71.20, 12),
        on_view_change=lambda vp: saved.append(
            (vp.center_lat, vp.center_lon, vp.zoom)
        ),
    )

    screen.render_body(80)
    # Restored to the saved centre/zoom, not a fit of the markers.
    assert screen._viewport.zoom == 12
    assert screen._viewport.center_lat == pytest.approx(46.80)
    assert screen._viewport.center_lon == pytest.approx(-71.20)
    assert saved == []  # reopening unchanged doesn't rewrite the stored view

    screen.handle("text", "d")  # pan east persists the moved view
    assert saved and saved[-1][2] == 12  # zoom unchanged
    assert saved[-1][1] > -71.20  # centre moved east

    screen.handle("text", "=")  # zooming in persists too
    assert saved[-1][2] == 13


def test_map_screen_escape_dismisses() -> None:
    """Esc resolves the screen's future with None (backs out to the menu)."""
    import asyncio

    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    screen = MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)

    async def drive() -> object:
        screen.future = asyncio.get_running_loop().create_future()
        screen.handle("escape")
        return await screen.future

    assert asyncio.run(drive()) is None


def test_map_observation_round_trips_node_type(ctx) -> None:
    """A stored observation's advert type survives persistence and aggregation."""
    run_id = ctx.repo.start_run("monitor", {})
    ctx.repo.record_observation(
        run_id,
        Observation(node="a1", name="Yagi", node_type=NODE_TYPE_REPEATER, snr=6.0,
                    lat=45.5, lon=-73.5),
    )
    node = next(n for n in ctx.repo.heard_nodes() if n.node == "a1")
    assert node.is_repeater and node.node_type == NODE_TYPE_REPEATER
