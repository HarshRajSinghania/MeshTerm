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
from meshterm.ui.braillechart import GAP
from meshterm.ui.timemachine_screen import (
    TimeMachineScreen,
    _day_centers,
    _day_columns,
    _day_ticks,
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


def test_day_columns_always_hits_the_exact_width() -> None:
    """The stretched output always lands on exactly 2 × chars, remainder included.

    A plain floor division (the old implementation) drops the remainder, leaving
    the bars short of the axis border and caption sized for the full width — the
    chart reading as shifted left of where its own axis says it ends.
    """
    for n, chars in [(7, 92), (3, 10), (1, 20), (14, 40), (30, 45), (60, 30)]:
        cols = _day_columns(list(range(n)), chars)
        assert len(cols) == chars * 2, (n, chars)


def test_fill_days_shows_gap_days_as_zero_bars() -> None:
    """A quiet day between busy ones is emitted with zero counts, not skipped."""
    from datetime import datetime, timezone

    from meshterm.ui.timemachine_screen import _fill_days

    active = [("2026-07-02", 50, 5), ("2026-07-05", 80, 8)]
    now = datetime(2026, 7, 6, 12, tzinfo=timezone.utc)
    filled = _fill_days(active, datetime(2026, 7, 1, tzinfo=timezone.utc), now)
    assert [iso for iso, _p, _n in filled] == [
        "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05", "2026-07-06",
    ]
    counts = {iso: packets for iso, packets, _n in filled}
    assert counts["2026-07-03"] == 0 and counts["2026-07-04"] == 0  # the gap, now visible
    assert counts["2026-07-02"] == 50 and counts["2026-07-05"] == 80  # the busy days intact


def test_fill_days_never_invents_days_before_recording_began() -> None:
    """A window floor earlier than the first recorded day clamps to that day, not the floor."""
    from datetime import datetime, timezone

    from meshterm.ui.timemachine_screen import _fill_days

    active = [("2026-07-10", 10, 1)]
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    filled = _fill_days(active, datetime(2026, 6, 12, tzinfo=timezone.utc), now)  # a 30 d floor
    assert filled[0][0] == "2026-07-10"   # not the 2026-06-12 floor
    assert filled[-1][0] == "2026-07-12"  # …but still runs through today


def test_day_centers_land_under_each_bar() -> None:
    """A day's centre cell sits within its own bar's span (so a tick points at it)."""
    chars = 60
    n = 7
    centers = _day_centers(n, chars)
    assert len(centers) == n
    base = chars / n
    for i, cell in enumerate(centers):
        assert i * base <= cell <= (i + 1) * base  # inside day i's character span
    assert centers == sorted(centers) and centers[-1] <= chars - 1


def test_day_ticks_shorten_dates_and_align_to_bars() -> None:
    """One tick per day when they fit: month only on the first, bare day numbers after."""
    days = [(f"2026-07-{d:02d}", 10, 3) for d in range(1, 8)]  # Jul 1..7
    chars = 60
    ticks = _day_ticks(days, chars)
    assert [cell for cell, _ in ticks] == _day_centers(len(days), chars)  # under the bars
    labels = [label for _, label in ticks]
    assert labels[0] == "Jul 1"          # month printed once, on the first tick
    assert labels[1:] == ["2", "3", "4", "5", "6", "7"]  # bare day numbers after it


def test_day_ticks_reprint_the_month_on_a_rollover() -> None:
    """The first of a month always carries its month name, so a boundary reads clearly."""
    days = [(iso, 1, 1) for iso in ("2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02")]
    labels = [label for _, label in _day_ticks(days, 60)]
    assert labels == ["Jun 29", "30", "Jul 1", "2"]


def test_day_ticks_prioritise_the_newest_then_the_oldest_day() -> None:
    """With room for almost nothing, the newest day wins, then the oldest.

    A chart squeezed to the minimum width still labels its "now" edge first and
    its far edge second; whatever interior days fit are claimed right to left, so
    the freshest history stays the best annotated.
    """
    days = [(f"2026-06-{d:02d}", 1, 1) for d in range(1, 31)]  # 30 days
    chars = 20
    ticks = _day_ticks(days, chars)
    centers = _day_centers(len(days), chars)
    cols = [cell for cell, _ in ticks]
    assert centers[-1] in cols  # the newest day always keeps its tick
    assert centers[0] in cols   # …and the oldest is claimed right after it


def test_day_columns_widths_differ_by_at_most_one_dot_and_interleave() -> None:
    """An uneven split spreads the wider days through the chart, not to one side.

    13 days over 30 chars (60 dots) pay 12 boundary notches, leaving 48 dots of
    bar that can't divide evenly: bars get 3 or 4 dots. The widths must never
    differ by more than one dot column, and the wide days must mix with the
    narrow ones instead of pooling at either end (the old char-unit split put
    every wide day on the left, every narrow one on the right).
    """
    cols = _day_columns([1] * 13, 30)
    assert len(cols) == 60
    notches = [i for i, c in enumerate(cols) if c is GAP]  # one per day boundary
    assert len(notches) == 12
    edges = [-1, *notches, len(cols)]  # each bar runs between two notches
    widths = [b - a - 1 for a, b in zip(edges, edges[1:])]
    assert len(widths) == 13 and set(widths) == {3, 4}
    first_wide = widths.index(4)
    last_wide = len(widths) - 1 - widths[::-1].index(4)
    assert any(w == 3 for w in widths[first_wide:last_wide])  # narrow days sit between wide ones


def test_day_ticks_pack_the_axis_keeping_both_ends() -> None:
    """More days than labels fit: the axis packs what it can, ends always included.

    Ticks are claimed newest-first, then oldest, then right to left, each keeping
    two blank cells from its neighbours — so a crowded axis stays dense (well past
    the old evenly-thinned handful) and any dropped days come from the interior.
    """
    days = [(f"2026-06-{d:02d}", 1, 1) for d in range(1, 31)]  # 30 days
    chars = 40
    ticks = _day_ticks(days, chars)
    centers = _day_centers(len(days), chars)
    cols = [cell for cell, _ in ticks]
    assert cols[0] == centers[0] and cols[-1] == centers[-1]  # both ends survive
    assert len(ticks) >= 8  # bare day numbers pack far denser than dated labels
    # Every placed label keeps at least two blank cells from the one before it.
    spans = []
    for cell, label in ticks:
        start = max(0, min(chars - len(label), cell - len(label) // 2))
        spans.append((start, start + len(label)))
    assert all(b_start >= a_end + 2 for (_, a_end), (b_start, _) in zip(spans, spans[1:]))


def test_day_columns_notch_the_boundaries_and_keep_the_first_bar_flush() -> None:
    """Notches sit strictly between days; the first bar starts at the axis itself.

    The braille glyphs already carry a left margin, so a leading GAP dot would
    read as double padding against the axis border while the right edge (bar
    flush against the closing border) shows single — the edges must match.
    """
    cols = _day_columns([2, 8, 4], 12)  # 24 dots − 2 notches = 22 of bar: 7+7+8
    assert cols[0] == 2  # flush against the axis — no leading notch
    assert cols[:7] == [2] * 7
    assert cols[7] is GAP
    assert cols[8:15] == [8] * 7
    assert cols[15] is GAP
    assert cols[16:] == [4] * 8  # …and flush against the right border


def test_day_columns_skips_the_notch_for_a_single_character_day() -> None:
    """A day exactly one character wide stays fully lit — nothing to space apart."""
    cols = _day_columns([5, 9], 2)  # 2 days, 2 chars -> exactly 1 char (2 dots) each
    assert GAP not in cols
    assert cols == [5, 5, 9, 9]


def test_day_columns_skips_notches_when_days_outnumber_characters() -> None:
    """More days than character columns: every day is sub-character, no notches."""
    cols = _day_columns(list(range(50)), 30)  # 50 days into 30 chars (<=60 dots)
    assert len(cols) == 60
    assert GAP not in cols


def test_day_chart_notch_reaches_the_axis_but_a_zero_day_keeps_its_baseline() -> None:
    """The notch column is blank down to the axis; a zero-value day stays a grey line.

    Tall day, empty day, tall day: the boundary notches (dots 7 and 15) blank
    their whole dot column so the gap runs clean to the axis border, the first
    bar's very first dot is lit (flush against the axis), and the empty day
    draws the faint zero baseline across its own columns.
    """
    from meshterm.ui.braillechart import timeline_rows

    cols = _day_columns([8, 0, 8], 12)  # 22 dots of bar (7+7+8), notches at 7 and 15
    bottom = timeline_rows(cols, rows=2)[-1]
    assert ord(bottom.plain[0]) & 0x40  # first bar flush: its bottom-left dot is lit
    # Cell 3 holds the first boundary: bar dot on its left, the notch blank on its
    # right — no bottom-right dot, so the gap runs clean to the axis border below.
    assert not (ord(bottom.plain[3]) & 0x80)
    # The empty middle day (dots 8..14) draws the faint zero baseline, in grey.
    zero_cells = bottom.plain[4:7]
    assert all(ord(c) & (0x40 | 0x80) for c in zero_cells)  # baseline dots present
    assert ord(bottom.plain[7]) & 0x40  # the day's last baseline dot…
    assert not (ord(bottom.plain[7]) & 0x80)  # …then the second notch, blank
    zero_spans = [s for s in bottom.spans if 4 <= s.start < 8]
    assert zero_spans and all(s.style == "faint" for s in zero_spans)


# --- the pages --------------------------------------------------------------------------


def test_node_page_renders_all_sections(tmp_path: Path) -> None:
    """Volume, SNR band, rhythm, and the record roll-up all render."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    body = _plain(_node_sections(ctx, NODE, "YUL", None, 90))
    assert "Volume" in body and "24 receptions" in body
    assert "SNR" in body and "-2.0" in body and "+8.0" in body
    assert "Rhythm" in body and "15-min" in body  # 15-minute slices, like the mesh page
    assert "Record" in body and "median" in body
    assert "advert 24" in body  # kinds breakdown, packet rows absent
    repo.close()


def test_node_page_rhythm_is_fifteen_minute_and_shares_the_volume_gutter(tmp_path: Path) -> None:
    """The node rhythm matches the mesh's: a full-day 15-min sweep, gutter-aligned to Volume."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    lines = _plain(_node_sections(ctx, NODE, "YUL", None, 90), width=90).split("\n")

    def border_col(heading: str) -> int:
        start = next(i for i, line in enumerate(lines) if heading in line)
        border = next(line for line in lines[start:] if "└" in line)
        return border.index("└")

    assert border_col("Volume") == border_col("Rhythm")  # shared gutter → aligned left edge
    rhythm = next(i for i, line in enumerate(lines) if "Rhythm" in line)
    caption = next(lines[i + 1] for i in range(rhythm, len(lines)) if "└" in lines[i])
    assert "24 h" in caption  # the full-day sweep closes on 24 h, like the mesh rhythm


def test_time_axis_shortens_to_dates_on_wide_windows_and_times_on_narrow() -> None:
    """A multi-day window labels marks as bare dates; a same-day one, as times."""
    from datetime import datetime, timedelta, timezone

    from meshterm.ui.timemachine_screen import _time_axis

    start = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
    wide = _time_axis(start, start + timedelta(days=10))
    narrow = _time_axis(start, start + timedelta(hours=12))
    assert wide(1.0) == "now" and narrow(1.0) == "now"
    assert any(ch.isalpha() for ch in wide(0.0))  # a month name, e.g. "Jul 1"
    assert ":" in narrow(0.0)                      # a clock time, e.g. "08:00"


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
    assert "Rhythm" in body and "15-min" in body  # 15-minute slices, four per hour
    assert "Arrivals" in body and "Newcomer" in body
    # Arrivals are aligned lanes: the hash sits in its own column and the old
    # per-row "first heard" prefix now lives once, in the column header.
    assert "f7" * 6 in body
    assert "FIRST HEARD" in body and "first heard" not in body
    assert "Ledger" in body and "26 observations" in body and "2 nodes" in body
    repo.close()


def test_mesh_page_day_chart_axis_matches_the_bar_width(tmp_path: Path) -> None:
    """A day chart's bottom border spans exactly as many columns as its bars.

    A rounding mismatch here (the old ``_day_columns``) leaves the border and
    caption sized for a wider chart than the bars actually drawn — the bars
    reading as shifted left of an axis drawn for more columns than exist. The
    border now carries ``┬`` tick marks under the dated columns, so its interior
    is counted as dashes *and* ticks together.
    """
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    lines = _plain(_mesh_sections(ctx, None, 90), width=90).split("\n")
    heading_idx = next(i for i, line in enumerate(lines) if "Packets per day" in line)
    border_idx = next(
        i for i, line in enumerate(lines[heading_idx:], heading_idx)
        if re.fullmatch(r"\s*└[─┬]+┘", line)
    )
    border_interior = len(re.search(r"└([─┬]+)┘", lines[border_idx]).group(1))
    row_line = lines[border_idx - 1]
    content = re.search(r"[┤│](.*?)[├│]", row_line).group(1)
    assert len(content) == border_interior
    repo.close()


def test_mesh_page_day_axis_ticks_sit_under_dated_columns(tmp_path: Path) -> None:
    """The day chart notches its border with ``┬`` and labels the ticks with dates."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    lines = _plain(_mesh_sections(ctx, None, 90), width=90).split("\n")
    heading_idx = next(i for i, line in enumerate(lines) if "Packets per day" in line)
    border = next(
        line for line in lines[heading_idx:] if re.fullmatch(r"\s*└[─┬]+┘", line)
    )
    caption = lines[lines.index(border) + 1]
    tick_cols = [i for i, ch in enumerate(border) if ch == "┬"]
    assert tick_cols, "the day axis should carry tick marks"
    # The seeded history ends today, so the newest bar's tick reads 'today'.
    assert "today" in caption
    # Every tick has a non-blank label somewhere near it (labels centre on their tick).
    assert any(not caption[max(0, c - 3):c + 4].isspace() for c in tick_cols)
    repo.close()


def test_mesh_page_rhythm_left_edge_aligns_with_the_day_charts(tmp_path: Path) -> None:
    """The rhythm shares the day charts' y-axis gutter, so all three left edges line up.

    The rhythm keeps its own narrower width (only the gutter is shared), so this checks
    the border's opening ``└`` column, not the whole width.
    """
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    lines = _plain(_mesh_sections(ctx, None, 90), width=90).split("\n")

    def border_col(heading: str) -> int:
        start = next(i for i, line in enumerate(lines) if heading in line)
        border = next(line for line in lines[start:] if "└" in line)
        return border.index("└")

    assert border_col("Packets per day") == border_col("Rhythm")
    assert border_col("Nodes per day") == border_col("Rhythm")
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
