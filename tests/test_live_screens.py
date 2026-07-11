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
from meshterm.ui.theme import snr_style
from meshterm.ui.trace_screen import _BAR_FULL, _BAR_HALF, _BAR_WIDTH, TraceScreen, snr_bar
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


def _lit_plain(bar) -> str:  # noqa: ANN001
    """The reading's own coloured prefix, stripped of the dimmed unlit track.

    The unlit remainder reuses :data:`_BAR_FULL` too (dimmed to the ``track`` style
    instead of a distinct glyph), so telling lit from unlit means reading which
    style each character actually landed in, not just which glyph it is.
    """
    if bar.style == "track":
        return ""
    track_spans = [s for s in bar.spans if s.style == "track"]
    end = min((s.start for s in track_spans), default=len(bar.plain))
    return bar.plain[:end]


def _filled_steps(bar) -> int:  # noqa: ANN001
    """Count a rendered bar's fill steps — two per full braille cell, one per half."""
    plain = _lit_plain(bar)
    return plain.count(_BAR_FULL) * 2 + plain.count(_BAR_HALF)


def test_snr_bar_scales_with_signal_quality() -> None:
    """A stronger signal fills more of the track; None renders an entirely unlit one."""
    weak = snr_bar(-12.0)
    strong = snr_bar(8.0)
    assert _filled_steps(weak) < _filled_steps(strong)
    assert len(weak.plain) == len(strong.plain) == _BAR_WIDTH  # track width is constant
    none_bar = snr_bar(None)
    assert _filled_steps(none_bar) == 0
    assert none_bar.style == "track"  # the whole track dims, not a separate faint dot run


def test_snr_bar_clamps_out_of_range_readings() -> None:
    """Readings beyond the display range clamp to the ends instead of over/underflowing."""
    assert _filled_steps(snr_bar(99.0)) == _filled_steps(snr_bar(10.0))
    assert _filled_steps(snr_bar(-99.0)) == 1  # a heard hop always shows something


def test_snr_bar_packs_two_steps_per_character() -> None:
    """16 steps of resolution pack into 8 characters: full cells, then one trailing half."""
    one_step = snr_bar(-13.4375)  # frac = 1/16 of the -15..+10 span
    assert one_step.plain[0] == _BAR_HALF
    assert one_step.plain[1:] == _BAR_FULL * (_BAR_WIDTH - 1)  # unlit track, same glyph
    track_spans = [(s.start, s.end) for s in one_step.spans if s.style == "track"]
    assert track_spans == [(1, _BAR_WIDTH)]

    three_steps = snr_bar(-10.3125)  # frac = 3/16 → one full cell, one half
    assert three_steps.plain[:2] == _BAR_FULL + _BAR_HALF
    assert three_steps.plain[2:] == _BAR_FULL * (_BAR_WIDTH - 2)


def test_snr_bar_unlit_track_dims_to_a_distinct_style() -> None:
    """The unlit track renders in ``track``, not the reading's colour or old ``faint`` dots."""
    bar = snr_bar(-10.3125)
    assert "·" not in bar.plain  # no more plain-dot placeholder
    track_span = next(s for s in bar.spans if s.style == "track")
    assert bar.plain[track_span.start : track_span.end] == _BAR_FULL * (_BAR_WIDTH - 2)
    assert bar.style == snr_style(-10.3125)  # the lit prefix still carries the reading's colour

    assert snr_bar(10.0).plain == _BAR_FULL * _BAR_WIDTH  # top of range: every cell full


# --- _previous_outbound -----------------------------------------------------------


def _walk(*nodes: str, success: bool = True) -> TraceResult:
    """A stored walk whose hop hashes are ``nodes`` (plus the final hash-less us)."""
    hops = [Hop(index=i, node=n, snr=1.0) for i, n in enumerate(nodes)]
    hops.append(Hop(index=len(nodes), node=None, snr=1.0))
    return TraceResult(
        target="Alice", success=success, hops=hops, round_trip_ms=200.0, path_hash_bytes=2
    )


