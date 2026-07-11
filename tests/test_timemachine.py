"""Time Machine tests: the history queries, the bucketing math, and the pages.

Pages are built against a real (temporary) repository seeded with canned
observations, then rendered headless — the dashboard tests' approach.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from meshterm.core.models import Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.ui.timemachine_screen import (
    TimeMachineScreen,
    _mesh_sections,
    _node_sections,
    _snr_cell_style,
    bucket_medians,
    bucketize,
)

NODE = "3d" * 6


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _seeded_repo(tmp_path: Path) -> Repository:
    """A repository with one chatty node (SNR fading), one newcomer, one packet row."""
    repo = Repository(tmp_path / "tm.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow()
    for hours_ago in range(48, 0, -2):
        repo.record_observation(
            run,
            Observation(
                node=NODE, name="YUL", kind="advert",
                snr=8.0 if hours_ago > 24 else -2.0, rssi=-95.0,
                observed_at=now - timedelta(hours=hours_ago),
            ),
        )
    repo.record_observation(
        run,
        Observation(node="f7" * 6, name="Newcomer",
                    observed_at=now - timedelta(hours=3)),
    )
    # A packet row: counted in daily totals, excluded from per-node reception.
    repo.record_observation(
        run,
        Observation(node=NODE, kind="packet", snr=1.0, path="",
                    observed_at=now - timedelta(hours=1)),
    )
    return repo


def _plain(renderables, width: int = 90) -> str:
    from rich.console import Group

    from meshterm.ui.tui.render import render_lines

    text = "\n".join(render_lines(Group(*renderables), width))
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# --- the repository queries -------------------------------------------------------------


def test_node_observations_window_and_packet_exclusion(tmp_path: Path) -> None:
    """The per-node feed is oldest-first, windowed, and free of packet rows."""
    repo = _seeded_repo(tmp_path)
    everything = repo.node_observations(NODE)
    assert len(everything) == 24  # 48 h of every-2-h adverts; the packet row excluded
    assert everything[0].observed_at < everything[-1].observed_at
    day = repo.node_observations(NODE, since=utcnow() - timedelta(days=1))
    assert 0 < len(day) < len(everything)
    repo.close()


def test_first_seen_orders_arrivals_and_windows(tmp_path: Path) -> None:
    """Arrivals report each node's first-ever stamp, newest arrival first."""
    repo = _seeded_repo(tmp_path)
    arrivals = repo.first_seen()
    assert [node for node, _n, _f in arrivals] == ["f7" * 6, NODE]
    assert arrivals[0][1] == "Newcomer"
    recent = repo.first_seen(since=utcnow() - timedelta(days=1))
    assert [node for node, _n, _f in recent] == ["f7" * 6]
    repo.close()


def test_daily_activity_counts_packets_and_nodes(tmp_path: Path) -> None:
    """Daily totals count every row but only identified non-packet nodes."""
    repo = _seeded_repo(tmp_path)
    days = repo.daily_activity()
    assert sum(d[1] for d in days) == 26  # 24 adverts + newcomer + the packet row
    assert max(d[2] for d in days) == 2  # at most YUL + Newcomer on one day
    assert days == sorted(days)  # oldest first
    repo.close()


def test_hourly_activity_groups_by_utc_hour(tmp_path: Path) -> None:
    """The rhythm feed lands each observation in its UTC hour, windowed on demand."""
    repo = Repository(tmp_path / "hh.db")
    run = repo.start_run("monitor", {}, None)
    base = utcnow().replace(hour=5, minute=0, second=0, microsecond=0)
    for minutes in (0, 10, 20):
        repo.record_observation(
            run, Observation(node="aa" * 6, observed_at=base + timedelta(minutes=minutes))
        )
    repo.record_observation(
        run, Observation(node="aa" * 6, observed_at=base.replace(hour=17))
    )
    counts = repo.hourly_activity()
    assert counts[5] == 3 and counts[17] == 1 and sum(counts) == 4
    assert sum(repo.hourly_activity(since=base.replace(hour=6))) == 1
    repo.close()


# --- the math ---------------------------------------------------------------------------


def test_bucketize_and_medians_split_the_span() -> None:
    """Counts and medians land in the right slices, edges clamped."""
    now = utcnow()
    start = now - timedelta(hours=4)
    stamps = [start, now - timedelta(hours=3), now - timedelta(minutes=1), now]
    assert bucketize(stamps, start, now, 4) == [1, 1, 0, 2]
    pairs = [(start, 0.0), (start, 10.0), (now, -4.0)]
    medians = bucket_medians(pairs, start, now, 2)
    assert medians == [5.0, -4.0]


