"""The live trace screen: watch repeated path traces stream in, hop by hop.

This is the interactive face of the ``trace`` tool (the scripted CLI keeps its one-shot
table output). Where the old flow chained three prompts into a progress bar and a static
result window, this screen follows the app's living-screen pattern (chat, nodes, map):
pick a target, and the screen opens tracing immediately — each reply lands in a running
log as it arrives, the route line and per-hop medians update live, and further bursts,
sample-count changes, and a forced path are all one keystroke away without ever leaving
the screen.

Layout, top to bottom: the walked route (seeded from the target's last stored trace until
a fresh reply arrives), the run's robust aggregates (success rate, median bottleneck SNR,
median RTT), per-hop median SNR with quality bars, and the individual traces newest-first.

Every burst is persisted exactly like a scripted run: one ``runs`` row per burst, each
trace recorded under it, so the stored history reads the same no matter which front end
produced it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..core.models import TraceResult, TraceStats
from ..services import trace_runner
from .theme import snr_style
from .tui.render import render_lines
from .tui.screen import Screen
from .tui.spinner import Spinner
from .widgets import NodeResolver, _link_text, _route_text

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between spinner frames while a burst is in flight.
_SPINNER_INTERVAL = 0.12

#: The sample counts the ``s`` key cycles through — small odd numbers, so the median is
#: always a real reading and the radio's duty cycle stays polite.
_SAMPLE_CYCLE = (1, 3, 5, 9)

#: How wide the per-hop SNR quality bars draw, in cells.
_BAR_WIDTH = 16

#: The SNR range the bars span, in dB: -15 (barely readable) to +10 (excellent). Values
#: outside clamp to the ends, so the bar always shows *something* for a heard hop.
_BAR_SNR_MIN = -15.0
_BAR_SNR_MAX = 10.0

#: A burst runner: ``(samples, path_spec, on_trace)`` → runs the traces, streaming each
#: result to ``on_trace`` as it lands. Provided by :func:`open_trace`, which closes over
#: the device, repository, and settings so the screen stays free of persistence concerns.
BurstRunner = Callable[[int, str, Callable[[TraceResult], None]], Awaitable[None]]


def snr_bar(snr: Optional[float], width: int = _BAR_WIDTH) -> Text:
    """Render an SNR reading as a horizontal quality bar in the shared SNR colours.

    Args:
        snr: The reading in dB, or ``None`` (renders as an empty, muted track).
        width: Total bar track width in cells.

    Returns:
        A :class:`Text` of filled blocks over a faint track, coloured by
        :func:`~meshterm.ui.theme.snr_style`.
    """
    if snr is None:
        return Text("·" * width, style="faint")
    span = _BAR_SNR_MAX - _BAR_SNR_MIN
    frac = min(1.0, max(0.0, (snr - _BAR_SNR_MIN) / span))
    filled = max(1, round(frac * width))
    bar = Text("▆" * filled, style=snr_style(snr))
    bar.append("·" * (width - filled), style="faint")
    return bar


class TraceScreen(Screen):
    """A full-screen live trace session for one target.

    Keys: Enter runs another burst, ``s`` cycles the burst's sample count, ``p`` edits the
    forced path in a floating prompt, the usual scroll keys move the view, and Esc backs
    out (cancelling any in-flight burst; already-recorded traces are kept).
    """

    floating = False

    def __init__(
        self,
        target: str,
        *,
        device_label: str,
        device_hash: Optional[str],
        resolve: NodeResolver,
        session: Any,
        burst: BurstRunner,
        edit_path: Callable[[str], Awaitable[Optional[str]]],
        previous: Optional[TraceResult] = None,
        samples: int = 3,
    ) -> None:
        """Create the screen (traces start when :meth:`start_burst` is first called).

        Args:
            target: The trace destination (contact name or key prefix).
            device_label: Our own node's name, labelling the route's endpoints.
            device_hash: Our own public key, so the endpoints carry a hash like every hop.
            resolve: Maps a hop's raw hash to a friendly contact name when known.
            session: The running TUI session (for repaints).
            burst: Runs one burst of traces, streaming results (see :data:`BurstRunner`).
            edit_path: Shows the floating forced-path prompt over this screen, seeded with
                the current spec; resolves to the new spec or ``None`` when cancelled.
            previous: The target's most recent stored trace, if any — its route seeds the
                route line so the screen opens knowing the path history last saw.
            samples: Traces per burst to start with.
        """
        super().__init__()
        self.title = f"trace · {target}"
        self._target = target
        self._device_label = device_label
        self._device_hash = device_hash
        self._resolve = resolve
        self._session = session
        self._burst = burst
        self._edit_path = edit_path
        self._previous = previous
        self._samples = samples if samples in _SAMPLE_CYCLE else 3
        self._path_spec = ""
        self._traces: list[TraceResult] = []
        self._running = False
        self._dialog_open = False
        self._status = ""
        self._spinner = Spinner()
        self._worker: Optional[asyncio.Task] = None

    # --- state -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, tracking whether a burst is in flight."""
        if self._running:
            return "tracing… · ↑↓ PgUp/PgDn scroll · Esc back"
        return "Enter trace · s samples · p path · ↑↓ PgUp/PgDn scroll · Esc back"

    def start_burst(self) -> None:
        """Kick off one burst of traces in the background (no-op while one is running)."""
        if self._running:
            return
        self._running = True
        self._status = ""
        self._spinner.reset()
        self._worker = asyncio.ensure_future(self._run_burst())
        self._session.invalidate()

    async def _run_burst(self) -> None:
        """Drive one burst to completion, animating the spinner and reporting failures."""
        ticker = asyncio.ensure_future(self._animate())
        try:
            await self._burst(self._samples, self._path_spec, self._on_trace)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report inline, keep the screen alive
            self._status = f"trace failed: {exc}"
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a burst
                pass
            self._running = False
            self._session.invalidate()

    async def _animate(self) -> None:
        """Advance the in-flight spinner and repaint on a steady cadence, until cancelled."""
        while True:
            await asyncio.sleep(_SPINNER_INTERVAL)
            self._spinner.tick()
            self._session.invalidate()

    def _on_trace(self, result: TraceResult) -> None:
        """Append one landed trace and repaint (called by the burst as replies arrive)."""
        self._traces.append(result)
        self._session.invalidate()

    def cancel(self) -> None:
        """Cancel any in-flight burst (already-recorded traces are kept)."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    # --- input -------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Run bursts, tweak samples/path, scroll, or dismiss."""
        if action == "enter":
            self.start_burst()
        elif action == "text" and data.lower() == "s":
            if not self._running:
                idx = _SAMPLE_CYCLE.index(self._samples)
                self._samples = _SAMPLE_CYCLE[(idx + 1) % len(_SAMPLE_CYCLE)]
        elif action == "text" and data.lower() == "p":
            self._open_path_dialog()
        elif action == "up":
            self.scroll_lines(-1)
        elif action == "down":
            self.scroll_lines(1)
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self.scroll_to_bottom()
        elif action == "escape":
            self.cancel()
            self.resolve(None)

    def _open_path_dialog(self) -> None:
        """Float the forced-path prompt over the screen (one at a time, not mid-burst)."""
        if self._dialog_open or self._running:
            return
        self._dialog_open = True

        async def edit() -> None:
            try:
                spec = await self._edit_path(self._path_spec)
                if spec is not None:
                    self._path_spec = spec.strip()
            finally:
                self._dialog_open = False
                self._session.invalidate()

        asyncio.ensure_future(edit())

    # --- rendering -----------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the route, aggregates, per-hop medians, and the trace log."""
        lines = render_lines(Group(*self._sections()), width)
        self._scroll_total = max(1, len(lines))
        return lines

    def _sections(self) -> list[RenderableType]:
        """Assemble the screen's stacked sections for the current state."""
        stats = TraceStats.from_traces(self._target, self._traces)
        current = next((t for t in reversed(self._traces) if t.success), None)
        sections: list[RenderableType] = [
            self._route_line(current),
            Text(),
            self._summary(stats),
        ]
        if stats.hop_snrs:
            hash_bytes = current.path_hash_bytes if current is not None else None
            sections += [Text(), Text("Per-hop medians", style="accent")]
            sections.append(self._hops_table(stats, hash_bytes))
        sections += [Text(), Text("Traces", style="accent")]
        sections.append(self._trace_log())
        return sections

    def _route_line(self, current: Optional[TraceResult]) -> Text:
        """The walked route: fresh when a reply has landed, else the stored previous one."""
        line = Text("route  ", style="muted")
        shown = current or self._previous
        if shown is None:
            line.append("unknown — waiting for the first reply", style="muted")
            return line
        line.append_text(
            _route_text(shown, self._device_label, self._resolve, self._device_hash)
        )
        if current is None:
            stamp = shown.timestamp.astimezone().strftime("%b %d %H:%M")
            line.append(f"  (previous · {stamp})", style="faint")
        return line

    def _summary(self, stats: TraceStats) -> Text:
        """The run's aggregates plus the current burst configuration, label-aligned."""
        snr = stats.median_min_snr
        snr_text = (
            Text(f"{snr:+.1f} dB", style=snr_style(snr)) if snr is not None else Text("—")
        )
        rtt = f"{stats.median_rtt_ms:.0f} ms" if stats.median_rtt_ms is not None else "—"
        rate = f"{stats.success_rate:.0%} ({stats.successes}/{stats.samples})"
        summary = Text.assemble(
            ("success rate    ", "muted"), (rate if stats.samples else "—", ""), ("\n", ""),
            ("median min SNR  ", "muted"), snr_text, ("\n", ""),
            ("median RTT      ", "muted"), (rtt, ""), ("\n", ""),
            ("burst           ", "muted"),
            (f"{self._samples} trace{'s' if self._samples != 1 else ''}", ""),
            ("  ·  path ", "muted"),
        )
        if self._path_spec:
            summary.append(self._path_spec, style="brand")
        else:
            summary.append("auto (device-routed)", style="muted")
        return summary

    def _hops_table(self, stats: TraceStats, hash_bytes: Optional[int]) -> Table:
        """The per-hop median SNRs with quality bars, in path order."""
        table = Table(box=None, padding=(0, 1, 0, 0), expand=False, show_header=False)
        table.add_column(justify="right", style="muted")  # hop index
        table.add_column()  # link
        table.add_column(justify="right")  # median snr
        table.add_column()  # bar
        for agg in stats.hop_snrs:
            table.add_row(
                str(agg.index),
                _link_text(
                    agg.origin, agg.destination, self._device_label, self._resolve,
                    hash_bytes, self._device_hash,
                ),
                Text(f"{agg.median_snr:+.1f} dB", style=snr_style(agg.median_snr)),
                snr_bar(agg.median_snr),
            )
        return table

    def _trace_log(self) -> RenderableType:
        """The individual traces, newest first, with the in-flight spinner on top."""
        rows: list[RenderableType] = []
        if self._running:
            spin = self._spinner.text()
            spin.append("  tracing…", style="muted")
            rows.append(spin)
        elif self._status:
            rows.append(Text(self._status, style="err"))
        elif not self._traces:
            rows.append(Text("press Enter to trace", style="muted"))
        for number, trace in zip(range(len(self._traces), 0, -1), reversed(self._traces)):
            rows.append(self._trace_row(number, trace))
        return Group(*rows)

    def _trace_row(self, number: int, trace: TraceResult) -> Text:
        """One log line: number, local time, outcome, hop count, bottleneck SNR, RTT."""
        stamp = trace.timestamp.astimezone().strftime("%H:%M:%S")
        row = Text.assemble((f"#{number:<3}", "muted"), (f"{stamp}  ", "muted"))
        if not trace.success:
            row.append("✗ no reply", style="err")
            return row
        row.append("✓ ", style="ok")
        row.append(f"{trace.hop_count} hop{'s' if trace.hop_count != 1 else ''}", style="")
        if trace.min_snr is not None:
            row.append("  min ", style="muted")
            row.append(f"{trace.min_snr:+.1f} dB", style=snr_style(trace.min_snr))
        if trace.round_trip_ms is not None:
            row.append(f"  {trace.round_trip_ms:.0f} ms", style="muted")
        return row


