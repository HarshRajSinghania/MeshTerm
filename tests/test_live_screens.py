"""Unit tests for the live Trace and TX-optimize screens.

These drive the two full-screen tools' pure logic — burst/sweep state machines, key
handling, and rendering — against fake sessions and injected runners, so they run fast
and headless (the same approach as ``test_tui``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from meshterm.core.models import Hop, TraceResult, TraceStats, TxLevelResult, TxOptResult, utcnow
from meshterm.ui.trace_screen import TraceScreen, snr_bar
from meshterm.ui.tx_screen import TxSweepScreen


class _FakeSession:
    """The one session capability the screens use directly: requesting a repaint."""

    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _trace(*snrs: float, success: bool = True, target: str = "Alice") -> TraceResult:
    """Build a trace with one hop per SNR reading."""
    hops = [Hop(index=i, node=f"{i:02x}{i:02x}", snr=snr) for i, snr in enumerate(snrs)]
    return TraceResult(
        target=target, success=success, hops=hops, round_trip_ms=200.0, path_hash_bytes=2
    )


def _plain(lines: list[str]) -> str:
    """Join rendered ANSI lines and strip nothing — assertions use substring checks."""
    return "\n".join(lines)


# --- snr_bar -------------------------------------------------------------------


def test_snr_bar_scales_with_signal_quality() -> None:
    """A stronger signal fills more of the track; None renders an empty muted track."""
    weak = snr_bar(-12.0).plain
    strong = snr_bar(8.0).plain
    assert weak.count("▆") < strong.count("▆")
    assert len(weak) == len(strong)  # the track width is constant
    assert "▆" not in snr_bar(None).plain


def test_snr_bar_clamps_out_of_range_readings() -> None:
    """Readings beyond the display range clamp to the ends instead of over/underflowing."""
    assert snr_bar(99.0).plain.count("▆") == snr_bar(10.0).plain.count("▆")
    assert snr_bar(-99.0).plain.count("▆") == 1  # a heard hop always shows something


# --- TraceScreen ----------------------------------------------------------------


def _trace_screen(burst=None, edit_path=None, previous=None) -> tuple[TraceScreen, _FakeSession]:
    session = _FakeSession()

    async def default_burst(samples, path_spec, on_trace):  # noqa: ANN001
        for _ in range(samples):
            on_trace(_trace(5.0, 2.0))

    async def default_edit(current):  # noqa: ANN001
        return current

    screen = TraceScreen(
        "Alice",
        device_label="Us",
        device_hash="aabb" + "00" * 30,
        resolve=lambda label: label,
        session=session,
        burst=burst or default_burst,
        edit_path=edit_path or default_edit,
        previous=previous,
    )
    return screen, session


async def test_trace_screen_burst_streams_results_into_the_log() -> None:
    """A burst appends each landed trace; the log and aggregates render them."""
    screen, _ = _trace_screen()
    screen.start_burst()
    await asyncio.sleep(0)  # let the worker run
    await screen._worker
    body = _plain(screen.render_body(100))
    assert "success rate" in body and "3/3" in body
    assert "#3" in body  # newest-first numbering
    assert "Per-hop medians" in body


async def test_trace_screen_seeds_route_from_previous_trace() -> None:
    """Before any fresh reply, the stored route shows, marked as previous."""
    old = _trace(4.0)
    old.timestamp = utcnow() - timedelta(hours=3)
    screen, _ = _trace_screen(previous=old)
    body = _plain(screen.render_body(100))
    assert "(previous" in body
    # A fresh success replaces the seeded route and drops the marker.
    screen._on_trace(_trace(6.0))
    body = _plain(screen.render_body(100))
    assert "(previous" not in body


async def test_trace_screen_only_one_burst_at_a_time() -> None:
    """Enter during a burst is a no-op; the running flag gates re-entry."""
    started = 0
    release = asyncio.Event()

    async def burst(samples, path_spec, on_trace):  # noqa: ANN001
        nonlocal started
        started += 1
        await release.wait()

    screen, _ = _trace_screen(burst=burst)
    screen.start_burst()
    assert screen._running
    screen.handle("enter")  # ignored while running
    release.set()
    await screen._worker
    assert started == 1
    assert not screen._running


async def test_trace_screen_burst_failure_reads_inline() -> None:
    """A failed burst reports its error in the log area instead of crashing the screen."""

    async def burst(samples, path_spec, on_trace):  # noqa: ANN001
        raise RuntimeError("no route")

    screen, _ = _trace_screen(burst=burst)
    screen.start_burst()
    await screen._worker
    assert "trace failed: no route" in _plain(screen.render_body(100))


async def test_trace_screen_samples_cycle_only_when_idle() -> None:
    """`s` cycles the burst size through the odd sample counts, but never mid-burst."""
    screen, _ = _trace_screen()
    assert screen._samples == 3
    screen.handle("text", "s")
    assert screen._samples == 5
    screen._running = True
    screen.handle("text", "s")
    assert screen._samples == 5  # locked while a burst is in flight


async def test_trace_screen_path_dialog_updates_the_spec() -> None:
    """`p` opens the injected path prompt; its result becomes the next burst's path."""
    asked: list[str] = []

    async def edit(current: str):  # noqa: ANN001
        asked.append(current)
        return "3d,f2"

    screen, _ = _trace_screen(edit_path=edit)
    screen.handle("text", "p")
    await asyncio.sleep(0)
    assert asked == [""]
    assert screen._path_spec == "3d,f2"
    assert "3d,f2" in _plain(screen.render_body(100))


