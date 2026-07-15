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
from meshterm.ui.records_screen import RecordDialog

HUB_ID, FAR_ID = "3d63c6429436", "f2c24f54551e"


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


def _dialog(record: DiscoveredPath, rank: int = 1) -> RecordDialog:
    return RecordDialog(
        record, CATEGORY_BY_ID[record.category], rank,
        resolve=_resolve, device_label="Homestead", device_hash=None,
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
