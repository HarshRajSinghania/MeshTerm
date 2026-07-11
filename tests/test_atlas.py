"""Mesh Atlas tests: the radial layout and the screen's rendering and selection.

The layout is a pure function, tested directly; the screen is driven headless against a
fake session and a hand-built topology, the same approach as the dashboard tests.
"""

from __future__ import annotations

import re
from datetime import timedelta

from meshterm.core.models import Contact, utcnow
from meshterm.services.topology import MeshTopology
from meshterm.ui.atlas_screen import AtlasScreen, radial_layout

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


# --- the layout ------------------------------------------------------------------------


def test_layout_rings_by_hop_distance() -> None:
    """Us at the centre, direct links ring 1, their neighbours ring 2."""
    links = [(US, "n1", 1.0), ("n1", "n2", 1.0)]
    positions, rings, islands = radial_layout(US, links, 160, 96)
    assert rings == {US: 0, "n1": 1, "n2": 2}
    assert not islands
    cx, cy = positions[US]
    d1 = abs(positions["n1"][0] - cx) + abs(positions["n1"][1] - cy)
    d2 = abs(positions["n2"][0] - cx) + abs(positions["n2"][1] - cy)
    assert 0 < d1 < d2  # farther evidence, farther out


def test_layout_islands_take_the_outermost_ring() -> None:
    """A cluster with no path to us lands outside everything reached."""
    links = [(US, "n1", 1.0), ("x1", "x2", 1.0)]
    _positions, rings, islands = radial_layout(US, links, 160, 96)
    assert islands == {"x1", "x2"}
    assert rings["x1"] == rings["x2"] == rings["n1"] + 1


def test_layout_spreads_a_ring_and_stays_deterministic() -> None:
    """Ring members never collide, and the same input lays out the same twice."""
    links = [(US, f"n{i}", 1.0) for i in range(8)]
    first = radial_layout(US, links, 200, 120)
    second = radial_layout(US, links, 200, 120)
    assert first == second
    positions = first[0]
    ring_cells = {(x // 2, y // 4) for n, (x, y) in positions.items() if n != US}
    assert len(ring_cells) == 8  # every marker in its own character cell


def test_layout_keeps_nodes_on_canvas() -> None:
    """Every position lands within the dot grid, margins included."""
    links = [(US, f"n{i}", 1.0) for i in range(6)] + [("n0", "m0", 1.0)]
    positions, _rings, _islands = radial_layout(US, links, 144, 64)
    for x, y in positions.values():
        assert 0 <= x < 144 and 0 <= y < 64


# --- the screen ------------------------------------------------------------------------


def test_atlas_renders_graph_and_overview_panel() -> None:
    """The canvas fills the viewport and the panel carries legend and evidence."""
    screen = _screen(_topo())
    lines = screen.render_body(80)
    assert len(lines) == 24  # canvas rows + spacer + three panel rows
    body = _plain(lines)
    assert "Homestead" in body and "YUL-Cartierville" in body and "Alice" in body
    assert "trace 1" in body and "packet 1" in body
    assert "Mesh Atlas · 3 nodes · 2 links" == screen.title


def test_atlas_selection_walks_and_fills_the_panel() -> None:
    """→ steps overview → us → nodes; the panel reads the selection's links."""
    screen = _screen(_topo())
    screen.render_body(80)  # establish the layout and the cycle
    screen.handle("right")
    body = _plain(screen.render_body(80))
    assert "this device" in body
    screen.handle("right")
    body = _plain(screen.render_body(80))
    assert "repeater" in body and "1 hop out" in body
    assert "+6.0" in body  # the us↔YUL link's median SNR
    screen.handle("left")
    screen.handle("left")
    assert "←/→ walk the graph" in _plain(screen.render_body(80))  # back to overview


def test_atlas_marks_islands_in_the_panel() -> None:
    """An island selection says so instead of quoting a bogus hop count."""
    screen = _screen(_topo(with_island=True))
    screen.render_body(80)
    for _ in range(4):  # overview → us → YUL → Alice → first island node
        screen.handle("right")
    body = _plain(screen.render_body(80))
    assert "island — no observed path to us" in body


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
