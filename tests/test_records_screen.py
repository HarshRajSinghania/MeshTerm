"""Trophy-case dialog tests: one record's full story, headless.

Driven like the other screen tests — ``render_body`` is pure lines-out and ``handle``
pure state — so the stats lanes, the route, and the renamed *Trace this path* action
are all assertable without a terminal.
"""

from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.persistence.repository import DiscoveredPath, Repository
from meshterm.services.records import CATEGORIES, CATEGORY_BY_ID
from meshterm.ui.records_screen import RecordDialog, WalkVertex, open_records
from meshterm.ui.surface import TuiUi
from meshterm.ui.tui import frame
from meshterm.ui.tui.prompt import TypedConfirmDialog
from meshterm.ui.tui.screen import CANCEL
from meshterm.ui.tui.select import SelectScreen
from meshterm.ui.tui.session import TuiSession
from meshterm.ui.widgets import node_marker, self_marker

HUB_ID, FAR_ID = "3d63c6429436", "f2c24f54551e"


def _loop_shape() -> list[WalkVertex]:
    """A three-point circuit — us and two positioned hops — enough to enclose an area."""
    sglyph, scolor = self_marker()
    hglyph, hcolor = node_marker(2)  # a repeater
    nglyph, ncolor = node_marker(1)  # a plain node
    return [
        WalkVertex(0.0, 0.0, sglyph, scolor, True),
        WalkVertex(2.0, 1.0, hglyph, hcolor, False),
        WalkVertex(1.0, 3.0, nglyph, ncolor, False),
    ]


from tests.conftest import plain as _plain  # THE strip-and-join screen reader


def _resolve(hop: str) -> str:
    return {HUB_ID: "YUL-Cartierville", FAR_ID: "Far"}.get(hop, hop)