def test_previous_outbound_extracts_the_proven_route() -> None:
    """The last successful boomerang's first half is the reusable outbound leg.

    Verified on hardware that the device itself almost never has a learned route
    (contacts report flood), so this stored evidence is what auto mode actually
    walks for a multi-hop target.
    """
    from meshterm.ui.trace_screen import _previous_outbound

    walk = _walk("3d63", "f2c2", "aabb", "f2c2", "3d63")
    assert _previous_outbound(walk, "aabb" + "00" * 30) == ("3d63", "f2c2")


def test_previous_outbound_direct_walk_yields_no_repeaters() -> None:
    """A direct answer (target only) extracts an empty outbound leg — dest-only again."""
    from meshterm.ui.trace_screen import _previous_outbound

    assert _previous_outbound(_walk("aabb"), "aabb" + "00" * 30) == ()


def test_previous_outbound_rejects_unusable_history() -> None:
    """Failures, asymmetric walks, and walks that turned elsewhere are never reused."""
    from meshterm.ui.trace_screen import _previous_outbound

    target = "aabb" + "00" * 30
    assert _previous_outbound(None, target) is None
    assert _previous_outbound(_walk("3d63", "aabb", "3d63", success=False), target) is None
    # Asymmetric: came home a different way — not a boomerang to this target.
    assert _previous_outbound(_walk("3d63", "aabb", "f2c2"), target) is None
    # Palindromic, but it turned at some other node, not our target.
    assert _previous_outbound(_walk("3d63", "9999", "3d63"), target) is None


# --- TraceScreen ----------------------------------------------------------------


def _trace_screen(
    trace=None, compose_path=None, explore=None, pick_width=None, pick_samples=None,
    previous=None, mode="target", samples=1, auto_spec=None, auto_source="",
) -> tuple[TraceScreen, _FakeSession]:
    session = _FakeSession()

    async def default_trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0, 2.0))

    async def default_flow(current):  # noqa: ANN001
        return current

    screen = TraceScreen(
        "Alice" if mode == "target" else "(path)",
        mode=mode,
        device_label="Us",
        device_hash="aabb" + "00" * 30,
        resolve=lambda label: label,
        session=session,
        trace=trace or default_trace,
        compose_path=compose_path or default_flow,
        explore=(explore or default_flow) if mode == "target" else None,
        pick_width=pick_width or default_flow,
        pick_samples=pick_samples or default_flow,
        width_bytes=lambda: 2,
        sample_count=lambda: samples,
        pace_s=0.0,  # tests never sleep; pacing is asserted through the statuses
        previous=previous,
        auto_spec=auto_spec or (lambda: ""),
        auto_source=auto_source,
    )
    return screen, session


async def test_trace_screen_one_trace_per_enter_accumulates() -> None:
    """At the default sample count, each Enter transmits exactly one trace.

    Repeat sampling stays a human decision unless a bigger sample count is chosen
    explicitly, so three keypresses mean three traces and a three-sample median.
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


async def test_trace_screen_runs_the_chosen_sample_count() -> None:
    """One Enter runs the whole chosen sample count, every trace recorded."""
    ran: list[str] = []

    async def trace(path_spec, on_trace):  # noqa: ANN001
        ran.append(path_spec)
        on_trace(_trace(5.0))

    screen, session = _trace_screen(trace=trace, samples=3)
    screen.start_trace()
    await screen._worker
    assert len(ran) == 3
    assert len(screen._traces) == 3
    assert session.stack == []  # the dialog was popped with the run


async def test_trace_screen_multi_trace_reports_progress_and_abort_keeps_landed() -> None:
    """A multi-trace run counts itself off; aborting keeps what already landed."""
    release = asyncio.Event()
    ran = 0

    async def trace(path_spec, on_trace):  # noqa: ANN001
        nonlocal ran
        ran += 1
        on_trace(_trace(5.0))
        if ran == 2:
            await release.wait()  # hold the run mid-flight on the second trace

    screen, session = _trace_screen(trace=trace, samples=5)
    screen.start_trace()
    await asyncio.sleep(0)
    dialog = session.stack[0]
    assert "2/5" in dialog.status  # the dialog counts the run off
    assert "2/5" in screen.footer_hint
    assert "2/5" in _plain(screen.render_body(100))  # the log spinner row too
    screen.cancel()
    with pytest.raises(asyncio.CancelledError):
        await screen._worker
    assert len(screen._traces) == 2  # already-recorded traces are kept
    assert session.stack == []


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


async def test_previous_stamp_sits_on_its_own_line() -> None:
    """The (previous · …) marker renders under the route, never squeezed beside it."""
    old = _trace(4.0)
    old.timestamp = utcnow() - timedelta(hours=3)
    screen, _ = _trace_screen(previous=old)
    stamp_line = next(l for l in screen.render_body(100) if "(previous" in l)
    assert "→" not in stamp_line  # the route stays on the line above


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
    """Committing Compose path runs the flow; its result becomes the next trace's path."""
    asked: list[str] = []

    async def compose(current: str):  # noqa: ANN001
        asked.append(current)
        return "3d,f2,3d"  # one forced hop: outbound, target, then the mirrored return

    screen, _ = _trace_screen(compose_path=compose)
    screen.handle("down")  # Trace → Back…
    screen.handle("down")  # …wrapping onto Compose path, the first row
    screen.handle("enter")
    await asyncio.sleep(0)
    assert asked == [""]
    assert screen._path_spec == "3d,f2,3d"
    body = _plain(screen.render_body(100))
    assert "3d,f2,3d" in body
    # the planned route previews outbound *and* the resolved, dimmed return leg
    assert body.count("Us") >= 2
    assert body.count("3d") >= 2


