"""Tests for the Repository persistence layer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from meshterm.core.models import PATH_TRACE_TARGET, Hop, TraceResult
from meshterm.persistence.repository import Repository


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    """A Repository on a scratch database, closed when the test finishes."""
    r = Repository(tmp_path / "test.db")
    yield r
    r.close()


def _make_trace(
    target: str,
    *hops: tuple[str | None, float],
    success: bool = True,
    rtt: float | None = 42.0,
    tx: int | None = 11,
    ts: datetime | None = None,
) -> TraceResult:
    """Build a trace with full control over fields that matter for persistence."""
    return TraceResult(
        target=target,
        success=success,
        hops=[Hop(i, node, snr) for i, (node, snr) in enumerate(hops)],
        round_trip_ms=rtt,
        tx_power=tx,
        timestamp=ts or datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )


# -- record + hydrate round-trip -------------------------------------------


def test_record_and_hydrate_preserves_all_fields(repo: Repository) -> None:
    """Every TraceResult field survives a write→read cycle."""
    ts = datetime(2026, 6, 1, 8, 30, 0, tzinfo=timezone.utc)
    trace = _make_trace(
        "Alice", ("relay", 3.5), ("Alice", -2.0), (None, 1.0), rtt=88.5, tx=7, ts=ts
    )
    run = repo.start_run("trace", {})
    repo.record_trace(run, trace)

    got = repo.recent_traces("Alice")[0]
    assert got.target == "Alice"
    assert got.success is True
    assert got.round_trip_ms == 88.5
    assert got.tx_power == 7
    assert got.timestamp == ts
    assert [(h.index, h.node, h.snr) for h in got.hops] == [
        (0, "relay", 3.5),
        (1, "Alice", -2.0),
        (2, None, 1.0),
    ]


def test_failed_trace_round_trips(repo: Repository) -> None:
    """A failed trace (no hops, no RTT) persists and reads back correctly."""
    trace = _make_trace("Bob", success=False, rtt=None, tx=None)
    run = repo.start_run("trace", {})
    repo.record_trace(run, trace)

    got = repo.recent_traces("Bob")[0]
    assert got.success is False
    assert got.hops == []
    assert got.round_trip_ms is None
    assert got.tx_power is None
    assert got.path_hash_bytes is None  # no hashes to derive a width from


def test_hydrated_traces_recover_the_path_hash_width(repo: Repository) -> None:
    """The stored hop hashes carry the trace's width, so rehydration recovers it.

    Without this, a previous trace's route line renders every hash — including our
    own device's full 64-char public key — at absurd lengths.
    """
    run = repo.start_run("trace", {})
    trace = _make_trace("Alice", ("3d63", 3.5), ("f2c2", -2.0), (None, 1.0))
    repo.record_trace(run, trace)

    assert repo.latest_trace("Alice").path_hash_bytes == 2
    assert repo.recent_traces("Alice")[0].path_hash_bytes == 2


# -- recent_traces ---------------------------------------------------------


def test_recent_traces_returns_newest_first(repo: Repository) -> None:
    """Traces come back in reverse insertion order."""
    run = repo.start_run("trace", {})
    for i in range(5):
        repo.record_trace(run, _make_trace("X", ("hop", float(i))))

    traces = repo.recent_traces("X")
    snrs = [t.hops[0].snr for t in traces]
    assert snrs == [4.0, 3.0, 2.0, 1.0, 0.0]


def test_recent_traces_respects_limit(repo: Repository) -> None:
    """The limit parameter caps the number of returned traces."""
    run = repo.start_run("trace", {})
    for _ in range(10):
        repo.record_trace(run, _make_trace("X", ("h", 1.0)))

    assert len(repo.recent_traces("X", limit=3)) == 3


def test_recent_traces_filters_by_target(repo: Repository) -> None:
    """Only traces to the requested target are returned."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("Alice", ("r", 1.0)))
    repo.record_trace(run, _make_trace("Bob", ("r", 2.0)))
    repo.record_trace(run, _make_trace("Alice", ("r", 3.0)))

    got = repo.recent_traces("Alice")
    assert len(got) == 2
    assert all(t.target == "Alice" for t in got)


