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
    def __init__(self) -> None:
        self.repaints = 0

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
    screen = AtlasScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={
            topo.canonical(YUL.public_key): YUL,
            topo.canonical(ALICE.public_key): ALICE,
        },
        self_label="Homestead",
    )
    screen.note_viewport(cell_h)  # the frame records this before every real paint
    return screen


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
    assert "3d" * 6 in row  # the whole 12-hex id, not a truncated prefix…
    assert "…" not in row  # …and nothing about it elided


def test_atlas_link_row_shrinks_the_key_before_the_name() -> None:
    """A long name keeps its letters; the key gives ground first, on a byte boundary.

    When the row can't hold both a long name and the full 12-hex id, the key truncates to
    its lit hash plus a ``…`` (an even, whole-byte count) rather than the name losing
    characters — and the hash prefix is never the part dropped.
    """
    long_repeater = Contact(
        name="Repeater-Downtown-01", public_key="d4c3b2a1f0e9" + "00" * 26, node_type=2
    )
    topo = MeshTopology(US, contacts=[long_repeater])
    node = topo.canonical(long_repeater.public_key)
    topo.add_walk([topo.self_id, node], snrs=[4.0], when=utcnow(), source="trace")
    screen = AtlasScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={node: long_repeater},
        self_label="Homestead",
        prefix_bytes=1,  # the first byte (``d4``) is the addressed hash
    )
    screen.note_viewport(24)
    row = next(
        line for line in _plain(screen.render_body(68)).split("\n")
        if "Repeater-Downtown-01" in line and "❯" in line  # the list row, not a canvas label
    )
    assert "Repeater-Downtown-01" in row  # the name shows whole, not clipped
    assert "…" in row  # the key is the lane that gave ground
    assert "d4c3" in row  # its lit hash (and a byte or two more) survives
    assert "d4c3b2a1f0e9" not in row  # …but not the whole id — it was truncated


def test_atlas_graph_labels_names_in_full_when_the_canvas_has_room() -> None:
    """A neighbour's name is drawn whole on the canvas, not clipped to a flat short cap.

    The fan pulls west of the edge and each label clamps to the room actually there, so a
    long contact name keeps its letters wherever the canvas can hold it.
    """
    long_names = [
        Contact(name="YUL-Polytechnique", public_key="e8" * 32, node_type=2),
        Contact(name="Repeater-Downtown-01", public_key="d4" * 32, node_type=2),
    ]
    topo = MeshTopology(US, contacts=long_names)
    when = utcnow()
    for c, snr in zip(long_names, (8.0, 1.0)):
        topo.add_walk([topo.self_id, topo.canonical(c.public_key)], snrs=[snr], when=when,
                      source="trace")
    screen = AtlasScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={topo.canonical(c.public_key): c for c in long_names},
        self_label="Homestead",
    )
    screen.note_viewport(24)
    # The canvas rows are everything above the legend line.
    lines = _plain(screen.render_body(80)).split("\n")
    canvas = "\n".join(lines[: next(i for i, l in enumerate(lines) if "edge = SNR" in l)])
    assert "YUL-Polytechnique" in canvas  # 17 chars, drawn whole — no "YUL-Polytechniq…"
    assert "Repeater-Downtown-01" in canvas  # 20 chars, whole