def test_snr_band_hangs_negative_readings_below_zero() -> None:
    """All-negative SNR medians hang from a grey zero ceiling, coloured by quality."""
    from meshterm.ui.braillechart import chart_span, timeline_rows

    medians = [-12.0, None, -4.0, -8.0]
    assert chart_span(medians) == (-12.0, 0.0)  # zero is always on the scale
    rows = timeline_rows(medians, rows=2, style=_snr_cell_style)
    assert len(rows) == 2 and len(rows[0].plain) == 2
    # The worst reading (−12) hangs the full height; −8 dips its tail into the
    # bottom row (right column only) while the shallower −4 stays out of it.
    assert rows[1].plain[0] != chr(0x2800)  # the −12 column reaches the bottom row
    assert rows[1].plain[1] == chr(0x2808)  # −8's tail, no −4 (left-column) dots
    # Cells take the shared SNR palette, driven by their own readings.
    assert rows[0].spans[0].style == _snr_cell_style([-12.0])
    assert rows[0].spans[1].style == _snr_cell_style([-4.0, -8.0])


# --- the pages --------------------------------------------------------------------------


def test_node_page_renders_all_sections(tmp_path: Path) -> None:
    """Volume, SNR band, rhythm, and the record roll-up all render."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    body = _plain(_node_sections(ctx, NODE, "YUL", None, 90))
    assert "Volume" in body and "24 receptions" in body
    assert "SNR" in body and "-2.0" in body and "+8.0" in body
    assert "Rhythm" in body and "local hour" in body
    assert "Record" in body and "median" in body
    assert "advert 24" in body  # kinds breakdown, packet rows absent
    repo.close()


def test_node_page_empty_window_offers_widening(tmp_path: Path) -> None:
    """A window with nothing recorded says so instead of drawing empty charts."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    body = _plain(_node_sections(ctx, "no" * 6, "ghost", timedelta(days=1), 90))
    assert "Nothing recorded in this window" in body
    repo.close()


def test_mesh_page_renders_days_rhythm_arrivals_and_ledger(tmp_path: Path) -> None:
    """The overview shows daily charts, the rhythm, the newcomers, and the ledger."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    body = _plain(_mesh_sections(ctx, None, 90, prefix_bytes=2))
    assert "Packets per day" in body and "Nodes per day" in body
    assert "Rhythm" in body and "local hour" in body
    assert "Arrivals" in body and "Newcomer" in body
    # Arrivals are aligned lanes: the hash sits in its own column and the old
    # per-row "first heard" prefix now lives once, in the column header.
    assert "f7" * 6 in body
    assert "FIRST HEARD" in body and "first heard" not in body
    assert "Ledger" in body and "26 observations" in body and "2 nodes" in body
    repo.close()


def test_picker_row_lanes_align_under_the_header() -> None:
    """Picker rows lane up under the header; unknown nodes read as heat-coloured names."""
    from meshterm.core.models import HeardNode
    from meshterm.ui.timemachine_screen import _picker_header, _picker_row

    node = HeardNode(
        node="3d" * 6, name=None, count=42, median_snr=None, best_snr=None,
        last_rssi=None, last_seen=utcnow(),
    )
    row = _picker_row(node, node.name, 10, 2)
    plain = row.plain
    assert "unknown" in plain and "3d" * 6 in plain and "42" in plain
    # A just-heard mystery node reads hot (white), not placeholder-grey.
    assert any(span.style == "#ffffff" for span in row.spans)
    # The hash's routing prefix (2 bytes here) is lit brand, the tail muted.
    hash_at = plain.index("3d" * 6)
    assert any(
        span.style == "brand" and span.start == hash_at and span.end == hash_at + 4
        for span in row.spans
    )
    # Header labels land over their lanes (+2 covers the select pointer column).
    header = _picker_header(10)
    assert header.index("HASH") == plain.index("3d" * 6) + 2
    assert header.index("NAME") == plain.index("unknown") + 2


def test_screen_cycles_windows_and_caches(tmp_path: Path) -> None:
    """`w` moves to the next window (retitling) and each window renders once."""
    calls: list = []

    def build(window, width):
        calls.append(window)
        from rich.text import Text

        return [Text("page")]

    screen = TimeMachineScreen(session=_FakeSession(), label="YUL", build=build)
    assert "7 d" in screen.title
    screen.render_body(80)
    screen.render_body(80)
    assert len(calls) == 1  # cached per window/width
    screen.handle("text", "w")
    assert "30 d" in screen.title
    screen.render_body(80)
    assert len(calls) == 2
