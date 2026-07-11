"""The live trace screen: compose a route from observed topology, then watch it answer.

This is the interactive face of the ``trace`` tool (the scripted CLI keeps its one-shot
table output). Where the old flow started tracing the instant a target was picked, the
screen now opens *armed but idle*: the target's last-known route shows, the path is
whatever you make it, and nothing transmits until you say so. An action list drives it —
↑/↓ select a row, Enter commits it, and each row keeps its hotkey:

* **Trace** (Enter — the cursor opens here, so plain Enter still just traces) runs a
  *single* trace. While it flies, a floating *tracing* dialog (spinner, Abort) sits over
  the screen — the reply streams into the log behind it, and Esc in the dialog cancels
  the trace without leaving the screen. One transmission per keypress is deliberate:
  repeaters penalize chatty nodes (flood detection can blacklist us), so sampling is
  left human-paced — commit Trace again and the screen keeps aggregating every trace of
  the session into its medians.
* **Path width** (``w``) floats a small dialog picking the per-hop path-hash width (1,
  2, 4, or 8 bytes — the leading key slice forced hops are addressed by). It seeds from
  the device's routing width; changing it re-renders a standing forced path at the new
  width and shapes every spec the composer and explorer emit after it.
* **Compose path** (``p``) opens the path composer (:mod:`~meshterm.ui.path_composer`):
  build the route hop by hop, each step suggested from the links observed in *received*
  traffic — traces, firmware-learned contact routes, and RX-logged packet paths —
  strongest first, with raw hex entry for nodes the data has never seen. Standing on a
  repeater you hold admin credentials for, the composer can also *fetch that repeater's
  neighbour table* over the mesh (login required; firmware ignores guests):
  second-vantage evidence, persisted and folded straight back into the suggestions.
  By default only the outbound leg is composed and the return is those hops mirrored —
  the spec sent to the device spells out the whole boomerang, since the trace protocol
  has no separate return-path field — but the composer's ⇄ row switches to asymmetric,
  where the entire walk (out *and* home) is yours to route.
* **Explore paths** (``x``) explores scenarios: ranked candidate routes to the target
  straight from the topology evidence (the device's own learned route, the direct shot,
  and the strongest observed alternatives). Adopt one directly — or probe them all, one
  measured trace per candidate (the same single-transmission rule), persisted as
  ``path_candidates`` rows and ranked reliability-first, with the winner offered for
  adoption. Evidence proposes, measurement decides, you dispose.

Layout, top to bottom: the walked route (live when a reply has landed, else the planned
composed path, else the target's last stored trace), the run's robust aggregates, the
action list, per-hop median SNR with quality bars, and the individual traces
newest-first. The body scrolls with PgUp/PgDn/Home/End (↑/↓ belong to the action
cursor, which only pins the view while it is actually being moved).

Every trace is persisted exactly like a scripted run: one ``runs`` row per trace,
recorded under it, so the stored history reads the same no matter which front end
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
from .widgets import NodeResolver, _link_text, _route_text, highlighted_hash

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between spinner frames while a trace or probe is in flight.
_SPINNER_INTERVAL = 0.12

#: How wide the per-hop SNR quality bars draw, in cells.
_BAR_WIDTH = 16

#: The SNR range the bars span, in dB: -15 (barely readable) to +10 (excellent). Values
#: outside clamp to the ends, so the bar always shows *something* for a heard hop.
_BAR_SNR_MIN = -15.0
_BAR_SNR_MAX = 10.0

#: A single-trace runner: ``(path_spec, on_trace)`` → runs exactly one trace, handing
#: the result to ``on_trace`` when it lands. Provided by :func:`open_trace`, which closes
#: over the device, repository, and settings so the screen stays free of persistence
#: concerns.
TraceOnce = Callable[[str, Callable[[TraceResult], None]], Awaitable[None]]

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

    Pushed over the trace screen while a trace (or scenario probe) transmits, so the
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
        #: Whether to render the last-reply line. Trace/probe owners stream replies
        #: through it; request-shaped owners (a neighbour fetch) have none to show.
        self.show_last = True

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
        if self.show_last:
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

    An action list carries the verbs: *Trace* (one transmission per commit, because
    repeaters can blacklist nodes that burst traffic; a floating dialog with Abort
    rides on top while it flies), *Path width* (``w``), *Compose path* (``p``), and
    *Explore paths* (``x``). ↑/↓ move the cursor over the rows and Enter commits the
    selected one — the cursor opens on Trace, so plain Enter still just traces.
    PgUp/PgDn/Home/End scroll the body, and Esc backs out (cancelling any in-flight
    trace; already-recorded traces are kept).
    """

    floating = False

    #: The action rows, in display order (each also keeps a hotkey — see :meth:`handle`).
    _ACTIONS: tuple[str, ...] = ("trace", "width", "compose", "explore")

    def __init__(
        self,
        target: str,
        *,
        device_label: str,
        device_hash: Optional[str],
        resolve: NodeResolver,
        session: Any,
        trace: TraceOnce,
        compose_path: PathFlow,
        explore: PathFlow,
        pick_width: PathFlow,
        width_bytes: Callable[[], int],
        previous: Optional[TraceResult] = None,
    ) -> None:
        """Create the screen (nothing transmits until the user commits Trace).

        Args:
            target: The trace destination (contact name or key prefix).
            device_label: Our own node's name, labelling the route's endpoints.
            device_hash: Our own public key, so the endpoints carry a hash like every hop.
            resolve: Maps a hop's raw hash to a friendly contact name when known.
            session: The running TUI session (for repaints and the floating dialog).
            trace: Runs exactly one trace, handing back the result (see
                :data:`TraceOnce`).
            compose_path: Opens the hop-by-hop path composer over this screen, seeded
                with the current spec; resolves to the new spec or ``None`` if cancelled.
            explore: Opens the scenario browser/probe flow over this screen; resolves to
                an adopted spec or ``None`` to keep the current one.
            pick_width: Floats the path-hash width dialog; resolves to the standing
                spec re-rendered at the chosen width, or ``None`` when nothing changes
                (same :data:`PathFlow` shape as the other flows).
            width_bytes: Reads the currently chosen per-hop width, for the action row's
                label (the owner holds the width, since the flows emit specs at it).
            previous: The target's most recent stored trace, if any — its route seeds the
                route line so the screen opens knowing the path history last saw.
        """
        super().__init__()
        self.title = f"trace · {target}"
        self._target = target
        self._device_label = device_label
        self._device_hash = device_hash
        self._resolve = resolve
        self._session = session
        self._trace_once = trace
        self._compose_path = compose_path
        self._explore = explore
        self._pick_width = pick_width
        self._width_bytes = width_bytes
        self._previous = previous
        self._path_spec = ""
        self._traces: list[TraceResult] = []
        self._running = False
        self._dialog_open = False
        self._status = ""
        self._spinner = Spinner()
        self._worker: Optional[asyncio.Task] = None
        self._flight: Optional[TracingDialog] = None
        self._index = 0  # cursor over the action rows; 0 keeps Enter = trace
        self._pin_cursor = False  # only pin the view while ↑/↓ are actually in use

    # --- state -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, tracking whether a trace is in flight."""
        if self._running:
            return "tracing… · PgUp/PgDn scroll · Esc back"
        return "↑↓ actions · Enter run · PgUp/PgDn scroll · Esc back"

    def start_trace(self) -> None:
        """Kick off one trace in the background (no-op while one is already flying)."""
        if self._running:
            return
        self._running = True
        self._status = ""
        self._spinner.reset()
        self._worker = asyncio.ensure_future(self._run_trace())
        self._session.invalidate()

    async def _run_trace(self) -> None:
        """Drive one trace to completion under the floating tracing dialog.

        The dialog is pushed for the duration and popped however the trace ends —
        completion, failure, or abort — and its Abort wires straight to :meth:`cancel`,
        so the cancellation path is the same whether Esc lands on the dialog or the
        screen.
        """
        dialog = TracingDialog(
            f"tracing · {self._target}", spinner=self._spinner, on_abort=self.cancel
        )
        self._flight = dialog
        self._session.push(dialog)
        ticker = asyncio.ensure_future(self._animate())
        try:
            await self._trace_once(self._path_spec, self._on_trace)
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
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a trace
                pass
            self._session.pop(dialog)
            self._flight = None
            self._running = False
            self._session.invalidate()

    async def _animate(self) -> None:
        """Advance the in-flight spinner and repaint on a steady cadence, until cancelled."""
        while True:
            await asyncio.sleep(_SPINNER_INTERVAL)
            self._spinner.tick()
            self._session.invalidate()

    def _on_trace(self, result: TraceResult) -> None:
        """Append the landed trace, echo it on the dialog, and repaint."""
        self._traces.append(result)
        if self._flight is not None:
            self._flight.last = result
        self._session.invalidate()

    def cancel(self) -> None:
        """Cancel any in-flight trace (already-recorded traces are kept)."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    # --- input -------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the action cursor, commit the selected action, scroll, or dismiss.

        ↑/↓ belong to the action cursor (and pin the view to it); the body scrolls
        with PgUp/PgDn/Home/End, each of which releases the pin so a long trace log
        can be read without the cursor yanking the view back.
        """
        if action == "enter":
            self._commit_action()
        elif action == "up":
            self._index = (self._index - 1) % len(self._ACTIONS)
            self._pin_cursor = True
        elif action == "down":
            self._index = (self._index + 1) % len(self._ACTIONS)
            self._pin_cursor = True
        elif action == "text" and data.lower() == "p":
            self._open_flow(self._compose_path)
        elif action == "text" and data.lower() == "x":
            self._open_flow(self._explore)
        elif action == "text" and data.lower() == "w":
            self._open_flow(self._pick_width)
        elif action == "pageup":
            self._pin_cursor = False
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self._pin_cursor = False
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self._pin_cursor = False
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self._pin_cursor = False
            self.scroll_to_bottom()
        elif action == "escape":
            self.cancel()
            self.resolve(None)

    def _commit_action(self) -> None:
        """Run the action row under the cursor (the flows guard against re-entry)."""
        key = self._ACTIONS[self._index]
        if key == "trace":
            self.start_trace()
        elif key == "width":
            self._open_flow(self._pick_width)
        elif key == "compose":
            self._open_flow(self._compose_path)
        else:
            self._open_flow(self._explore)

    def _open_flow(self, flow: PathFlow) -> None:
        """Float a path-picking flow over the screen (one at a time, not mid-trace).

        The composer, the scenario explorer, and the width picker all resolve the
        same way: a new spec to adopt (``""`` returns routing to the device), or
        ``None`` to leave the current path untouched.

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
        """Render the route, aggregates, action list, per-hop medians, and the log.

        The action rows are rendered line by line (not through one Rich group) so the
        highlighted row's body line is known exactly — that is what :meth:`cursor_line`
        pins while the user is navigating.
        """
        stats = TraceStats.from_traces(self._target, self._traces)
        current = next((t for t in reversed(self._traces) if t.success), None)
        lines = render_lines(
            Group(self._route_line(current), Text(), self._summary(stats)), width
        )
        lines.append("")
        self._cursor: Optional[int] = None
        for i, key in enumerate(self._ACTIONS):
            selected = i == self._index
            text = self._action_text(key, selected)
            text.no_wrap = True
            text.truncate(width, overflow="ellipsis")
            if selected:
                self._cursor = len(lines)
            lines.append(render_to_ansi(text, width))
        tail: list[RenderableType] = []
        if stats.hop_snrs:
            hash_bytes = current.path_hash_bytes if current is not None else None
            tail += [Text(), Text("Per-hop medians", style="accent")]
            tail.append(self._hops_table(stats, hash_bytes))
        tail += [Text(), Text("Traces", style="accent")]
        tail.append(self._trace_log())
        lines.extend(render_lines(Group(*tail), width))
        self._scroll_total = max(1, len(lines))
        return lines

    def cursor_line(self) -> Optional[int]:
        """The highlighted action row while ↑/↓ are in use; free scrolling otherwise.

        Returning ``None`` between navigations matters: the frame force-keeps a
        cursor line visible, which would otherwise stop PgDn ever scrolling the
        action list off screen to read a long trace log.
        """
        return getattr(self, "_cursor", None) if self._pin_cursor else None

    def _action_text(self, key: str, selected: bool) -> Text:
        """One action row: pointer, glyph, label, and the row's hotkey, muted."""
        text = Text("❯ " if selected else "  ", style="brand" if selected else "")
        if key == "trace":
            text.append("▶ ", style="ok")
            text.append("Trace — one transmission")
        elif key == "width":
            w = self._width_bytes()
            text.append("⚙ ", style="accent")
            text.append(f"Path width — {w} byte{'s' if w != 1 else ''} per hop")
            text.append("  w", style="muted")
        elif key == "compose":
            text.append("✎ ", style="brand")
            text.append("Compose path")
            text.append("  p", style="muted")
        else:
            text.append("⚡ ", style="warn")
            text.append("Explore paths")
            text.append("  x", style="muted")
        if selected:
            text.style = "brand"
        return text

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
        """The composed path as a route preview, or ``None`` without one.

        ``_path_spec`` is the literal wire spec — the whole walk, since the trace
        protocol has no separate return-path field. A spec that reads the same
        reversed is a symmetric boomerang (outbound hops, the target, those hops
        mirrored): its second half is dimmed, reading as "this part isn't yours to
        compose." Anything else was hand-composed hop by hop, so every hop renders
        in full colour and only the automatic landing back on us stays faint.
        """
        tokens = [h.strip() for h in self._path_spec.split(",") if h.strip()]
        if not tokens:
            return None
        if len(tokens) % 2 and tokens == tokens[::-1]:
            mid = len(tokens) // 2  # outbound hops + target = first half, inclusive
            outbound, return_leg = tokens[: mid + 1], tokens[mid + 1 :]
        else:
            outbound, return_leg = tokens, []
        text = Text(self._device_label, style="accent")
        for hop in outbound:
            text.append(" → ", style="muted")
            text.append_text(self._planned_hop_text(hop, dim=False))
        for hop in return_leg:
            text.append(" → ", style="faint")
            text.append_text(self._planned_hop_text(hop, dim=True))
        text.append(" → ", style="faint")
        text.append(self._device_label, style="faint")
        return text

    def _planned_hop_text(self, hop: str, *, dim: bool) -> Text:
        """One planned-path node: resolved name (with hash) or bare hash.

        Args:
            hop: The hop's raw hex key prefix.
            dim: Whether to render in the return leg's faint style rather than the
                outbound leg's normal brand/muted styling.
        """
        style = "faint" if dim else "brand"
        text = Text()
        named = self._resolve(hop)
        if named and named != hop:
            text.append(named, style=style)
            text.append(f" ({hop})", style="faint" if dim else "muted")
        else:
            text.append(hop, style=style)
        return text

    def _summary(self, stats: TraceStats) -> Text:
        """The session's aggregates plus the current path, label-aligned."""
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
            ("path            ", "muted"),
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
    each trace opens its own ``runs`` row and is recorded under it (same shape a
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
    from ..core.connection import DeviceAuthenticationError
    from ..core.models import LOCAL_DEVICE_LABEL, Contact, NeighbourInfo
    from ..services.path_probe import ProbeCandidate, ProbeOutcome, probe_paths
    from ..services.topology import (
        MeshTopology,
        PathScenario,
        _is_hex,
        build_topology,
        collapse_width,
    )
    from .path_composer import FetchNeighbours, PathComposerScreen
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
    # protocol default and what all stored evidence uses anyway. The user can override
    # it for the session through the screen's *Path width* action (see pick_width).
    try:
        width_bytes = _collapse_trace_width(int(await device.get_path_hash_mode()))
    except Exception:  # noqa: BLE001 - optional read; the 1-byte default always works
        width_bytes = 1
    device_width = width_bytes  # remembered so the width dialog can mark the default

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
            neighbour_links=ctx.repo.neighbour_links(),
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

    async def fetch_neighbours_via(repeater: Contact, repeater_id: str) -> bool:
        """Log in to a repeater, fetch its neighbour table, and persist the snapshot.

        The composer's fetch action lands here: password from the admin store (or a
        one-time prompt, remembered on success — the tx-optimize convention), then the
        login + fetch run under a floating spinner dialog with Abort. Every outcome
        closes its own ``runs`` row; a rejected login also forgets the stored password
        so the next attempt asks fresh.

        Args:
            repeater: The repeater contact to query (carries the public key).
            repeater_id: Its canonical id, the key the snapshot is stored under.

        Returns:
            ``True`` when new neighbour links were recorded (the caller should rebuild
            the topology); ``False`` on cancel, failure, or an empty table.
        """
        password = ctx.admin_store.get(repeater)
        if password is None:
            password = await session.text(
                f"Admin password for {repeater.name}",
                prompt="The repeater ignores neighbour requests without an admin login.",
                password=True,
            )
            if not password:
                return False
        run_id = ctx.repo.start_run(
            "trace",
            {"mode": "neighbours", "repeater": repeater.name},
            ctx.profile_name,
        )
        spinner = Spinner()
        dialog = TracingDialog(
            f"neighbours · {repeater.name}", spinner=spinner, on_abort=lambda: None
        )
        dialog.show_last = False  # a fetch has no streaming replies to echo
        dialog.status = f"logging in to {repeater.name}…"

        async def work() -> list[NeighbourInfo]:
            if not await device.admin_login(repeater, password):
                raise DeviceAuthenticationError(
                    f"{repeater.name!r} rejected the admin login (wrong password?). "
                    "The saved password was cleared; retry to enter a new one."
                )
            ctx.admin_store.remember(repeater, password)
            dialog.status = "fetching the neighbour table…"
            session.invalidate()
            return await device.fetch_neighbours(repeater)

        task = asyncio.ensure_future(work())
        dialog.on_abort = task.cancel

        async def animate() -> None:
            while True:
                await asyncio.sleep(_SPINNER_INTERVAL)
                spinner.tick()
                session.invalidate()

        ticker = asyncio.ensure_future(animate())
        session.push(dialog)
        error: Optional[BaseException] = None
        aborted = False
        entries: list[NeighbourInfo] = []
        try:
            entries = await task
        except asyncio.CancelledError:
            aborted = True
        except Exception as exc:  # noqa: BLE001 - surface in a dialog, keep composing
            error = exc
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a fetch
                pass
            session.pop(dialog)
        if aborted:
            ctx.repo.finish_run(run_id, "error", {"error": "aborted"})
            return False
        if error is not None:
            if isinstance(error, DeviceAuthenticationError):
                ctx.admin_store.forget(repeater)  # bad password: don't keep reusing it
            ctx.repo.finish_run(
                run_id, "error", {"error": str(error) or type(error).__name__}
            )
            await session.message_dialog(
                Text(str(error), style="err"), title="fetch neighbours"
            )
            return False
        ctx.repo.record_neighbours(run_id, repeater_id, entries)
        ctx.repo.finish_run(
            run_id, "ok", {"repeater": repeater.name, "neighbours": len(entries)}
        )
        if not entries:
            # Verified on real firmware: an empty table is a normal answer (repeaters
            # forget neighbours across reboots and relearn them from adverts).
            await session.message_dialog(
                Text(
                    f"{repeater.name} answered, but its neighbour table is empty — "
                    "it relearns neighbours from received adverts, so ask again later.",
                    style="muted",
                ),
                title="fetch neighbours",
            )
            return False
        return True

    async def compose(current: str) -> Optional[str]:
        """Open the hop-by-hop composer seeded with the current spec's hops.

        Runs the composer in a loop: a :class:`FetchNeighbours` resolution performs the
        fetch, rebuilds the topology with the new evidence, and reopens the composer
        exactly where the user stood (same hops, refreshed suggestions).
        """
        if target_hash is None:
            await unaddressable()
            return None
        topo = fresh_topology()
        target_id = topo.canonical(target_hash) or target_hash[:12]
        # Re-seed from the current spec. A spec that reads the same reversed is the
        # symmetric boomerang (hops, target, mirror): seed just the outbound hops and
        # let the composer regenerate the rest. Anything else was hand-composed, so
        # reopen in asymmetric mode with every hop of the walk editable. Tokens no
        # contact matches are kept verbatim rather than dropped — an adopted route
        # must survive a reopen even where the evidence graph is blind.
        tokens = [p.strip() for p in current.split(",") if p.strip()]
        ids = [topo.canonical(h) or h for h in tokens]
        symmetric = not ids or (len(ids) % 2 == 1 and ids == ids[::-1])
        seed = ids[: len(ids) // 2] if symmetric else ids
        while True:
            # Nodes whose neighbour table can be asked for: repeater contacts with a
            # public key to log in against (our own node has nothing new to tell us).
            fetchable: dict[str, Contact] = {}
            for c in contacts:
                if not c.is_repeater or not (c.public_key or "").strip():
                    continue
                cid = topo.canonical(c.public_key)
                if cid is not None and cid != topo.self_id:
                    fetchable[cid] = c
            screen = PathComposerScreen(
                target_id=target_id,
                target_hash=target_hash,
                target_label=target_label,
                device_label=device_label,
                topology=topo,
                width_bytes=width_bytes,
                hops=seed,
                fetch_nodes=frozenset(fetchable),
                symmetric=symmetric,
            )
            result = await session.run_screen(screen)
            if result is CANCEL:
                return None
            if isinstance(result, FetchNeighbours):
                seed = screen.hops  # resume mid-thought after the fetch
                symmetric = screen.symmetric
                repeater = fetchable.get(result.node)
                if repeater is not None and await fetch_neighbours_via(
                    repeater, result.node
                ):
                    topo = fresh_topology()  # fold the new reports into suggestions
                continue
            return result

    async def pick_width(current: str) -> Optional[str]:
        """Float the path-hash width picker and re-render the standing spec to match.

        Each row previews the target's key with the addressed slice lit at that
        width, so the choice reads as "this much of every key goes on the air". The
        chosen width shapes every spec the composer/explorer emit afterwards; a
        standing forced path is re-rendered immediately — hops are widened back
        through their canonical hashes where known, then collapsed uniformly (a hop
        only ever known narrower keeps the whole spec at what it can honour).

        Returns:
            The re-rendered spec, or ``None`` when cancelled, unchanged, or there is
            no forced path to re-render (the width itself still sticks).
        """
        nonlocal width_bytes
        sample = (target_hash or device_hash or "").lower().removeprefix("0x")
        items: list = []
        for w in (1, 2, 4, 8):
            title = Text(f"{w} byte{'s' if w > 1 else ' '}")
            if sample:
                title.append("  ")
                title.append_text(highlighted_hash(sample[:16], w))
            if w == device_width:
                title.append("  · device default", style="muted")
            items.append(Choice(title=title, value=w))
        picked = await session.run_screen(
            SelectScreen(
                "path width",
                items,
                prompt="Forced hops are addressed by this many leading bytes of each key.",
                default=width_bytes,
                footer_hint="↑↓ · Enter set · Esc keep",
                filterable=False,
                wrap=False,
            )
        )
        if picked is CANCEL or picked is None or int(picked) == width_bytes:
            return None
        width_bytes = int(picked)
        tokens = [p.strip() for p in current.split(",") if p.strip()]
        if not tokens:
            return None
        topo = fresh_topology()
        full = [topo.canonical(t) or t for t in tokens]
        width = collapse_width(*full, ceiling=width_bytes)
        return ",".join(f[: width * 2] for f in full)

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
        """One probed candidate as a ranked select row: one measured trace, not a guess."""
        stats = outcome.stats
        text = Text(f"#{rank}  ", style="muted")
        if stats.successes:
            text.append("✓ replied", style="ok")
        else:
            text.append("✗ no reply", style="err")
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
        candidates: list[ProbeCandidate],
    ) -> Optional[list[ProbeOutcome]]:
        """Measure every candidate — one trace each — under an abortable dialog.

        One ``runs`` row spans the sweep; each trace and each candidate aggregate is
        recorded under it, so an aborted probe still keeps everything it measured.
        Each candidate gets exactly one transmission (the screen-wide rule: repeaters
        can blacklist nodes that burst traffic); ``None`` when aborted.
        """
        run_id = ctx.repo.start_run(
            "trace",
            {
                "target": target,
                "mode": "probe",
                "paths": [c.spec for c in candidates],
            },
            ctx.profile_name,
        )
        spinner = Spinner()
        dialog = TracingDialog(f"probing · {target_label}", spinner=spinner, on_abort=lambda: None)
        dialog.status = f"path 1/{len(candidates)} · one trace each"

        def on_result(index: int, done: int, result: TraceResult) -> None:
            dialog.status = f"path {index + 1}/{len(candidates)} · one trace each"
            dialog.last = result
            session.invalidate()

        sweep = asyncio.ensure_future(
            probe_paths(
                device,
                target,
                candidates,
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
                title=Text.assemble(("⚡ ", "warn"), "Probe all — trace each path once and rank"),
                value=("probe", None),
            )
        )
        items.append(Choice(title="Back", value=("back", None)))
        picked = await session.run_screen(
            SelectScreen(
                f"reach {target_label} · scenarios",
                items,
                prompt="Choose the outbound leg — the return is always those hops mirrored.",
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
        outcomes = await run_probe(candidates)
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

    async def trace_once(path_spec: str, on_trace: Callable[[TraceResult], None]) -> None:
        """Run one persisted trace: one transmission, its own ``runs`` row.

        A single transmission per keypress is deliberate — repeaters penalize (and
        can blacklist) nodes that burst traffic — so repeat sampling is left to the
        human, and the screen aggregates whatever lands.
        """
        path = trace_runner.parse_trace_path(path_spec, contacts) if path_spec.strip() else None
        params: dict[str, Any] = {"target": target}
        if path:
            params["path"] = path
        run_id = ctx.repo.start_run("trace", params, ctx.profile_name)
        try:
            result = await device.run_trace(target, path=path)
        except BaseException as exc:
            # A cancelled or failed trace still closes its run row, so no ``running``
            # orphan is left behind.
            ctx.repo.finish_run(run_id, "error", {"error": str(exc) or type(exc).__name__})
            raise
        ctx.repo.record_trace(run_id, result)
        on_trace(result)
        ctx.repo.finish_run(
            run_id,
            "ok",
            {
                "target": target,
                "success": result.success,
                "min_snr": result.min_snr,
                "rtt_ms": result.round_trip_ms,
            },
        )

    screen = TraceScreen(
        target,
        device_label=device_label,
        device_hash=device_hash,
        resolve=resolve,
        session=session,
        trace=trace_once,
        compose_path=compose,
        explore=explore,
        pick_width=pick_width,
        width_bytes=lambda: width_bytes,
        previous=ctx.repo.latest_trace(target),
    )
    try:
        await session.run_screen(screen)
    finally:
        screen.cancel()
    return len(screen._traces)
