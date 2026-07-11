"""Mesh Atlas tests: the ring layout service and the screen's camera, find, and panel.

The layout is a pure function, tested directly; the screen is driven headless against a
fake session and a hand-built topology, the same approach as the dashboard tests.
"""

from __future__ import annotations

import math
import re
from datetime import timedelta

from meshterm.core.models import Contact, utcnow
from meshterm.services.atlas_layout import _MIN_SEPARATION, compute_layout
from meshterm.services.topology import MeshTopology
from meshterm.ui.atlas_screen import AtlasScreen

US = "aa" * 6
YUL = Contact(name="YUL-Cartierville", public_key="3d" * 32, node_type=2)
ALICE = Contact(name="Alice", public_key="b2" * 32, last_seen=utcnow() - timedelta(minutes=5))


class _FakeSession:
    def __init__(self, cell_h: int = 24) -> None:
        self.cell_h = cell_h
        self.repaints = 0

    def base_body_size(self) -> tuple[int, int]:
        return (80, self.cell_h)

    def invalidate(self) -> None:
        self.repaints += 1


def _topo(*, with_island: bool = False) -> MeshTopology:
    """us — YUL — Alice as a two-ring chain, optionally plus a detached island pair."""
    topo = MeshTopology(US, contacts=[YUL, ALICE])
    yul = topo.canonical(YUL.public_key)
    alice = topo.canonical(ALICE.public_key)
    when = utcnow()
    topo.add_walk([topo.self_id, yul], snrs=[6.0], when=when, source="trace")
    topo.add_walk([yul, alice], snrs=[-2.0], when=when, source="packet")
    if with_island:
        topo.add_walk(["c3" * 6, "d4" * 6], snrs=[1.0], when=when, source="neighbour")
    return topo


def _screen(topo: MeshTopology, cell_h: int = 24) -> AtlasScreen:
    return AtlasScreen(
        session=_FakeSession(cell_h),
        topo=topo,
        contacts={
            topo.canonical(YUL.public_key): YUL,
            topo.canonical(ALICE.public_key): ALICE,
        },
        self_label="Homestead",
    )


def _plain(lines: list[str]) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines))


def _radius(pos: tuple[float, float]) -> float:
    return math.hypot(*pos)


def _angle(pos: tuple[float, float]) -> float:
    return math.atan2(pos[1], pos[0]) % math.tau


# --- the layout service ------------------------------------------------------------------


def test_layout_rings_by_hop_distance() -> None:
    """Us at the origin, direct links on ring 1, their neighbours on ring 2."""
    layout = compute_layout(US, [(US, "n1", 1.0), ("n1", "n2", 1.0)])
    assert layout.rings == {US: 0, "n1": 1, "n2": 2}
    assert not layout.islands and layout.max_ring == 2
    assert layout.positions[US] == (0.0, 0.0)
    assert 0 < _radius(layout.positions["n1"]) < _radius(layout.positions["n2"]) <= 1.0


def test_layout_islands_take_the_outermost_ring() -> None:
    """A cluster with no path to us lands outside everything reached."""
    layout = compute_layout(US, [(US, "n1", 1.0), ("x1", "x2", 1.0)])
    assert layout.islands == {"x1", "x2"}
    assert layout.rings["x1"] == layout.rings["x2"] == layout.rings["n1"] + 1


def test_layout_chains_stay_radial() -> None:
    """Relaxation pulls a chain's outer node onto its parent's bearing."""
    layout = compute_layout(US, [(US, "n1", 1.0), ("n1", "n2", 1.0)])
    gap = abs(_angle(layout.positions["n1"]) - _angle(layout.positions["n2"]))
    gap = min(gap, math.tau - gap)
    assert gap < 0.15  # essentially the same bearing: a straight radial spoke


def test_layout_spreads_a_ring_and_stays_deterministic() -> None:
    """Ring members keep a minimum angular gap, and the same input lays out the same."""
    links = [(US, f"n{i}", 1.0) for i in range(8)]
    first = compute_layout(US, links)
    second = compute_layout(US, links)
    assert first.positions == second.positions
    angles = sorted(_angle(first.positions[f"n{i}"]) for i in range(8))
    gaps = [b - a for a, b in zip(angles, angles[1:])]
    gaps.append(math.tau - angles[-1] + angles[0])
    assert min(gaps) >= min(_MIN_SEPARATION, math.tau / 8) - 1e-6


def test_layout_stays_in_unit_space() -> None:
    """Every position lands within the unit circle the screen projects from."""
    links = [(US, f"n{i}", 1.0) for i in range(6)] + [("n0", "m0", 1.0), ("q1", "q2", 0.5)]
    layout = compute_layout(US, links)
    assert all(_radius(p) <= 1.0 + 1e-9 for p in layout.positions.values())


# --- the screen: rendering ----------------------------------------------------------------


def test_atlas_renders_graph_and_overview_panel() -> None:
    """The canvas fills the viewport and the panel carries legend and evidence."""
    screen = _screen(_topo())
    lines = screen.render_body(80)
    assert len(lines) == 24  # canvas rows + spacer + three panel rows
    body = _plain(lines)
    assert "Homestead" in body and "YUL-Cartierville" in body and "Alice" in body
    assert "trace 1" in body and "packet 1" in body
    assert "rings = hops out" in body  # the overview hint row
    assert "Mesh Atlas · 3 nodes · 2 links" == screen.title


