"""Mesh atlas tests: the focus-and-walk browser over a hand-built topology.

The screen is driven headless against a fake session, the same approach as the
dashboard tests: render_body is pure lines-out, handle() is pure state, so walking,
backtracking, find, and the canvas/list split are all assertable without a terminal.
"""

from __future__ import annotations

import re
from datetime import timedelta

from meshterm.core.models import Contact, utcnow
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


# --- rendering ----------------------------------------------------------------------------


def test_atlas_opens_focused_on_us_with_the_link_list() -> None:
    """The default focus is our own node: canvas, legend, and its links beneath."""
    screen = _screen(_topo())
    body = _plain(screen.render_body(80))
    assert "Homestead" in body and "this device" in body
    assert "Links" in body and "strongest observed first" in body
    assert "YUL-Cartierville" in body  # our one neighbour, as a selectable row
    assert "edge = SNR" in body  # the canvas legend
    assert screen.title == "Mesh atlas — Homestead · 3 nodes · 2 links"


def test_atlas_link_rows_carry_snr_evidence_and_onward_count() -> None:
    """A neighbour row reads SNR, samples, source tags, age, and its onward links."""
    screen = _screen(_topo())
    body = _plain(screen.render_body(80))
    row = next(line for line in body.split("\n") if "YUL-Cartierville" in line and "❯" in line)
    assert "+6.0" in row  # the link's median SNR
    assert "1×" in row  # samples
    assert "T" in row  # trace evidence tag
    assert "⋯ 1" in row  # one link onward (YUL — Alice)


def test_atlas_empty_graph_renders_guidance() -> None:
    """With no evidence at all the screen explains how the atlas fills up."""
    topo = MeshTopology(US, contacts=[])
    screen = _screen(topo)
    body = _plain(screen.render_body(80))
    assert "no evidence to draw yet" in body
    assert "trace" in body


# --- walking ------------------------------------------------------------------------------


def test_atlas_enter_walks_and_grows_the_trail() -> None:
    """Enter focuses the highlighted neighbour; the breadcrumb trail reads the walk."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # walk to YUL (our only neighbour)
    assert screen._focus == topo.canonical(YUL.public_key)
    body = _plain(screen.render_body(80))
    assert "Homestead › YUL-Cartierville" in body  # the trail
    assert "repeater" in body and "1 hop out" in body
    assert "Alice" in body  # YUL's onward neighbour is now a row
    assert "⌫ back" in body  # the row leading home is marked


def test_atlas_backspace_steps_back_along_the_trail() -> None:
    """⌫ pops the trail one step; at the trail's start it does nothing."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")
    assert len(screen._trail) == 2
    screen.handle("backspace")
    assert screen._trail == [topo.self_id]
    screen.handle("backspace")  # already home — inert
    assert screen._trail == [topo.self_id]


def test_atlas_walking_into_the_back_node_pops_instead_of_growing() -> None:
    """Enter on the trail-back row retraces rather than appending a ping-pong walk."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # us → YUL
    rows = screen._rows()
    screen._index = rows.index(topo.self_id)  # highlight the row leading back home
    screen.handle("enter")
    assert screen._trail == [topo.self_id]  # popped, not [us, yul, us]


def test_atlas_home_refocuses_us() -> None:
    """Home resets the walk to our own node from anywhere."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # us → YUL
    screen.handle("home")
    assert screen._trail == [topo.self_id]


def test_atlas_came_from_anchors_west_on_the_canvas() -> None:
    """The node the trail arrived from sits due west of the focus."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # focus YUL; we came from us
    placed = screen._place_neighbours(100, 40, 60.0, 30.0)
    assert placed[topo.self_id] == (40, 40)  # cx - rx, level with the centre
    alice = topo.canonical(ALICE.public_key)
    assert placed[alice][0] > 100  # everyone else fans east


# --- find ---------------------------------------------------------------------------------


def test_atlas_find_lists_matches_and_teleports() -> None:
    """Typing filters every known node; Enter focuses the match and restarts the trail."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    for ch in "ali":
        screen.handle("text", ch)
    assert "find: ali" in screen.footer_hint
    body = _plain(screen.render_body(80))
    assert "Matches" in body and "Alice" in body
    assert "2 hops out" in body  # the match row says how far away it sits
    assert "1 match" in screen.title

    screen.handle("enter")
    alice = topo.canonical(ALICE.public_key)
    assert screen._focus == alice
    assert screen._trail == [alice]  # a teleport restarts the trail
    assert screen._filter == ""


def test_atlas_find_marks_islands() -> None:
    """A match with no path to us reads island, and focusing it says why."""
    screen = _screen(_topo(with_island=True))
    screen.render_body(80)
    for ch in "c3":
        screen.handle("text", ch)
    body = _plain(screen.render_body(80))
    assert "island" in body
    screen.handle("enter")
    body = _plain(screen.render_body(80))
    assert "island — no observed path to you" in body


def test_atlas_esc_peels_find_then_dismisses() -> None:
    """Esc clears an active find first; the next Esc leaves the screen."""
    import asyncio

    screen = _screen(_topo())
    screen.render_body(80)
    screen.handle("text", "a")

    async def drive() -> object:
        screen.future = asyncio.get_running_loop().create_future()
        screen.handle("escape")  # peels the filter
        assert screen._filter == "" and not screen.future.done()
        screen.handle("escape")  # dismisses
        return await screen.future

    assert asyncio.run(drive()) is None


# --- rebuild ------------------------------------------------------------------------------


def test_atlas_rebuild_refreshes_the_evidence_and_keeps_the_trail() -> None:
    """^R re-reads storage; the trail survives where its nodes still exist."""
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
    assert screen._trail == [grown.self_id]