def _record(**kw) -> DiscoveredPath:
    defaults = dict(
        id=1, category="grand_tour", width_bytes=1, spec="3d,f2",
        route=(HUB_ID, FAR_ID), score=2.0,
        stats={
            "hop_count": 2, "distinct_nodes": 2, "repeats": False, "min_snr": 6.0,
            "km_travelled": 3.2, "km_complete": True, "far_km": 1.5, "rtt_ms": 250.0,
        },
        app_version="0.1.0",
        discovered_at=datetime(2026, 7, 12, 14, 30, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return DiscoveredPath(**defaults)


def _dialog(record: DiscoveredPath, rank: int = 1, **kw) -> RecordDialog:
    return RecordDialog(
        record, CATEGORY_BY_ID[record.category], rank,
        resolve=_resolve, device_label="Homestead", device_hash=None, **kw,
    )


def test_record_dialog_shows_stats_route_and_the_trace_action() -> None:
    """Score in the category's unit, the resolved route bracketed by us, and actions."""
    body = _plain(_dialog(_record()).render_body(60))
    assert "2 nodes" in body            # the score, in the discipline's own unit
    assert "YUL-Cartierville" in body   # a resolved relay on the route
    assert "Homestead" in body          # us, bracketing the walked route
    assert "Trace this path" in body    # the renamed action (was "Walk again")
    assert "Walk again" not in body
    assert "Delete record" in body
    assert "Back" in body


def test_record_dialog_draws_the_walk_as_a_route_graph() -> None:
    """The walk is drawn on THE route graph — us starred at both ends, over a caption."""
    body = _plain(_dialog(_record()).render_body(60))
    graph = body[: body.index("labels = hash byte")]
    assert graph.count("★") == 2  # our node marks both endpoints of the round trip
    assert "labels = hash byte" in body  # the graph's caption
    assert any("⠀" <= ch <= "⣿" for ch in body)  # braille edges are drawn


def test_record_graph_is_only_as_tall_as_one_lane_needs() -> None:
    """A walk is one path: the marker row, plus a label row only where a label landed.

    The shared widget's default floor (5) reserves room for a fan of alternatives; nothing
    here ever fans, so the walk sat in rows of blank canvas and pushed the route and the
    actions down the card for nothing.
    """
    for record in (_record(), _record(route=(HUB_ID, FAR_ID, HUB_ID, FAR_ID, HUB_ID))):
        lines = _dialog(record)._graph_lines(60)  # however many relays: still one lane
        assert 0 < len(lines) <= 3
        assert all(_plain([line]).strip() for line in lines)  # not a blank row among them


def test_record_dialog_route_runs_unlabelled_across_the_whole_card() -> None:
    """The route is THE path widget at full width — no ``route`` lane eating twelve cells.

    Under a graph captioned ``you → … → you``, the line *is* the route; the label lane only
    cost hops. It wraps at hop boundaries, so a long walk folds instead of truncating, and
    our own two ends stand on the ★ the graph above already marks us with.
    """
    dialog = _dialog(_record(route=tuple([HUB_ID, FAR_ID] * 4)))
    body = _plain(dialog.render_body(60)).splitlines()
    spec_at = next(i for i, line in enumerate(body) if line.startswith("spec"))
    # The route's own first line: the last one before the spec that isn't a hanging fold.
    route_at = next(i for i in range(spec_at - 1, 0, -1) if not body[i].startswith("  "))
    route = body[route_at:spec_at]
    assert not route[0].startswith("route")  # no label lane
    assert route[0].startswith("★")  # our end opens the walk on the app-wide star…
    assert route[-1].endswith("★")  # …and closes it on the same
    assert "Homestead" not in "".join(route)  # never our name, and never our key
    assert "YUL-Cartierville" in "".join(route)  # the hops themselves are named in full
    assert len(route) > 1  # it folded rather than truncating…
    # …every fold hanging under the step, and the labelled lanes resume at the spec.
    assert all(line.startswith("  ") for line in route[1:])


def test_record_dialog_shows_the_node_type_legend() -> None:
    """The standard node-type key sits under the graph, so its markers read."""
    body = _plain(_dialog(_record()).render_body(60))
    assert "★ you" in body and "▲ repeater" in body and "◉ sensor" in body


def test_record_graph_marks_a_repeater_relay_with_its_triangle() -> None:
    """A relay whose type is a repeater draws ▲, not the generic ● dot."""
    dialog = _dialog(_record(), type_of=lambda h: 2 if h == HUB_ID else None)
    graph = _plain(dialog._graph_lines(60))
    assert "▲" in graph  # the repeater relay, in its map glyph


def test_record_graph_collapses_a_revisited_route_to_distinct_nodes() -> None:
    """A boomerang that doubles back seats each node once — the depth graph can't stack a
    node on itself — so the revisiting route draws the same graph as its distinct one."""
    looping = _dialog(_record(route=(HUB_ID, FAR_ID, HUB_ID)))
    distinct = _dialog(_record(route=(HUB_ID, FAR_ID)))
    assert looping._graph_lines(60) == distinct._graph_lines(60)
    graph = _plain(looping._graph_lines(60))
    assert "3d" in graph and "f2" in graph  # both distinct relays labelled, neither piled


def test_record_dialog_title_names_the_discipline_and_rank() -> None:
    """The title uses the plain discipline name and the board standing."""
    assert _dialog(_record(), rank=3).title == "Record — Most nodes #3"


def test_record_dialog_marks_incomplete_distance_as_a_lower_bound() -> None:
    """A Longest-distance walk with an unpositioned hop reads ``≥``, never exact."""
    record = _record(
        category="long_haul", score=8.4,
        stats={"km_travelled": 8.4, "km_complete": False,
               "hop_count": 3, "distinct_nodes": 3},
    )
    body = _plain(_dialog(record).render_body(60))
    assert "≥ 8.4 km" in body


def test_record_dialog_enter_commits_the_trace_action() -> None:
    """The cursor opens on Trace this path, so Enter hands back ``"trace"``."""
    dialog = _dialog(_record())
    captured: list = []
    dialog.resolve = captured.append  # type: ignore[method-assign]
    assert dialog._actions[dialog._index] == "trace"
    dialog.handle("enter")
    assert captured == ["trace"]


def test_record_dialog_down_moves_to_delete() -> None:
    """↑/↓ walk the action rows in order."""
    dialog = _dialog(_record())
    dialog.handle("down")
    assert dialog._actions[dialog._index] == "delete"


def test_record_dialog_names_the_far_point() -> None:
    """The reached node's name rides beside the far-point distance."""
    body = _plain(_dialog(_record(), far_label="Far").render_body(60))
    assert re.search(r"far point\s+1\.5 km\s+Far", body)


def test_record_dialog_shows_reliability_when_the_far_node_has_history() -> None:
    """The reliability lane reads the % and its sample count; absent without history."""
    with_rate = _plain(_dialog(_record(), reliability=(0.875, 7, 8)).render_body(60))
    assert re.search(r"reliability\s+88%", with_rate)
    assert "7/8" in with_rate
    without = _plain(_dialog(_record()).render_body(60))
    assert "reliability" not in without


def test_record_dialog_draws_the_enclosed_area_beside_the_stats() -> None:
    """A drawable walk pins its area, with node markers and no labels, right of the stats."""
    lines = _plain(
        _dialog(_record(), far_label="Far", shape=_loop_shape()).render_body(58)
    ).splitlines()
    stats_block = lines[: lines.index("")]  # the lanes, up to the blank before the graph
    block = "\n".join(stats_block)
    assert stats_block[0].startswith("score")  # the drawing rides beside the stats
    assert any(chr(0x2800) <= ch <= chr(0x28FF) for ch in block)  # the area, in braille
    assert "★" in block  # us pinned at the origin
    assert "▲" in block  # the repeater vertex, in its map glyph
    assert "Far" in block  # the far-point name (in the stats, not a drawing label)


def test_record_dialog_drops_the_area_drawing_when_too_narrow() -> None:
    """Below the side-by-side floor the stats reclaim the width; the far name stays."""
    body = _plain(_dialog(_record(), far_label="Far", shape=_loop_shape()).render_body(40))
    lines = body.splitlines()
    score_row = next(line for line in lines if line.startswith("score"))
    assert not any(chr(0x2800) <= ch <= chr(0x28FF) for ch in score_row)  # no drawing
    assert "Far" in body  # the far-point name survives the drop


def test_record_dialog_scrolls_free_of_the_action_cursor() -> None:
    """PgUp/PgDn/Home/End scroll the card; the action cursor stops following."""
    dialog = _dialog(_record(), shape=_loop_shape())
    dialog.note_metrics(40, 10)  # a body taller than the viewport
    assert dialog.cursor_line() is None  # opens at the top, free of the cursor
    dialog.handle("pagedown")
    assert dialog.scroll > 0 and not dialog._follow
    dialog.handle("end")
    assert dialog.scroll == 30  # total 40 − viewport 10
    dialog.handle("home")
    assert dialog.scroll == 0


def test_record_dialog_arrows_follow_the_action_cursor() -> None:
    """An arrow re-arms cursor-follow so the selected action is kept in view."""
    dialog = _dialog(_record())
    dialog.handle("pagedown")  # into free-scroll
    assert not dialog._follow
    dialog.handle("down")
    assert dialog._follow
    dialog.render_body(60)  # the frame renders before reading the cursor
    assert dialog.cursor_line() is not None


# --- the browser loop: dialogs float over the trophy case, never a blank frame ------


@pytest.fixture()
def tui_ctx(tmp_path: Path) -> AppContext:
    """A mock-backed context wired to a headless TUI session (no prompt_toolkit app)."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "records.db")
    ctx = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    ctx.ui = TuiUi(TuiSession())
    yield ctx
    ctx.repo.close()


async def _step_until(predicate, *, limit: int = 200):
    """Yield to the event loop until ``predicate()`` is truthy; return it (or its last value)."""
    value = predicate()
    for _ in range(limit):
        if value:
            return value
        await asyncio.sleep(0)
        value = predicate()
    return value


def _trophy_case(session, *, other_than=None):
    """The trophy-case browser on top of the stack, if it is the frontmost screen."""
    top = session.top
    if isinstance(top, SelectScreen) and top.title == "Trophy case" and top is not other_than:
        return top
    return None


async def test_delete_all_confirm_floats_over_the_browser(tui_ctx) -> None:
    """The trophy case stays drawn full-frame behind the "Delete all records" confirm.

    Regression: the browser was run *then popped* before the confirm opened, so the confirm
    floated over a blank frame — the background "cleared". Re-pushing the browser as the
    backdrop keeps it the full-frame base while the red typed-delete gate floats over it.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.repo.record_discovery(
        "grand_tour", 1, "3d,f2", (HUB_ID, FAR_ID),
        score=2.0, stats={"hop_count": 2, "distinct_nodes": 2}, app_version="0.1.0",
    )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None, "the trophy case list never opened"

        browser.resolve(("del_all", None, 0, None))  # choose "Delete all records"
        confirm = await _step_until(
            lambda: session._float_layers()[0] if session._has_float() else None
        )
        assert isinstance(confirm, TypedConfirmDialog)  # the red typed-delete gate
        assert session._base_screen() is browser  # the browser is still the full-frame base

        confirm.resolve(CANCEL)  # Esc — back out without deleting
        again = await _step_until(lambda: _trophy_case(session, other_than=browser))
        assert again is not None, "the browser never reopened after the confirm"
        again.resolve(("back", None, 0, None))  # leave the trophy case
        result = await task
    finally:
        if not task.done():
            task.cancel()

    assert result == {"records": 1}  # backed out — the record still stands


async def test_delete_a_disciplines_records_is_a_popup_over_the_browser(tui_ctx) -> None:
    """Picking a discipline to delete floats a popup over the browser, not a full-screen list.

    The discipline picker is a floating :class:`SelectScreen`; with the browser kept on the
    stack it draws as a centered popup over the visible trophy case (``_base_screen`` stays
    the browser) instead of replacing the whole frame.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.repo.record_discovery(
        "grand_tour", 1, "3d,f2", (HUB_ID, FAR_ID),
        score=2.0, stats={"hop_count": 2, "distinct_nodes": 2}, app_version="0.1.0",
    )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None

        browser.resolve(("del_cat", None, 0, None))  # "Delete a discipline's records…"
        picker = await _step_until(
            lambda: session._float_layers()[0] if session._has_float() else None
        )
        assert isinstance(picker, SelectScreen)  # a floating popup, not the frame
        assert picker.title == "Delete a discipline's records"
        assert session._base_screen() is browser  # the browser stays behind it

        picker.resolve(CANCEL)  # Esc — abandon the pick
        again = await _step_until(lambda: _trophy_case(session, other_than=browser))
        assert again is not None
        again.resolve(("back", None, 0, None))
        result = await task
    finally:
        if not task.done():
            task.cancel()

    assert result == {"records": 1}


async def test_a_board_row_spends_its_cells_on_the_walk(tui_ctx) -> None:
    """A record row leads with rank/day/score, then the walk with both ends bare.

    Every record is a boomerang, so naming ourselves at both ends said nothing twice a row
    and cost more cells than the whole score lane; the ends go to ``★`` and the score lane
    is fitted to this board's own widest score instead of a fixed twelve.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.repo.record_discovery(
        "grand_tour", 1, "3d,f2", (HUB_ID, FAR_ID, HUB_ID),
        score=2.0, stats={"hop_count": 3, "distinct_nodes": 2}, app_version="0.1.0",
    )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None
        label = next(
            choice.label for choice in browser._choices()
            if isinstance(choice.value, tuple) and choice.value[0] == "open"
        )
        row = label.plain
        assert row.startswith("#1 ")  # the standing on the board
        assert "2 nodes" in row  # the score, in the discipline's unit
        assert row.count("★") == 2  # our two ends, bare — never named twice a row
        assert "Homestead" not in row  # …so the device name is nowhere on the line
        # And those ends are *us*, in the you white — nothing here is ours to compose, so
        # there is no "not yours" fade to wear.
        stars = [s for s in label.spans if row[s.start : s.end] == "★"]
        assert [str(s.style) for s in stars] == ["you", "you"]
        assert "3d" in row and "f2" in row  # the hops, at the record's own hash width
        # The lanes before the walk are held to what they say: a score lane fitted to this
        # board's widest score (no dead padding), and the day without the minute the dialog
        # carries — every cell they don't spend is a hop the route gets to show.
        assert "2 nodes  ★" in row
        assert ":" not in row[: row.index("★")]
    finally:
        if not task.done():
            task.cancel()


async def test_the_board_pins_its_discipline_heading_and_description(tui_ctx) -> None:
    """Scrolling into a board keeps its ``── discipline ──`` heading *and* description overhead.

    Every discipline heading is followed by its word-wrapped description — what that board
    scores, which is exactly what a reader partway down it needs. The two pin as one block:
    with every separator a pinning candidate, the *last muted line* under a heading won the
    top row on its own and the heading never stuck — the first discipline's least of all,
    since its description is what the very first scrolled row sits under.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    for i in range(6):  # one full board, so its records outlast a short viewport
        ctx.repo.record_discovery(
            "grand_tour", 1, f"3d,{i:02x}", (HUB_ID, FAR_ID),
            score=float(i + 1), stats={"hop_count": 2, "distinct_nodes": 2},
            app_version="0.1.0",
        )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None
        # Exactly the six disciplines are landmarks, each block led by its own heading and
        # carrying the description written under it — nothing else is a candidate.
        browser.render_body(72)
        blocks = [[_plain([line]).strip() for line in rows]
                  for _idx, rows in browser._sticky_headers]
        assert [rows[0] for rows in blocks] == [f"── {c.icon} {c.title} ──" for c in CATEGORIES]
        assert all(len(rows) >= 2 for rows in blocks)  # each carries its description too
        # Highlighting deep in the one populated board scrolls its heading off the top of a
        # short viewport; the heading leads what pins, its description under it.
        for _ in range(4):
            browser.handle("down")
        visible, above, _below = frame._visible_slice(browser, browser.render_body(72), 10)
        board = CATEGORY_BY_ID["grand_tour"]
        assert _plain([visible[0]]).strip() == f"── {board.icon} {board.title} ──"
        assert _plain([visible[1]]).strip().startswith(board.description[:20])
        assert above is True
        browser.resolve(("back", None, 0, None))
        await task
    finally:
        if not task.done():
            task.cancel()


async def test_trace_this_path_unwinds_to_the_menu_not_the_browser(
    tui_ctx, monkeypatch
) -> None:
    """After the Trace path hand-off, ``open_records`` returns instead of reopening.

    The anti-deep-stack rule: trophy case → Trace this path → (trace screen closes)
    must land on the main menu, not back in the browser — otherwise bouncing between
    the boards and the trace screens piles up states the user has to Esc through.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.repo.record_discovery(
        "grand_tour", 1, "3d,f2", (HUB_ID, FAR_ID),
        score=2.0, stats={"hop_count": 2, "distinct_nodes": 2}, app_version="0.1.0",
    )
    walked: list[str] = []

    async def fake_trace_path(ctx_, spec=""):  # noqa: ANN001
        walked.append(spec)
        return 0

    monkeypatch.setattr("meshterm.ui.trace_screen.open_trace_path", fake_trace_path)

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None
        record = ctx.repo.discoveries("grand_tour")[0]
        browser.resolve(("open", CATEGORY_BY_ID["grand_tour"], 1, record))
        dialog = await _step_until(
            lambda: session._float_layers()[0] if session._has_float() else None
        )
        assert isinstance(dialog, RecordDialog)
        dialog.resolve("trace")  # "Trace this path"
        result = await task  # …and the flow returns; no browser re-entry to unwind
    finally:
        if not task.done():
            task.cancel()

    assert walked == ["3d,f2"]  # the record's spec went straight to Trace path
    assert result == {"records": 1}


async def test_browser_cuts_every_row_the_way_it_cuts_the_highlighted_one(tui_ctx) -> None:
    """Unselected walks are cut, not middle-elided — the highlight only adds the shift.

    Both ends of a record's walk are the same ``★`` on every row (a record is a boomerang),
    so the ``⋯`` rescue that saves a route's two endpoints from a right truncation would buy
    back nothing here while spending cells the walk's *front* — the part that differs from row
    to row — was going to get. Every row therefore carries the whole line and is cut at the
    lane, exactly as the highlighted row is at shift zero.
    """
    ctx = tui_ctx
    long_route = tuple(f"{byte:02x}c24f54551e" for byte in range(0x20, 0x2c))
    for rank, route in enumerate((long_route, long_route[::-1]), start=1):
        ctx.repo.record_discovery(
            "grand_tour", 1, f"{route[0][:2]},f2", route,
            score=float(20 - rank), stats={"hop_count": len(route), "distinct_nodes": len(route)},
            app_version="0.1.0",
        )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(ctx.ui.session))
        assert browser is not None
        rows = [_plain([line]) for line in browser.render_body(72)]
        walks = [row for row in rows if "★" in row]
        assert len(walks) == 2, rows  # one highlighted, one not
        assert not any("⋯" in row for row in walks)  # nothing middle-elides any more
        # Highlighted and not, every walk starts at our star right after the score lane…
        assert all(re.search(r"#\d+ .*nodes  ★ → ", row) for row in walks), walks
        # …and runs on past the lane rather than closing on a rescued second endpoint.
        assert all(row.rstrip().endswith("…") for row in walks), walks
        browser.resolve(("back", None, 0, None))
        await task
    finally:
        if not task.done():
            task.cancel()