async def test_trace_screen_explore_adopts_a_scenario_path() -> None:
    """Committing Explore paths runs the flow; adopting sets the spec, None keeps it."""

    async def adopt(current: str):  # noqa: ANN001
        return "3d63,f2c2"

    screen, _ = _trace_screen(explore=adopt)
    for _ in range(3):
        screen.handle("up")  # Trace → Sample count → Path width → Explore paths
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._path_spec == "3d63,f2c2"

    async def keep(current: str):  # noqa: ANN001
        return None

    screen._explore = keep
    screen.handle("enter")  # the cursor is still on Explore paths
    await asyncio.sleep(0)
    assert screen._path_spec == "3d63,f2c2"  # None leaves the spec untouched


async def test_trace_screen_action_cursor_commits_the_selected_row() -> None:
    """↑↓ move over the action rows; Enter commits the one under the cursor."""
    opened: list[str] = []

    async def width_flow(current):  # noqa: ANN001
        opened.append(f"width:{current}")
        return "3d63,f2c2,3d63"

    screen, _ = _trace_screen(pick_width=width_flow)
    body = _plain(screen.render_body(100))
    # The menu order the actions read in: build first, tune, then transmit, then out.
    labels = ["Compose path", "Explore paths", "Path width — 2 bytes per hop",
              "Sample count — 1 trace", "Trace — one transmission", "Back"]
    positions = [body.index(label) for label in labels]
    assert positions == sorted(positions)
    screen.handle("up")  # Trace → Sample count
    screen.handle("up")  # → Path width
    screen.handle("enter")
    await asyncio.sleep(0)
    assert opened == ["width:"]
    assert screen._path_spec == "3d63,f2c2,3d63"


async def test_trace_screen_sample_count_row_opens_its_dialog() -> None:
    """Committing Sample count floats the flow; its None resolution keeps the spec."""
    opened: list[str] = []

    async def samples_flow(current):  # noqa: ANN001
        opened.append(current)
        return None

    screen, _ = _trace_screen(pick_samples=samples_flow)
    screen._path_spec = "3d,f2,3d"
    screen.handle("up")  # Trace → Sample count
    screen.handle("enter")
    await asyncio.sleep(0)
    assert opened == ["3d,f2,3d"]
    assert screen._path_spec == "3d,f2,3d"  # the count is not a spec: nothing changes


async def test_trace_screen_hotkeys_are_retired() -> None:
    """The old w/p/x shortcuts are gone: typing must not float any flow."""
    opened: list[str] = []

    async def flow(current):  # noqa: ANN001
        opened.append(current)
        return None

    screen, _ = _trace_screen(compose_path=flow, explore=flow, pick_width=flow,
                              pick_samples=flow)
    for key in ("p", "x", "w", "s"):
        screen.handle("text", key)
    await asyncio.sleep(0)
    assert opened == []
    assert not screen._running


async def test_trace_screen_back_row_resolves_like_escape() -> None:
    """The Back row leaves the screen exactly as Esc does."""
    screen, _ = _trace_screen()
    screen.future = asyncio.get_running_loop().create_future()
    screen.handle("down")  # Trace → Back
    screen.handle("enter")
    assert screen.future.result() is None


