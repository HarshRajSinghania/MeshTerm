"""Unit tests for the live Trace and TX-optimize screens.

These drive the two full-screen tools' pure logic — trace/sweep state machines, key
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
    """The session capabilities the screens use directly: repaints and the dialog stack."""

    def __init__(self) -> None:
        self.repaints = 0
        self.stack: list = []

    def invalidate(self) -> None:
        self.repaints += 1

    def push(self, screen) -> None:  # noqa: ANN001
        self.stack.append(screen)

    def pop(self, screen=None) -> None:  # noqa: ANN001
        if screen is None:
            self.stack.pop()
        elif screen in self.stack:
            self.stack.remove(screen)


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


def _trace_screen(
    trace=None, compose_path=None, explore=None, previous=None
) -> tuple[TraceScreen, _FakeSession]:
    session = _FakeSession()

    async def default_trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0, 2.0))

    async def default_flow(current):  # noqa: ANN001
        return current

    screen = TraceScreen(
        "Alice",
        device_label="Us",
        device_hash="aabb" + "00" * 30,
        resolve=lambda label: label,
        session=session,
        trace=trace or default_trace,
        compose_path=compose_path or default_flow,
        explore=explore or default_flow,
        previous=previous,
    )
    return screen, session


async def test_trace_screen_one_trace_per_enter_accumulates() -> None:
    """Each Enter transmits exactly one trace; the session aggregates what landed.

    Single-transmission is the screen's blacklist-avoidance rule: repeat sampling is
    the human's call, so three keypresses mean three traces and a three-sample median.
    """
    screen, _ = _trace_screen()
    for _ in range(3):
        screen.start_trace()
        await screen._worker
    body = _plain(screen.render_body(100))
    assert "success rate" in body and "3/3" in body
    assert "#3" in body  # newest-first numbering
    assert "Per-hop medians" in body
    assert "burst" not in body.lower()  # no burst configuration is offered anywhere


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


async def test_trace_screen_only_one_trace_at_a_time() -> None:
    """Enter during an in-flight trace is a no-op; the running flag gates re-entry."""
    started = 0
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        nonlocal started
        started += 1
        await release.wait()

    screen, _ = _trace_screen(trace=trace)
    screen.start_trace()
    assert screen._running
    screen.handle("enter")  # ignored while running
    release.set()
    await screen._worker
    assert started == 1
    assert not screen._running


async def test_trace_screen_failure_reads_inline() -> None:
    """A failed trace reports its error in the log area instead of crashing the screen."""

    async def trace(path_spec, on_trace):  # noqa: ANN001
        raise RuntimeError("no route")

    screen, _ = _trace_screen(trace=trace)
    screen.start_trace()
    await screen._worker
    assert "trace failed: no route" in _plain(screen.render_body(100))


async def test_trace_screen_composer_updates_the_spec() -> None:
    """`p` opens the injected composer flow; its result becomes the next trace's path."""
    asked: list[str] = []

    async def compose(current: str):  # noqa: ANN001
        asked.append(current)
        return "3d,f2,3d"  # one forced hop: outbound, target, then the mirrored return

    screen, _ = _trace_screen(compose_path=compose)
    screen.handle("text", "p")
    await asyncio.sleep(0)
    assert asked == [""]
    assert screen._path_spec == "3d,f2,3d"
    body = _plain(screen.render_body(100))
    assert "3d,f2,3d" in body
    # the planned route previews outbound *and* the resolved, dimmed return leg
    assert body.count("Us") >= 2
    assert body.count("3d") >= 2


async def test_trace_screen_explore_adopts_a_scenario_path() -> None:
    """`x` runs the injected explore flow; adopting a path sets the spec, None keeps it."""

    async def adopt(current: str):  # noqa: ANN001
        return "3d63,f2c2"

    screen, _ = _trace_screen(explore=adopt)
    screen.handle("text", "x")
    await asyncio.sleep(0)
    assert screen._path_spec == "3d63,f2c2"

    async def keep(current: str):  # noqa: ANN001
        return None

    screen._explore = keep
    screen.handle("text", "x")
    await asyncio.sleep(0)
    assert screen._path_spec == "3d63,f2c2"  # None leaves the spec untouched


async def test_trace_screen_opens_idle_until_enter() -> None:
    """Selecting a target must never transmit by itself: nothing flies until Enter."""
    screen, session = _trace_screen()
    assert not screen._running and screen._worker is None
    assert "press Enter to trace" in _plain(screen.render_body(100))
    screen.handle("enter")
    assert screen._running
    await screen._worker
    assert session.stack == []  # the tracing dialog was popped with the trace


async def test_trace_screen_floats_the_tracing_dialog() -> None:
    """A trace pushes the abortable dialog for its duration and pops it however it ends."""
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0))
        await release.wait()

    screen, session = _trace_screen(trace=trace)
    screen.start_trace()
    await asyncio.sleep(0)
    assert len(session.stack) == 1
    dialog = session.stack[0]
    assert dialog.last is not None  # the landed reply echoes on the dialog
    body = _plain(dialog.render_body(60))
    assert "Abort" in body
    release.set()
    await screen._worker
    assert session.stack == []


async def test_tracing_dialog_abort_cancels_the_trace() -> None:
    """Enter/Esc on the tracing dialog cancels the in-flight trace via the screen."""
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        await release.wait()

    screen, session = _trace_screen(trace=trace)
    screen.start_trace()
    await asyncio.sleep(0)
    session.stack[0].handle("escape")
    with pytest.raises(asyncio.CancelledError):
        await screen._worker
    assert session.stack == []
    assert not screen._running


async def test_trace_screen_escape_cancels_the_inflight_trace() -> None:
    """Esc resolves the screen and cancels a trace that is still measuring."""
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        await release.wait()

    screen, _ = _trace_screen(trace=trace)
    screen.start_trace()
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