def test_recent_traces_includes_failures(repo: Repository) -> None:
    """Both successful and failed traces are returned (failures are informative)."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("X", ("h", 1.0), success=True))
    repo.record_trace(run, _make_trace("X", success=False))

    got = repo.recent_traces("X")
    assert len(got) == 2
    statuses = {t.success for t in got}
    assert statuses == {True, False}


def test_recent_traces_empty_target(repo: Repository) -> None:
    """Querying a target with no stored traces returns an empty list."""
    assert repo.recent_traces("NoSuchNode") == []


# -- traced_targets --------------------------------------------------------


def test_traced_targets_recency_ordered_and_capped(repo: Repository) -> None:
    """Targets come back most recently traced first, deduplicated, and capped.

    The all-time trace count must not matter: a heavily-traced old target (Alice here)
    ranks by its *latest* trace, so stale favourites and leftover mock names age out of
    the picker's short "Recently traced" list instead of squatting on top forever.
    """
    run = repo.start_run("trace", {})
    for _ in range(8):
        repo.record_trace(run, _make_trace("Alice", ("r", 1.0)))
    for name in ["Bob", "Carol", "Dave", "Erin", "Frank"]:
        repo.record_trace(run, _make_trace(name, ("r", 1.0)))
    repo.record_trace(run, _make_trace("Bob", ("r", 1.0)))  # Bob traced again just now

    assert repo.traced_targets() == ["Bob", "Frank", "Erin", "Dave", "Carol"]
    assert repo.traced_targets(limit=2) == ["Bob", "Frank"]


def test_traced_targets_empty_db(repo: Repository) -> None:
    """No traces → empty list."""
    assert repo.traced_targets() == []


def test_traced_targets_excludes_path_walks(repo: Repository) -> None:
    """Path walks record under the ``(path)`` sentinel and never become pickable targets."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("Alice", ("r", 1.0)))
    repo.record_trace(run, _make_trace(PATH_TRACE_TARGET, ("r", 2.0)))

    assert repo.traced_targets() == ["Alice"]
    # …but the walks still seed the path screen's previous-route line.
    latest = repo.latest_trace(PATH_TRACE_TARGET)
    assert latest is not None and latest.target == PATH_TRACE_TARGET


def test_target_trace_counts_tallies_successes_and_totals(repo: Repository) -> None:
    """Per target, (successes, total) over every trace — failures included, path walks not."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("Alice", ("r", 1.0), success=True))
    repo.record_trace(run, _make_trace("Alice", success=False))
    repo.record_trace(run, _make_trace("Alice", ("r", 2.0), success=True))
    repo.record_trace(run, _make_trace("Bob", ("r", 1.0), success=True))
    repo.record_trace(run, _make_trace(PATH_TRACE_TARGET, ("r", 3.0), success=True))

    counts = repo.target_trace_counts()
    assert counts["Alice"] == (2, 3)  # two of three came home
    assert counts["Bob"] == (1, 1)
    assert PATH_TRACE_TARGET not in counts  # path walks name no target


def test_target_last_traced_keeps_latest_per_target(repo: Repository) -> None:
    """Per target, the most recent trace time — failures counted, path walks excluded."""
    run = repo.start_run("trace", {})
    early = datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 1, 12, 18, 0, tzinfo=timezone.utc)
    repo.record_trace(run, _make_trace("Alice", ("r", 1.0), ts=early))
    repo.record_trace(run, _make_trace("Alice", success=False, ts=late))  # newer, timed out
    repo.record_trace(run, _make_trace("Bob", ("r", 1.0), ts=early))
    repo.record_trace(run, _make_trace(PATH_TRACE_TARGET, ("r", 3.0), ts=late))

    last = repo.target_last_traced()
    assert last["Alice"] == late  # the failed-but-newer trace wins — you still traced it
    assert last["Bob"] == early
    assert PATH_TRACE_TARGET not in last  # path walks name no target


# -- latest_trace edge cases -----------------------------------------------


def test_latest_trace_success_only_skips_failures(repo: Repository) -> None:
    """success_only=True skips failed traces, even if they're newer."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("X", ("h", 1.0), success=True))
    repo.record_trace(run, _make_trace("X", success=False))

    got = repo.latest_trace("X", success_only=True)
    assert got is not None
    assert got.success is True


def test_latest_trace_success_only_false_includes_failures(repo: Repository) -> None:
    """success_only=False returns the newest trace regardless of outcome."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("X", ("h", 1.0), success=True))
    repo.record_trace(run, _make_trace("X", success=False))

    got = repo.latest_trace("X", success_only=False)
    assert got is not None
    assert got.success is False


# -- runs ------------------------------------------------------------------


def test_run_lifecycle(repo: Repository) -> None:
    """A run transitions from running → ok with a summary."""
    run_id = repo.start_run("route-map", {"target": "Alice"}, profile="field")
    runs = repo.list_runs()
    assert runs[0].status == "running"
    assert runs[0].tool == "route-map"
    assert runs[0].profile == "field"
    assert runs[0].finished_at is None

    repo.finish_run(run_id, "ok", {"stability": 0.85})
    runs = repo.list_runs()
    assert runs[0].status == "ok"
    assert runs[0].finished_at is not None
    assert runs[0].summary == {"stability": 0.85}


def test_finish_run_error_without_summary(repo: Repository) -> None:
    """An errored run stores status without a summary."""
    run_id = repo.start_run("trace", {})
    repo.finish_run(run_id, "error")

    runs = repo.list_runs()
    assert runs[0].status == "error"
    assert runs[0].summary is None


def test_list_runs_respects_limit(repo: Repository) -> None:
    """list_runs caps results at the requested limit."""
    for i in range(10):
        repo.start_run("trace", {"i": i})

    assert len(repo.list_runs(limit=3)) == 3


def test_list_runs_newest_first(repo: Repository) -> None:
    """Runs come back in reverse insertion order."""
    repo.start_run("first", {})
    repo.start_run("second", {})
    repo.start_run("third", {})

    tools = [r.tool for r in repo.list_runs()]
    assert tools == ["third", "second", "first"]