def test_planned_route_dims_only_the_mirrored_return_leg() -> None:
    """A palindromic target-mode spec dims its second half; hand walks never dim."""
    screen, _ = _trace_screen()

    def faint_cells(text) -> int:  # noqa: ANN001
        return sum(
            span.end - span.start for span in text.spans if "faint" in str(span.style)
        )

    screen._path_spec = "3d,f2,3d"  # symmetric boomerang: the mirror is dimmed
    symmetric = screen._planned_route()
    assert symmetric.plain == "Us → 3d → f2 → 3d → Us"
    screen._path_spec = "3d,f2,27"  # a stale hand walk: every hop is the user's
    custom = screen._planned_route()
    assert custom.plain == "Us → 3d → f2 → 27 → Us"
    assert faint_cells(symmetric) > faint_cells(custom)

    # In path mode even a there-and-back-the-same-way walk is fully hand-composed,
    # so a palindrome must NOT read as "not yours to compose".
    walk, _ = _trace_screen(mode="path")
    walk._path_spec = "3d,f2,3d"
    assert faint_cells(walk._planned_route()) == faint_cells(custom)


def test_summary_appends_the_displayed_hop_count() -> None:
    """The path row ends with how many nodes the displayed route passes through."""
    screen, _ = _trace_screen()
    screen._path_spec = "3d,f2,3d"
    assert "· 3 hops" in _plain(screen.render_body(100))
    screen._on_trace(_trace(5.0, 2.0))  # a live 2-hop route now outranks the plan
    assert "· 2 hops" in _plain(screen.render_body(100))


def test_auto_resolved_route_renders_with_its_provenance() -> None:
    """With no composed path, the auto route shows as the plan, labelled with its source.

    What the screen draws is exactly what Trace will put on the air (both read the
    same resolver), so the user can see the forced boomerang — and where it came
    from — before committing a transmission.
    """
    screen, _ = _trace_screen(
        auto_spec=lambda: "3d63,f2c2,aabb,f2c2,3d63", auto_source="last trace · Jul 09 14:32"
    )
    plan = screen._planned_route().plain
    assert "Us → 3d63 → f2c2 → aabb → f2c2 → 3d63 → Us" in plan
    assert "(auto · last trace · Jul 09 14:32)" in plan
    body = _plain(screen.render_body(100))
    assert "auto · last trace · Jul 09 14:32" in body  # the summary's path row
    assert "· 5 hops" in body


def test_composed_path_outranks_the_auto_route() -> None:
    """A hand-composed spec replaces the auto plan everywhere — display and wire."""
    screen, _ = _trace_screen(auto_spec=lambda: "aabb", auto_source="device route")
    screen._path_spec = "3d63,aabb,3d63"
    plan = screen._planned_route().plain
    assert "Us → 3d63 → aabb → 3d63 → Us" in plan
    assert "(auto ·" not in plan
    assert screen._effective_spec() == ("3d63,aabb,3d63", False)


def test_unaddressable_target_reads_as_path_less_auto() -> None:
    """With nothing to force (no target hash), the summary says so instead of lying."""
    screen, _ = _trace_screen()  # auto_spec resolves ""
    assert "auto — path-less (unknown target)" in _plain(screen.render_body(100))


def test_trace_log_section_hidden_until_there_is_something_to_log() -> None:
    """Idle with no traces, the Traces heading (and its old hint) don't render."""
    screen, _ = _trace_screen()
    assert "Traces" not in _plain(screen.render_body(100))


async def test_trace_screen_path_mode_gates_trace_and_drops_explore() -> None:
    """Path mode: no Explore row, and Trace stays inert until a path exists."""
    screen, _ = _trace_screen(mode="path")
    body = _plain(screen.render_body(100))
    assert "Explore paths" not in body
    assert "Trace — compose a path first" in body
    assert "none — compose a path" in body
    screen.handle("enter")  # the cursor opens on Trace, but there is nothing to walk
    assert not screen._running and screen._worker is None
    screen._path_spec = "3d,f2"
    assert "Trace — one transmission" in _plain(screen.render_body(100))
    screen.handle("enter")
    assert screen._running
    await screen._worker
    assert len(screen._traces) == 1


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
