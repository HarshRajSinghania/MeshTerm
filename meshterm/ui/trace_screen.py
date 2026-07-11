"""The live trace screen: compose a route from observed topology, then watch it answer.

This is the interactive face of the ``trace`` tool (the scripted CLI keeps its one-shot
table output). Where the old flow started tracing the instant a target was picked, the
screen now opens *armed but idle*: the target's last-known route shows, the path is
whatever you make it, and nothing transmits until you say so. Three verbs drive it:

* **Enter** runs a burst. While it flies, a floating *tracing* dialog (spinner, live
  reply count, Abort) sits over the screen — replies keep streaming into the log behind
  it, and Esc in the dialog cancels the burst without leaving the screen.
* **p** opens the path composer (:mod:`~meshterm.ui.path_composer`): build the outbound
  route hop by hop, each step suggested from the links observed in *received* traffic —
  traces, firmware-learned contact routes, and RX-logged packet paths — strongest first,
  with raw hex entry for nodes the data has never seen. Only the outbound leg is
  composed: the trace protocol replies back along the reversed path automatically.
* **x** explores scenarios: ranked candidate routes to the target straight from the
  topology evidence (the device's own learned route, the direct shot, and the strongest
  observed alternatives). Adopt one directly — or probe them all, a small measured burst
  per candidate, persisted as ``path_candidates`` rows and ranked reliability-first, with
  the winner offered for adoption. Evidence proposes, measurement decides, you dispose.

Layout, top to bottom: the walked route (live when a reply has landed, else the planned
composed path, else the target's last stored trace), the run's robust aggregates, per-hop
median SNR with quality bars, and the individual traces newest-first.

Every burst is persisted exactly like a scripted run: one ``runs`` row per burst, each
trace recorded under it, so the stored history reads the same no matter which front end
produced it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..core.models import TraceResult, TraceStats
from ..services import trace_runner
from .theme import snr_style
from .tui.render import render_lines, render_to_ansi
from .tui.screen import Screen
from .tui.spinner import Spinner
from .widgets import NodeResolver, _link_text, _route_text

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between spinner frames while a burst or probe is in flight.
_SPINNER_INTERVAL = 0.12

#: The sample counts the ``s`` key cycles through — small odd numbers, so the median is
#: always a real reading and the radio's duty cycle stays polite.
_SAMPLE_CYCLE = (1, 3, 5, 9)

#: Traces per candidate during a scenario probe, capped below the burst size so probing
#: several paths stays within the airtime budget of a single ordinary burst.
_PROBE_SAMPLES_CAP = 3

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

#: A path picker flow: takes the current spec, runs its own dialogs over the screen, and
#: resolves to the new spec (``""`` = device-routed) or ``None`` to keep the current one.
PathFlow = Callable[[str], Awaitable[Optional[str]]]


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


class TracingDialog(Screen):
    """The floating in-flight dialog: a spinner chip, live progress, and Abort.

    Pushed over the trace screen while a burst (or scenario probe) transmits, so the
    activity — and the way out — is unmissable while replies keep streaming into the
    screen behind it. Enter, Esc, or Space aborts via the injected callback; the owner
    pops the dialog when the work finishes, so it never resolves a value of its own.
    """

    footer_hint = "Enter/Esc abort"

    def __init__(self, title: str, *, spinner: Spinner, on_abort: Callable[[], None]) -> None:
        """Build the dialog.

        Args:
            title: Heading for the dialog border (e.g. ``tracing · YUL-Poly``).
            spinner: The spinner to animate (shared with the owner's ticker).
            on_abort: Invoked when the user asks to abort (idempotent expected).
        """
        super().__init__()
        self.title = title
        self._spinner = spinner
        #: Invoked when the user aborts. Public so an owner whose task only exists
        #: after the dialog does (the probe sweep) can rewire it.
        self.on_abort = on_abort
        #: One-line progress, updated by the owner as replies land.
        self.status = "transmitting…"
        #: The most recent trace result, echoed beneath the status line.
        self.last: Optional[TraceResult] = None

    @property
    def dialog_width(self) -> int:
        """Natural outer width hugging the widest line (compositor still caps it)."""
        widths = [cell_len(self.title), cell_len(self.footer_hint),
                  cell_len(self.status) + 4, len("last  ✗ no reply — will retry"),
                  len("[ Abort ]")]
        return max(widths) + 8

    def _last_line(self) -> Text:
        """The most recent reply, or a waiting note before the first one lands."""
        line = Text("last  ", style="muted")
        if self.last is None:
            line.append("waiting for the first reply…", style="muted")
        elif not self.last.success:
            line.append("✗ no reply", style="err")
        else:
            line.append("✓ ", style="ok")
            line.append(f"{self.last.hop_count} hop{'s' if self.last.hop_count != 1 else ''}")
            if self.last.min_snr is not None:
                line.append("  min ", style="muted")
                line.append(f"{self.last.min_snr:+.1f} dB", style=snr_style(self.last.min_snr))
            if self.last.round_trip_ms is not None:
                line.append(f"  {self.last.round_trip_ms:.0f} ms", style="muted")
        return line

    def render_body(self, width: int) -> list[str]:
        """Render the spinner chip, the last reply, and the Abort button."""
        chip = self._spinner.text()
        chip.append(f"  {self.status}", style="")
        lines = render_lines(chip, width)
        lines.extend(render_lines(self._last_line(), width))
        lines.append("")
        lines.append(render_to_ansi(Text("❯ [ Abort ]", style="selected"), width))
        return lines

    def handle(self, action: str, data: str = "") -> None:
        """Any commit/dismiss key aborts the in-flight work; everything else is inert."""
        if action in ("enter", "escape", "space"):
            self.on_abort()


class TraceScreen(Screen):
    """A full-screen live trace session for one target — armed, but idle until told.

    Keys: Enter runs a burst (a floating dialog with Abort rides on top while it flies),
    ``p`` composes the forced path hop by hop from observed topology, ``x`` explores and
    probes ranked path scenarios, ``s`` cycles the burst's sample count, the usual scroll
    keys move the view, and Esc backs out (cancelling any in-flight burst;
    already-recorded traces are kept).
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
        compose_path: PathFlow,
        explore: PathFlow,
        previous: Optional[TraceResult] = None,
        samples: int = 3,
    ) -> None:
        """Create the screen (nothing transmits until the user presses Enter).

        Args:
            target: The trace destination (contact name or key prefix).
            device_label: Our own node's name, labelling the route's endpoints.
            device_hash: Our own public key, so the endpoints carry a hash like every hop.
            resolve: Maps a hop's raw hash to a friendly contact name when known.
            session: The running TUI session (for repaints and the floating dialog).
            burst: Runs one burst of traces, streaming results (see :data:`BurstRunner`).
            compose_path: Opens the hop-by-hop path composer over this screen, seeded
                with the current spec; resolves to the new spec or ``None`` if cancelled.
            explore: Opens the scenario browser/probe flow over this screen; resolves to
                an adopted spec or ``None`` to keep the current one.
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
        self._compose_path = compose_path
        self._explore = explore
        self._previous = previous
        self._samples = samples if samples in _SAMPLE_CYCLE else 3
        self._path_spec = ""
        self._traces: list[TraceResult] = []
        self._running = False
        self._dialog_open = False
        self._status = ""
        self._burst_done = 0
        self._spinner = Spinner()
        self._worker: Optional[asyncio.Task] = None
        self._flight: Optional[TracingDialog] = None

    # --- state -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, tracking whether a burst is in flight."""
        if self._running:
            return "tracing… · ↑↓ PgUp/PgDn scroll · Esc back"
        return (
            "Enter trace · p compose path · x explore paths · s samples · "
            "↑↓ scroll · Esc back"
        )

    def start_burst(self) -> None:
        """Kick off one burst of traces in the background (no-op while one is running)."""
        if self._running:
            return
        self._running = True
        self._status = ""
        self._burst_done = 0
        self._spinner.reset()
        self._worker = asyncio.ensure_future(self._run_burst())
        self._session.invalidate()

    async def _run_burst(self) -> None:
        """Drive one burst to completion under the floating tracing dialog.

        The dialog is pushed for the duration and popped however the burst ends —
        completion, failure, or abort — and its Abort wires straight to :meth:`cancel`,
        so the burst's cancellation path is the same whether Esc lands on the dialog or
        the screen.
        """
        dialog = TracingDialog(
            f"tracing · {self._target}", spinner=self._spinner, on_abort=self.cancel
        )
        dialog.status = self._flight_status(0)
        self._flight = dialog
        self._session.push(dialog)
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
            self._session.pop(dialog)
            self._flight = None
            self._running = False
            self._session.invalidate()

    def _flight_status(self, done: int) -> str:
        """The dialog's progress line after ``done`` replies of the burst have landed."""
        current = min(done + 1, self._samples)
        return f"trace {current}/{self._samples} · {self._target}"

    async def _animate(self) -> None:
        """Advance the in-flight spinner and repaint on a steady cadence, until cancelled."""
        while True:
            await asyncio.sleep(_SPINNER_INTERVAL)
            self._spinner.tick()
            self._session.invalidate()

    def _on_trace(self, result: TraceResult) -> None:
        """Append one landed trace, advance the dialog, and repaint."""
        self._traces.append(result)
        self._burst_done += 1
        if self._flight is not None:
            self._flight.last = result
            self._flight.status = self._flight_status(self._burst_done)
        self._session.invalidate()

    def cancel(self) -> None:
        """Cancel any in-flight burst (already-recorded traces are kept)."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    # --- input -------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Run bursts, compose/explore paths, tweak samples, scroll, or dismiss."""
        if action == "enter":
            self.start_burst()
        elif action == "text" and data.lower() == "s":
            if not self._running:
                idx = _SAMPLE_CYCLE.index(self._samples)
                self._samples = _SAMPLE_CYCLE[(idx + 1) % len(_SAMPLE_CYCLE)]
        elif action == "text" and data.lower() == "p":
            self._open_flow(self._compose_path)
        elif action == "text" and data.lower() == "x":
            self._open_flow(self._explore)
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

    def _open_flow(self, flow: PathFlow) -> None:
        """Float a path-picking flow over the screen (one at a time, not mid-burst).

        Both the composer and the scenario explorer resolve the same way: a new spec to
        adopt (``""`` returns routing to the device), or ``None`` to leave the current
        path untouched.

        Args:
            flow: The dialog flow to run with the current spec.
        """
        if self._dialog_open or self._running:
            return
        self._dialog_open = True

        async def run() -> None:
            try:
                spec = await flow(self._path_spec)
                if spec is not None:
                    self._path_spec = spec.strip()
            finally:
                self._dialog_open = False
                self._session.invalidate()

        asyncio.ensure_future(run())

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
        """The route: live when a reply has landed, else planned, else the stored one."""
        line = Text("route  ", style="muted")
        if current is not None:
            line.append_text(
                _route_text(current, self._device_label, self._resolve, self._device_hash)
            )
            return line
        planned = self._planned_route()
        if planned is not None:
            line.append_text(planned)
            return line
        if self._previous is not None:
            line.append_text(
                _route_text(
                    self._previous, self._device_label, self._resolve, self._device_hash
                )
            )
            stamp = self._previous.timestamp.astimezone().strftime("%b %d %H:%M")
            line.append(f"  (previous · {stamp})", style="faint")
            return line
        line.append("unknown — press Enter to trace", style="muted")
        return line

    def _planned_route(self) -> Optional[Text]:
        """The composed outbound path as a route preview, or ``None`` without one.

        Only the outbound leg exists in the spec; the reply retraces it in reverse, so
        the preview says so instead of drawing a mirrored (and redundant) return chain.
        """
        hops = [h.strip() for h in self._path_spec.split(",") if h.strip()]
        if not hops:
            return None
        text = Text(self._device_label, style="accent")
        for hop in hops:
            text.append(" → ", style="muted")
            named = self._resolve(hop)
            if named and named != hop:
                text.append(named, style="brand")
                text.append(f" ({hop})", style="muted")
            else:
                text.append(hop, style="brand")
        text.append("  ⟲ auto return  (planned)", style="faint")
        return text

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