async def open_trace(ctx: "AppContext", target: str) -> int:
    """Open the live trace screen for ``target`` and run it until dismissed.

    Wires the screen to the radio and the database: each burst opens its own ``runs``
    row, records every trace under it, and closes it with the same summary a scripted
    ``meshterm trace`` writes — so history stays uniform across front ends. The first
    burst starts immediately; the screen's route line is seeded from the target's most
    recent stored trace so the path history last saw shows while the first reply is
    still in flight.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        target: The trace destination (contact name or key prefix).

    Returns:
        The number of traces run while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the live trace screen is only available in the menu")
    session = ctx.ui.session

    device = await ctx.device()
    contacts = await device.get_contacts()
    resolve = trace_runner.make_node_resolver(contacts)
    self_info = await device.get_self_info()
    from ..core.models import LOCAL_DEVICE_LABEL

    device_label = str(self_info.get("name") or LOCAL_DEVICE_LABEL)
    device_hash = str(self_info.get("public_key") or "") or None

    async def burst(samples: int, path_spec: str, on_trace: Callable[[TraceResult], None]) -> None:
        """Run one persisted burst, streaming each landed trace to the screen."""
        path = trace_runner.parse_trace_path(path_spec, contacts) if path_spec.strip() else None
        params: dict[str, Any] = {"target": target, "samples": samples}
        if path:
            params["path"] = path
        run_id = ctx.repo.start_run("trace", params, ctx.profile_name)
        try:
            results = await trace_runner.run_traces(
                device,
                target,
                samples=samples,
                path=path,
                cooldown_s=ctx.settings.trace_cooldown_s,
                on_result=lambda _done, _total, result: on_trace(result),
                persist=lambda t: ctx.repo.record_trace(run_id, t),
            )
        except BaseException as exc:
            # A cancelled or failed burst still closes its run row, so no ``running``
            # orphan is left behind; the traces already recorded stay.
            ctx.repo.finish_run(run_id, "error", {"error": str(exc) or type(exc).__name__})
            raise
        stats = TraceStats.from_traces(target, results)
        ctx.repo.finish_run(
            run_id,
            "ok",
            {
                "target": stats.target,
                "samples": stats.samples,
                "success_rate": round(stats.success_rate, 3),
                "median_min_snr": stats.median_min_snr,
                "median_rtt_ms": stats.median_rtt_ms,
            },
        )

    async def edit_path(current: str) -> Optional[str]:
        """Float the forced-path prompt over the screen; blank means device-routed."""

        def validate(value: str) -> bool | str:
            if not value.strip():
                return True
            try:
                trace_runner.parse_trace_path(value, contacts)
                return True
            except ValueError as exc:
                return str(exc)

        return await session.autocomplete(
            "Force a path",
            [c.name for c in contacts],
            prompt="Comma-separated contacts/hex prefixes · blank = device-routed",
            default=current,
            validate=validate,
        )

    screen = TraceScreen(
        target,
        device_label=device_label,
        device_hash=device_hash,
        resolve=resolve,
        session=session,
        burst=burst,
        edit_path=edit_path,
        previous=ctx.repo.latest_trace(target),
    )
    screen.start_burst()
    try:
        await session.run_screen(screen)
    finally:
        screen.cancel()
    return len(screen._traces)
