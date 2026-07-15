"""Trophy-case dialog tests: one record's full story, headless.

Driven like the other screen tests — ``render_body`` is pure lines-out and ``handle``
pure state — so the stats lanes, the route, and the renamed *Trace this path* action
are all assertable without a terminal.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from meshterm.persistence.repository import DiscoveredPath
from meshterm.services.records import CATEGORY_BY_ID
from meshterm.ui.records_screen import RecordDialog, WalkVertex
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


def _plain(lines: list[str]) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines))


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
    assert body.count("★") == 2  # our node marks both endpoints of the round trip
    assert "labels = hash byte" in body  # the graph's caption
    assert any("⠀" <= ch <= "⣿" for ch in body)  # braille edges are drawn


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
    assert "far point  1.5 km" in body
    assert "far point  1.5 km  Far" in body


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