def test_atlas_selected_link_lights_the_route_that_reaches_it() -> None:
    """Selecting a link draws its edge *and* the approach into the focus at full strength.

    With every link stale (so an un-highlighted edge fades to half), the selected
    neighbour's edge and the came_from → focus approach are drawn undimmed — the lit
    route — while an unselected neighbour's edge stays faded.
    """
    from meshterm.ui.atlas_screen import _snr_rgb

    stale = utcnow() - timedelta(days=10)  # older than a week → _freshness 0.5
    yul = Contact(name="YUL", public_key="3d" * 32, node_type=2)
    alice = Contact(name="Alice", public_key="b2" * 32)
    bob = Contact(name="Bob", public_key="c4" * 32)
    topo = MeshTopology(US, contacts=[yul, alice, bob])
    y = topo.canonical(yul.public_key)
    topo.add_walk([topo.self_id, y], snrs=[10.0], when=stale, source="trace")   # approach: green
    topo.add_walk([y, topo.canonical(alice.public_key)], snrs=[0.0], when=stale, source="trace")   # amber
    topo.add_walk([y, topo.canonical(bob.public_key)], snrs=[-15.0], when=stale, source="trace")   # red
    screen = AtlasScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={topo.canonical(c.public_key): c for c in (yul, alice, bob)},
        self_label="Homestead",
    )
    screen.note_viewport(24)
    screen.render_body(80)
    screen.handle("enter")  # walk to YUL — now came_from is us, the approach edge
    # Land the selection on Alice (a fan neighbour, not the ⌫-back row).
    while screen._rows()[screen._index] != topo.canonical(alice.public_key):
        screen.handle("down")

    ansi_lines = screen.render_body(80)
    legend = next(i for i, l in enumerate(ansi_lines) if "edge = SNR" in _plain([l]))
    canvas = "".join(ansi_lines[:legend])

    def code(rgb: tuple[int, int, int]) -> str:
        return f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"

    def scaled(rgb: tuple[int, int, int], f: float) -> tuple[int, int, int]:
        return tuple(max(0, min(255, round(c * f))) for c in rgb)

    assert code(_snr_rgb(10.0)) in canvas          # approach edge, full-strength green
    assert code(_snr_rgb(0.0)) in canvas           # Alice's edge, full-strength amber
    assert code(scaled(_snr_rgb(-15.0), 0.5)) in canvas   # Bob's edge stays faded…
    assert code(_snr_rgb(-15.0)) not in canvas     # …and never reaches full strength


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
    # The focus line reads name (hash) — the glyph carries the type, not a spelled-out kind.
    assert "YUL-Cartierville (3d)" in body and "1 hop out" in body
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


def test_atlas_walking_to_an_earlier_node_drops_the_loop() -> None:
    """Revisiting a node already on the trail truncates the stack to its first appearance,
    dropping the circular stretch walked to get back there."""
    topo = MeshTopology(US, contacts=[YUL, ALICE])
    yul = topo.canonical(YUL.public_key)
    alice = topo.canonical(ALICE.public_key)
    when = utcnow()
    topo.add_walk([topo.self_id, yul], snrs=[6.0], when=when, source="trace")
    topo.add_walk([yul, alice], snrs=[-2.0], when=when, source="packet")
    topo.add_walk([topo.self_id, alice], snrs=[3.0], when=when, source="trace")  # closes the loop
    screen = _screen(topo)

    def walk_to(node: str) -> None:
        screen.render_body(80)
        screen._index = screen._rows().index(node)
        screen.handle("enter")

    walk_to(yul)  # us → YUL
    walk_to(alice)  # YUL → Alice
    assert screen._trail == [topo.self_id, yul, alice]
    walk_to(yul)  # Alice → YUL: loops back, so Alice is dropped, not re-appended
    assert screen._trail == [topo.self_id, yul]


def test_atlas_trail_drops_the_head_not_the_tail_when_narrow() -> None:
    """A trail too long for the line loses its head behind a leading …, keeping the focus."""
    screen = _screen(_topo())
    us = screen._topo.self_id
    yul = screen._topo.canonical(YUL.public_key)
    alice = screen._topo.canonical(ALICE.public_key)
    screen._trail = [us, yul, alice]
    text = screen._trail_text(20).plain  # too narrow for the whole "Homestead › … › Alice"
    assert text.startswith("…")
    assert text.endswith("Alice")  # the focus is always kept
    assert "Homestead" not in text  # the head was dropped, not the tail


def test_atlas_trail_names_carry_their_node_hues() -> None:
    """Trail names take the per-node key hue (ours the white you-style), the focus bold."""
    from meshterm.ui.theme import node_style

    screen = _screen(_topo())
    us = screen._topo.self_id
    yul = screen._topo.canonical(YUL.public_key)
    screen._trail = [us, yul]
    text = screen._trail_text(80)
    plain = text.plain
    yul_at = plain.index("YUL-Cartierville")
    assert any(
        s.start <= yul_at < s.end and "bold" in str(s.style) and node_style(yul) in str(s.style)
        for s in text.spans
    )


def test_atlas_home_refocuses_us() -> None:
    """Home resets the walk to our own node from anywhere."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # us → YUL
    screen.handle("home")
    assert screen._trail == [topo.self_id]


def test_atlas_came_from_anchors_west_and_the_fan_stays_east() -> None:
    """The trail-back node sits at the far west; every fan node east of the focus."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # focus YUL; we came from us
    alice = topo.canonical(ALICE.public_key)
    fx, fy = screen._focus_pos(80, 12)
    placed = screen._place_neighbours(80, 12, [alice], topo.self_id, False)
    bx, by = placed[topo.self_id]
    assert bx < fx and by > fy  # back home: far left, ducked under the focus label
    assert placed[alice][0] > fx  # the fan is east of the focus
    assert fx <= (80 * 2) // 3  # and the focus itself leans left