def test_atlas_empty_graph_renders_guidance() -> None:
    """With no evidence at all the screen explains how the atlas fills up."""
    topo = MeshTopology(US, contacts=[])
    screen = _screen(topo)
    body = _plain(screen.render_body(80))
    assert "no evidence to draw yet" in body
    assert "trace" in body


def test_atlas_details_lists_every_link() -> None:
    """The Enter dialog's body carries neighbour, SNR, samples, and source."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    yul = topo.canonical(YUL.public_key)
    text = screen._details_text(yul).plain
    assert "Homestead" in text and "Alice" in text
    assert "+6.0 dB" in text and "-2.0 dB" in text
    assert "1×" in text and "trace" in text and "packet" in text


# --- the screen: camera -------------------------------------------------------------------


def test_atlas_zooms_pans_and_resets_like_the_map() -> None:
    """PgUp/PgDn step the zoom, arrows pan (clamped), Home restores the full fit."""
    screen = _screen(_topo())
    screen.render_body(80)
    assert screen._zoom == 1.0

    screen.handle("pageup")
    screen.handle("pageup")
    assert screen._zoom == 2.25
    screen.render_body(80)
    assert "2.2×" in screen.title

    screen.handle("right")
    screen.handle("down")
    assert screen._cam != (0.0, 0.0)

    # At 1× the camera clamps back to the origin — the graph can't be lost.
    screen.handle("pagedown")
    screen.handle("pagedown")
    screen.handle("pagedown")
    assert screen._zoom == 1.0 and screen._cam == (0.0, 0.0)

    screen.handle("pageup")
    screen.handle("left")
    screen.handle("home")
    assert screen._zoom == 1.0 and screen._cam == (0.0, 0.0)


def test_atlas_fine_pan_moves_less_than_coarse() -> None:
    """Shift+arrow nudges the camera a fraction of a coarse step."""
    screen = _screen(_topo())
    screen.render_body(80)
    screen.handle("pageup")  # free some pan range
    screen.handle("right")
    coarse = screen._cam[0]
    screen.handle("home")
    screen.handle("pageup")
    screen.handle("shift_right")
    assert 0 < screen._cam[0] < coarse


# --- the screen: selection and find --------------------------------------------------------


def test_atlas_tab_walks_and_fills_the_panel() -> None:
    """Tab steps overview → us → nodes; the panel reads the selection's links."""
    screen = _screen(_topo())
    screen.render_body(80)  # establish the layout and the cycle
    screen.handle("tab")
    body = _plain(screen.render_body(80))
    assert "this device" in body
    screen.handle("tab")
    body = _plain(screen.render_body(80))
    assert "repeater" in body and "1 hop out" in body
    assert "+6.0" in body  # the us↔YUL link's median SNR
    # Esc peels the selection back to the overview.
    screen.handle("escape")
    assert "rings = hops out" in _plain(screen.render_body(80))


def test_atlas_marks_islands_in_the_panel() -> None:
    """An island selection says so instead of quoting a bogus hop count."""
    screen = _screen(_topo(with_island=True))
    screen.render_body(80)
    for _ in range(4):  # overview → us → YUL → Alice → first island node
        screen.handle("tab")
    body = _plain(screen.render_body(80))
    assert "island — no observed path to us" in body


def test_atlas_find_filters_selects_and_peels() -> None:
    """Typing narrows to matches, Enter adopts the first, Esc peels layer by layer."""
    import asyncio

    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)

    for ch in "ali":
        screen.handle("text", ch)
    assert screen._filter == "ali"
    screen.render_body(80)
    assert "1 match" in screen.title
    assert "find: ali" in screen.footer_hint
    body = _plain(screen.render_body(80))
    assert "1 of" in body and "Alice" in body  # the find panel names the match
    # Non-matching labels drop to context; the match stays labelled on canvas.
    canvas_only = _plain(screen.render_body(80)[:-4])
    assert "Alice" in canvas_only and "YUL-Cartierville" not in canvas_only

    screen.handle("enter")  # adopt the first match
    assert screen._filter == ""
    assert screen._selected == topo.canonical(ALICE.public_key)

    async def drive() -> object:
        screen.future = asyncio.get_running_loop().create_future()
        screen.handle("escape")  # selection → overview
        assert screen._selected is None and not screen.future.done()
        screen.handle("escape")  # overview → dismiss
        return await screen.future

    assert asyncio.run(drive()) is None


def test_atlas_rebuild_refreshes_the_evidence() -> None:
    """Ctrl+R re-reads storage and the new link count shows on the next paint."""
    grown = _topo(with_island=True)
    screen = AtlasScreen(
        session=_FakeSession(),
        topo=_topo(),
        contacts={},
        self_label="Homestead",
        rebuild=lambda: grown,
    )
    screen.render_body(80)
    assert "2 links" in screen.title
    screen.handle("retry")
    screen.render_body(80)
    assert "3 links" in screen.title