async def test_trace_screen_escape_cancels_the_inflight_burst() -> None:
    """Esc resolves the screen and cancels a burst that is still measuring."""
    release = asyncio.Event()

    async def burst(samples, path_spec, on_trace):  # noqa: ANN001
        await release.wait()

    screen, _ = _trace_screen(burst=burst)
    screen.start_burst()
    screen.future = asyncio.get_running_loop().create_future()
    screen.handle("escape")
    assert screen.future.result() is None
    with pytest.raises(asyncio.CancelledError):
        await screen._worker


# --- TxSweepScreen -----------------------------------------------------------------


def _level(tx: int, snr: float | None, successes: int = 2, samples: int = 2) -> TxLevelResult:
    return TxLevelResult(
        tx_power=tx,
        samples=samples,
        successes=successes,
        target_snr=snr,
        score=snr if snr is not None else float("-inf"),
        stats=TraceStats.from_traces("Alice", []),
    )


def _sweep_screen(offer_apply=None) -> tuple[TxSweepScreen, _FakeSession]:
    session = _FakeSession()
    screen = TxSweepScreen(
        admin_label="Repeater",
        target_label="Alice",
        path="a1b2,d4e5",
        tx_min=12,
        tx_max=28,
        step=3,
        samples=3,
        session=session,
        offer_apply=offer_apply or (lambda: None),
    )
    return screen, session


def _result(best_tx: int = 19, best_snr: float | None = 8.8) -> TxOptResult:
    return TxOptResult(
        target="Alice",
        admin_node="Repeater",
        path="a1b2,d4e5",
        original_tx=20,
        best_tx=best_tx,
        best_snr=best_snr,
        best_success_rate=1.0,
        applied=False,
        levels=[_level(best_tx, best_snr)],
    )


def test_sweep_screen_stars_the_running_best() -> None:
    """Each landed level renders ascending by TX with the current best starred."""
    screen, _ = _sweep_screen()
    screen.on_phase("coarse")
    screen.on_level(1, 7, _level(12, -2.0))
    screen.on_level(2, 7, _level(18, 7.5))
    body = _plain(screen.render_body(100))
    assert "coarse sweep" in body and "level 2/7" in body
    starred = next(line for line in body.splitlines() if "★" in line)
    assert "18" in starred


def test_sweep_screen_shows_failed_levels_distinctly() -> None:
    """A level nothing got through at reads as a no-reply row, not an empty bar."""
    screen, _ = _sweep_screen()
    screen.on_level(1, 7, _level(28, None, successes=0))
    assert "✗ no reply" in _plain(screen.render_body(100))


def test_sweep_screen_completion_offers_the_winner() -> None:
    """After completion the outcome shows and `a` re-offers the apply dialog."""
    offered: list[bool] = []
    screen, _ = _sweep_screen(offer_apply=lambda: offered.append(True))
    screen.handle("text", "a")  # mid-sweep: nothing to offer yet
    assert offered == []
    screen.complete(_result())
    body = _plain(screen.render_body(100))
    assert "sweep complete" in body and "TX 19" in body and "press a to apply" in body
    assert "a apply winner" in screen.footer_hint
    screen.handle("text", "a")
    assert offered == [True]


def test_sweep_screen_apply_updates_status_and_retires_the_key() -> None:
    """Marking the winner applied flips the status line and stops offering `a`."""
    offered: list[bool] = []
    screen, _ = _sweep_screen(offer_apply=lambda: offered.append(True))
    screen.complete(_result())
    screen.mark_applied()
    assert "✓ TX 19 set on Repeater" in _plain(screen.render_body(100))
    assert "a apply winner" not in screen.footer_hint
    screen.handle("text", "a")
    assert offered == []


def test_sweep_screen_no_result_never_offers_apply() -> None:
    """A sweep where nothing got through warns and keeps the apply key retired."""
    screen, _ = _sweep_screen()
    screen.complete(_result(best_snr=None))
    body = _plain(screen.render_body(100))
    assert "no traces reached" in body
    assert "a apply winner" not in screen.footer_hint


def test_sweep_screen_failure_keeps_measured_levels_on_screen() -> None:
    """A mid-sweep error is reported while the levels already measured stay visible."""
    screen, _ = _sweep_screen()
    screen.on_level(1, 7, _level(12, -2.0))
    screen.fail("link lost")
    body = _plain(screen.render_body(100))
    assert "sweep failed: link lost" in body
    assert "-2.0" in body


def test_sweep_screen_escape_resolves() -> None:
    """Esc resolves the screen (the controller then cancels and restores)."""
    screen, _ = _sweep_screen()

    class _Fut:
        def __init__(self) -> None:
            self.value = None
            self._done = False

        def done(self) -> bool:
            return self._done

        def set_result(self, value) -> None:  # noqa: ANN001
            self.value = value
            self._done = True

    screen.future = _Fut()
    screen.handle("escape")
    assert screen.future.done()