def _collapse_trace_width(mode: int) -> int:
    """Collapse a routing hash mode to the widest trace-representable hop width.

    Routing widths are ``mode + 1`` bytes, but a trace's flags can only encode 1, 2, 4,
    or 8 (see :func:`~meshterm.services.trace_runner.path_hash_flags`); the firmware
    matches by prefix, so collapsing down addresses the same nodes.

    Args:
        mode: The path-hash mode (``size - 1``); negative means unknown.

    Returns:
        The per-hop width in bytes (1, 2, 4, or 8).
    """
    size = max(mode + 1, 1)
    return max(s for s in (1, 2, 4, 8) if s <= size)


async def open_trace(ctx: "AppContext", target: str) -> int:
    """Open the live trace screen for ``target`` and run it until dismissed.

    Wires the screen to the radio, the database, and the observed-topology services:
    each burst opens its own ``runs`` row and records every trace under it (same shape a
    scripted ``meshterm trace`` writes); the composer and scenario flows build a fresh
    :class:`~meshterm.services.topology.MeshTopology` from stored evidence on each open,
    so suggestions always reflect the latest received traffic. Nothing transmits until
    the user asks — the screen opens idle, its route line seeded from the target's most
    recent stored trace.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        target: The trace destination (contact name or key prefix).

    Returns:
        The number of traces run while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..core.models import LOCAL_DEVICE_LABEL
    from ..services.path_probe import ProbeCandidate, ProbeOutcome, probe_paths
    from ..services.topology import MeshTopology, PathScenario, _is_hex, build_topology
    from .path_composer import PathComposerScreen
    from .surface import TuiUi
    from .tui import CANCEL, Choice, SelectScreen, Separator

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the live trace screen is only available in the menu")
    session = ctx.ui.session

    device = await ctx.device()
    contacts = await device.get_contacts()
    resolve = trace_runner.make_node_resolver(contacts)
    self_info = await device.get_self_info()

    device_label = str(self_info.get("name") or LOCAL_DEVICE_LABEL)
    device_hash = str(self_info.get("public_key") or "") or None

    # The per-hop width composed/scenario specs are emitted at: the region's routing
    # width, collapsed to what a trace can encode. Unknown (old firmware) → 1 byte, the
    # protocol default and what all stored evidence uses anyway.
    try:
        width_bytes = _collapse_trace_width(int(await device.get_path_hash_mode()))
    except Exception:  # noqa: BLE001 - optional read; the 1-byte default always works
        width_bytes = 1

    # The target as an addressable hash: a known contact's full key, or the typed hex
    # prefix itself. A non-hex unknown target can still be traced device-routed, but
    # composing/exploring needs a destination hash to pin the path on.
    needle = target.casefold()
    target_contact = next(
        (
            c
            for c in contacts
            if c.name.casefold() == needle
            or ((c.public_key or "").lower().startswith(target.lower()))
        ),
        None,
    )
    raw_hash = (
        (target_contact.public_key or target_contact.key_prefix)
        if target_contact
        else target
    ).lower().removeprefix("0x")
    target_hash = raw_hash if _is_hex(raw_hash) else None
    target_label = target_contact.name if target_contact else target

    def fresh_topology() -> MeshTopology:
        """Build the evidence graph from everything currently stored + on the device."""
        return build_topology(
            self_id=device_hash or "local",
            contacts=contacts,
            trace_paths=ctx.repo.trace_paths(),
            packet_paths=ctx.repo.packet_paths(),
        )

    async def unaddressable() -> None:
        """Explain why path features need a resolvable target, in a small dialog."""
        await session.message_dialog(
            Text(
                f"{target!r} isn't a known contact or a hex key prefix, so a forced "
                "path can't end at it. Trace it device-routed, or pick a contact.",
            ),
            title="no destination hash",
        )

    async def compose(current: str) -> Optional[str]:
        """Open the hop-by-hop composer seeded with the current spec's hops."""
        if target_hash is None:
            await unaddressable()
            return None
        topo = fresh_topology()
        target_id = topo.canonical(target_hash) or target_hash[:12]
        # Re-seed from the current spec, dropping its final hop (the target itself).
        seed = [
            cid
            for h in [p for p in current.split(",") if p.strip()][:-1]
            if (cid := topo.canonical(h.strip())) is not None
        ]
        result = await session.run_screen(
            PathComposerScreen(
                target_id=target_id,
                target_hash=target_hash,
                target_label=target_label,
                device_label=device_label,
                topology=topo,
                width_bytes=width_bytes,
                hops=seed,
            )
        )
        return None if result is CANCEL else result

    def scenario_title(scenario: PathScenario, topo: MeshTopology) -> Text:
        """One scenario as a select row: source, route, and its observed evidence."""
        styles = {"device": "accent", "direct": "muted", "observed": "brand"}
        text = Text(scenario.label, style=styles.get(scenario.source, ""))
        if scenario.hops:
            text.append("  via ", style="muted")
            names = [topo.display_name(h) or h for h in scenario.hops]
            text.append(" → ".join(names))
        else:
            text.append("  no repeaters", style="muted")
        if scenario.weakest_snr is not None:
            text.append("  ·  weakest ", style="muted")
            text.append(f"{scenario.weakest_snr:+.1f} dB", style=snr_style(scenario.weakest_snr))
        if scenario.samples:
            text.append(f"  ·  {scenario.samples}×", style="muted")
        elif scenario.score == 0:
            text.append("  ·  unobserved", style="faint")
        return text

    def outcome_title(rank: int, outcome: ProbeOutcome) -> Text:
        """One probed candidate as a ranked select row: measured, not guessed."""
        stats = outcome.stats
        rate_style = "ok" if stats.success_rate >= 1.0 else ("warn" if stats.successes else "err")
        text = Text(f"#{rank}  ", style="muted")
        text.append(f"{stats.success_rate:.0%}", style=rate_style)
        snr = stats.median_min_snr
        if snr is not None:
            text.append("  min ", style="muted")
            text.append(f"{snr:+.1f} dB", style=snr_style(snr))
        if stats.median_rtt_ms is not None:
            text.append(f"  {stats.median_rtt_ms:.0f} ms", style="muted")
        text.append("  via ", style="muted")
        text.append(outcome.candidate.spec, style="brand")
        text.append(f"  ({outcome.candidate.label})", style="faint")
        return text

    async def run_probe(
        candidates: list[ProbeCandidate], samples: int
    ) -> Optional[list[ProbeOutcome]]:
        """Measure every candidate under an abortable dialog; ``None`` when aborted.

        One ``runs`` row spans the sweep; each trace and each candidate aggregate is
        recorded under it, so an aborted probe still keeps everything it measured.
        """
        run_id = ctx.repo.start_run(
            "trace",
            {
                "target": target,
                "mode": "probe",
                "samples": samples,
                "paths": [c.spec for c in candidates],
            },
            ctx.profile_name,
        )
        spinner = Spinner()
        dialog = TracingDialog(f"probing · {target_label}", spinner=spinner, on_abort=lambda: None)
        dialog.status = f"path 1/{len(candidates)} · trace 1/{samples}"

        def on_result(index: int, done: int, result: TraceResult) -> None:
            current = min(done + 1, samples)
            dialog.status = f"path {index + 1}/{len(candidates)} · trace {current}/{samples}"
            dialog.last = result
            session.invalidate()

        sweep = asyncio.ensure_future(
            probe_paths(
                device,
                target,
                candidates,
                samples=samples,
                cooldown_s=ctx.settings.trace_cooldown_s,
                on_result=on_result,
                persist_trace=lambda t: ctx.repo.record_trace(run_id, t),
                persist_candidate=lambda o: ctx.repo.record_path_candidate(
                    run_id,
                    target,
                    o.candidate.spec,
                    bottleneck_snr=o.stats.median_min_snr,
                    success_rate=o.stats.success_rate,
                    median_rtt_ms=o.stats.median_rtt_ms,
                ),
            )
        )
        dialog.on_abort = sweep.cancel  # the task exists only now; rewire the button

        async def animate() -> None:
            while True:
                await asyncio.sleep(_SPINNER_INTERVAL)
                spinner.tick()
                session.invalidate()

        ticker = asyncio.ensure_future(animate())
        session.push(dialog)
        error: Optional[BaseException] = None
        aborted = False
        outcomes: list[ProbeOutcome] = []
        try:
            outcomes = await sweep
        except asyncio.CancelledError:
            aborted = True
        except Exception as exc:  # noqa: BLE001 - surface in a dialog, keep the screen
            error = exc
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a probe
                pass
            session.pop(dialog)
        if aborted:
            # Everything measured before the abort is already persisted; the run row
            # just records that the sweep didn't finish.
            ctx.repo.finish_run(run_id, "error", {"error": "aborted"})
            return None
        if error is not None:
            ctx.repo.finish_run(
                run_id, "error", {"error": str(error) or type(error).__name__}
            )
            await session.message_dialog(
                Text(f"probe failed: {error}", style="err"), title="path probe"
            )
            return None
        best = outcomes[0] if outcomes else None
        ctx.repo.finish_run(
            run_id,
            "ok",
            {
                "target": target,
                "paths": len(candidates),
                "best_path": best.candidate.spec if best else None,
                "best_success_rate": round(best.stats.success_rate, 3) if best else None,
                "best_median_min_snr": best.stats.median_min_snr if best else None,
            },
        )
        return outcomes

    async def explore(current: str) -> Optional[str]:
        """The scenario flow: browse ranked candidate routes, adopt one, or probe all."""
        if target_hash is None:
            await unaddressable()
            return None
        topo = fresh_topology()
        target_id = topo.canonical(target_hash) or target_hash[:12]
        device_route: Optional[tuple[str, ...]] = None
        if target_contact is not None and target_contact.route_hops is not None:
            device_route = tuple(
                topo.canonical(h) or h for h in target_contact.route_hops
            )
        scenarios = topo.scenarios(target_id, device_route=device_route)
        if not scenarios:
            await session.message_dialog(
                Text("no observed evidence involving this target yet — run a trace or "
                     "let monitoring accumulate paths first.", style="muted"),
                title="explore paths",
            )
            return None

        items: list = [Separator("── Candidate paths · from received evidence ──", style="accent")]
        for scenario in scenarios:
            items.append(
                Choice(title=scenario_title(scenario, topo), value=("use", scenario))
            )
        items.append(Separator(" "))
        items.append(
            Choice(
                title=Text.assemble(("⚡ ", "warn"), "Probe all — trace each path and rank"),
                value=("probe", None),
            )
        )
        items.append(Choice(title="Back", value=("back", None)))
        picked = await session.run_screen(
            SelectScreen(
                f"reach {target_label} · scenarios",
                items,
                prompt="Outbound path only — the reply always retraces it in reverse.",
                footer_hint="↑↓ move · Enter adopt/probe · Esc back",
                wrap=False,
            )
        )
        if picked is CANCEL or picked is None or picked[0] == "back":
            return None
        if picked[0] == "use":
            return picked[1].spec(target_hash, width_bytes)

        # Probe all: measure each distinct spec (scenarios can collapse to the same
        # spec once truncated to the trace width — the radio must not walk one twice).
        candidates: list[ProbeCandidate] = []
        for scenario in scenarios:
            spec = scenario.spec(target_hash, width_bytes)
            if all(c.spec != spec for c in candidates):
                candidates.append(ProbeCandidate(label=scenario.label, spec=spec))
        outcomes = await run_probe(candidates, _PROBE_SAMPLES_CAP)
        if not outcomes:
            return None
        result_items: list = [
            Separator("── Ranked · reliability, then bottleneck SNR ──", style="accent")
        ]
        for rank, outcome in enumerate(outcomes, start=1):
            result_items.append(Choice(title=outcome_title(rank, outcome), value=outcome))
        result_items.append(Separator(" "))
        result_items.append(Choice(title="Keep current path", value=None))
        adopted = await session.run_screen(
            SelectScreen(
                f"probe results · {target_label}",
                result_items,
                footer_hint="↑↓ move · Enter adopt path · Esc keep current",
                wrap=False,
            )
        )
        if adopted is CANCEL or adopted is None:
            return None
        return adopted.candidate.spec

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

    screen = TraceScreen(
        target,
        device_label=device_label,
        device_hash=device_hash,
        resolve=resolve,
        session=session,
        burst=burst,
        compose_path=compose,
        explore=explore,
        previous=ctx.repo.latest_trace(target),
    )
    try:
        await session.run_screen(screen)
    finally:
        screen.cancel()
    return len(screen._traces)