def _hub_topo(spokes: int) -> MeshTopology:
    """Us at the centre of a ``spokes``-neighbour hub, strengths descending."""
    topo = MeshTopology(US, contacts=[])
    when = utcnow()
    for i in range(spokes):
        node = f"{i:02x}" * 6
        for _ in range(spokes - i):  # more samples = stronger, so the order is fixed
            topo.add_walk([topo.self_id, node], snrs=[5.0], when=when, source="trace")
    return topo


def test_atlas_collapses_the_weak_links_into_one_ellipsis_marker() -> None:
    """Beyond the area's capacity, weaker neighbours fold into a single ``…`` node."""
    screen = AtlasScreen(
        session=_FakeSession(), topo=_hub_topo(14), contacts={}, self_label="us"
    )
    screen.note_viewport(20)
    body = _plain(screen.render_body(80))
    assert "weaker" in body  # the collapsed marker is labelled "+n weaker"
    canvas_part = body.split("Links")[0]
    assert "…" in canvas_part
    # The list still names every neighbour — selection is the list's job.
    assert len(screen._rows()) == 14


def test_atlas_fan_stays_sparse_and_collapses_the_rest() -> None:
    """The fan is kept deliberately sparse: even a modest hub sheds its weakest links."""
    screen = AtlasScreen(
        session=_FakeSession(), topo=_hub_topo(8), contacts={}, self_label="us"
    )
    screen.note_viewport(20)
    canvas_part = _plain(screen.render_body(80)).split("Links")[0]
    assert "…" in canvas_part and "weaker" in canvas_part  # not all eight are drawn


def test_atlas_labels_non_selected_nodes_to_the_right_of_their_icon() -> None:
    """A fan node is named just to the right of its marker — the walk's reading way."""
    from meshterm.ui.mapcanvas import MapCanvas

    screen = _screen(_topo())
    canvas = MapCanvas(80, 12)
    canvas.marker(40, 20, "●", (255, 255, 255))  # a marker with room to its east
    screen._label_right(canvas, 40, 20, "Bravo", (200, 200, 200))
    marker_cx = 40 >> 1
    assert canvas._label_cells  # the name landed
    assert all(cx > marker_cx for cx, _cy in canvas._label_cells)  # every cell east


def test_atlas_selecting_a_collapsed_row_lights_the_ellipsis_with_its_name() -> None:
    """Highlighting a weak (collapsed) row surfaces its name at the ``…`` marker."""
    screen = AtlasScreen(
        session=_FakeSession(), topo=_hub_topo(14), contacts={}, self_label="us"
    )
    screen.note_viewport(20)
    screen.render_body(80)
    screen._index = len(screen._rows()) - 1  # the weakest row, surely collapsed
    body = _plain(screen.render_body(80))
    weakest = screen._rows()[-1][:8]
    canvas_part = body.split("Links")[0]
    assert weakest in canvas_part  # the ellipsis marker took the selection's label
    assert "weaker" not in canvas_part  # ...replacing the "+n weaker" count


def test_atlas_body_fits_the_viewport_and_windows_the_list() -> None:
    """The screen never outgrows the frame; only the link list scrolls, marked."""
    screen = AtlasScreen(
        session=_FakeSession(), topo=_hub_topo(16), contacts={}, self_label="us"
    )
    screen.note_viewport(22)
    lines = screen.render_body(80)
    assert len(lines) <= 22  # canvas + chrome + list window == the viewport
    body = _plain(lines)
    assert "↓" in body and "more" in body  # the window marks the rows below
    assert "Links" in body


def test_atlas_pgdn_pages_the_highlight_by_the_list_window() -> None:
    """PgUp/PgDn stride by the list window, and the window follows the highlight."""
    screen = AtlasScreen(
        session=_FakeSession(), topo=_hub_topo(16), contacts={}, self_label="us"
    )
    screen.note_viewport(22)
    screen.render_body(80)
    stride = screen._list.page
    assert stride >= 1
    screen.handle("pagedown")
    assert screen._index == min(15, stride)
    for _ in range(6):
        screen.handle("pagedown")
    assert screen._index == 15  # clamped at the last row
    body = _plain(screen.render_body(80))
    assert "↑" in body and "more" in body  # rows scrolled off above are counted


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
