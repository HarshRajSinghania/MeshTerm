"""The live trace screens: compose a route from observed topology, then watch it answer.

This is the interactive face of the two trace tools (the scripted CLI keeps its one-shot
table output). A trace is one walked path — the protocol has no destination field, so
"target" is purely a UX notion — and the two menu features split along exactly that line:

* **Trace target** (:func:`open_trace`) answers *can I reach this node?* The route is
  symmetric: compose (or explore) the outbound leg, and the return is those hops
  mirrored around the pinned target.
* **Trace path** (:func:`open_trace_path`) answers *how far can a route I build carry?*
  There is no target at all: the whole walk is composed by hand, out and back by
  whatever way you choose, and it only has to end within our own earshot.

Both open the same screen, *armed but idle*: the last-known route shows, the path is
whatever you make it, and nothing transmits until you say so. An action list drives it —
↑/↓ select a row, Enter commits it:

* **Compose path** opens the path composer (:mod:`~meshterm.ui.path_composer`): build
  the route hop by hop, each step suggested from the links observed in *received*
  traffic — traces, firmware-learned contact routes, and RX-logged packet paths —
  strongest first, with raw hex entry for nodes the data has never seen. Standing on a
  repeater you hold admin credentials for, the composer can also *fetch that repeater's
  neighbour table* over the mesh (login required; firmware ignores guests):
  second-vantage evidence, persisted and folded straight back into the suggestions.
* **Explore paths** (Trace target only — candidates need a destination) explores
  scenarios: ranked candidate routes to the target straight from the topology evidence
  (the device's own learned route, the direct shot, and the strongest observed
  alternatives). Adopt one directly — or probe them all, one measured trace per
  candidate, persisted as ``path_candidates`` rows and ranked reliability-first, with
  the winner offered for adoption. Evidence proposes, measurement decides, you dispose.
* **Path width** floats a small dialog picking the per-hop path-hash width (1, 2, 4,
  or 8 bytes — the leading key slice forced hops are addressed by). It seeds from the
  device's routing width; changing it re-renders a standing forced path at the new
  width and shapes every spec the composer and explorer emit after it.
* **Sample count** floats a dialog picking how many traces one Trace action runs
  (1, 2, 3, 5, or 8). Multi-trace runs are *paced* — a cooldown sleeps between
  transmissions, because repeaters penalize (and can blacklist) nodes that burst
  traffic — and the in-flight dialog counts them off as replies stream in.
* **Trace** (the cursor opens here, so plain Enter still just traces) runs the chosen
  number of traces. While they fly, a floating *tracing* dialog (spinner, progress,
  Abort) sits over the screen — replies stream into the log behind it, and Esc in the
  dialog cancels without leaving the screen; traces already recorded are kept. The
  screen keeps aggregating every trace of the session into its medians.
* **Back** leaves the screen, exactly like Esc.

Layout, top to bottom: the walked route (live when a reply has landed, else the route
the next Trace will walk — composed by hand, or auto-resolved from the device's learned
route or the stored history and labelled with that provenance — else the most recent
stored trace), the run's robust aggregates, the
action list, per-hop median SNR with quality bars, and the individual traces
newest-first. The body scrolls with PgUp/PgDn/Home/End (↑/↓ belong to the action
cursor, which only pins the view while it is actually being moved).

Every trace is persisted exactly like a scripted run: one ``runs`` row per trace,
recorded under it, so the stored history reads the same no matter which front end
produced it. Path walks record under :data:`~meshterm.core.models.PATH_TRACE_TARGET`,
keeping them out of the target picker's history while still seeding the path screen's
previous-route line.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..core.models import PATH_TRACE_TARGET, TraceResult, TraceStats
from ..services import trace_runner
from ..services.topology import render_forced_spec
from .braillechart import meter
from .menus import back_rows
from .theme import snr_style
from .tui.render import render_lines, render_to_ansi
from .tui.screen import Screen
from .tui.spinner import Spinner
from .widgets import NodeResolver, _link_text, _route_text, highlighted_hash

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between spinner frames while a trace or probe is in flight.
_SPINNER_INTERVAL = 0.12

#: How wide the per-hop SNR quality bars draw, in characters. Each character packs
#: two fill steps (see :func:`snr_bar`), so the bar reads at 16-step resolution in
#: half the columns a one-step-per-cell block bar would need.
_BAR_WIDTH = 8

#: The SNR range the bars span, in dB: -15 (barely readable) to +10 (excellent). Values
#: outside clamp to the ends, so the bar always shows *something* for a heard hop.
_BAR_SNR_MIN = -15.0
_BAR_SNR_MAX = 10.0

#: The sample counts the Sample count dialog offers: how many traces one Trace action
#: runs, paced between transmissions.
SAMPLE_CHOICES = (1, 2, 3, 5, 8)

#: A single-trace runner: ``(path_spec, on_trace)`` → runs exactly one trace, handing
#: the result to ``on_trace`` when it lands. Provided by the session openers, which close
#: over the device, repository, and settings so the screen stays free of persistence
#: concerns.
TraceOnce = Callable[[str, Callable[[TraceResult], None]], Awaitable[None]]

#: A path picker flow: takes the current spec, runs its own dialogs over the screen, and
#: resolves to the new spec (``""`` = device-routed) or ``None`` to keep the current one.
#: The sample-count flow shares the shape and simply always resolves ``None``.
PathFlow = Callable[[str], Awaitable[Optional[str]]]


def snr_bar(snr: Optional[float], width: int = _BAR_WIDTH) -> Text:
    """Render an SNR reading as a horizontal quality bar in the shared SNR colours.

    The slim-on-a-track flavour of the app's braille :func:`~meshterm.ui.braillechart.meter`:
    the reading maps onto :data:`_BAR_SNR_MIN` → :data:`_BAR_SNR_MAX` and fills a dark
    same-glyph track at two steps per cell.

    Args:
        snr: The reading in dB, or ``None`` (renders as an entirely unlit track).
        width: Bar track width in characters (each worth two fill steps).

    Returns:
        A :class:`Text` of filled braille cells over a dark, same-glyph track,
        coloured by :func:`~meshterm.ui.theme.snr_style`.
    """
    if snr is None:
        return meter(None, width, style="track", slim=True, track="track")
    span = _BAR_SNR_MAX - _BAR_SNR_MIN
    frac = min(1.0, max(0.0, (snr - _BAR_SNR_MIN) / span))
    return meter(frac, width, style=snr_style(snr), slim=True, track="track")


class TracingDialog(Screen):
    """The floating in-flight dialog: a spinner chip, live progress, and Abort.

    Pushed over the trace screen while traces (or a scenario probe) transmit, so the
    activity — and the way out — is unmissable while replies keep streaming into the
    screen behind it. Enter, Esc, or Space aborts via the injected callback; the owner
    pops the dialog when the work finishes, so it never resolves a value of its own.
    """

    footer_hint = "Enter/Esc abort"

    def __init__(self, title: str, *, spinner: Spinner, on_abort: Callable[[], None]) -> None:
        """Build the dialog.

        Args:
            title: Heading for the dialog border (e.g. ``Tracing — YUL-Poly``).
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
                  len("  Abort  ")]
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
        """Render the spinner chip, the last reply, and the Abort button.

        The button is the app's one button look — a centered reverse-video chip in the
        shared ``selected`` fill, exactly as the ButtonDialog and reconnect dialog draw
        theirs — so every popup's committing control reads the same.
        """
        from .tui.prompt import _center

        chip = self._spinner.text()
        chip.append(f"  {self.status}", style="")
        lines = render_lines(chip, width)
        if self.show_last:
            lines.extend(render_lines(self._last_line(), width))
        lines.append("")
        button = Text("  Abort  ", style="selected")
        lines.append(render_to_ansi(_center(button, width), width))
        return lines

    def handle(self, action: str, data: str = "") -> None:
        """Any commit/dismiss key aborts the in-flight work; everything else is inert."""
        if action in ("enter", "escape", "space"):
            self.on_abort()


class TraceScreen(Screen):
    """A full-screen live trace session — armed, but idle until told.

    One screen serves both trace features; ``mode`` decides which. ``"target"`` traces
    a pinned destination over a symmetric (mirrored) route and offers *Explore paths*;
    ``"path"`` walks a hand-composed route with no target at all — no Explore (ranked
    candidates need a destination), no device routing, and Trace stays inert until a
    path exists to walk (composed here, or auto-resolved from the last stored walk).

    Adopting a different path — composed, explored, or re-rendered at a new width —
    restarts the measurement: the aggregates, per-hop medians, and trace log all
    described the old route, so they clear as if the screen had just opened.

    ↑/↓ move the cursor over the action rows and Enter commits the selected one — the
    cursor opens on Trace, so plain Enter still just traces. PgUp/PgDn/Home/End scroll
    the body, and Esc (or the Back row) backs out, cancelling any in-flight trace;
    already-recorded traces are kept.
    """

    floating = False

    def __init__(
        self,
        target: str,
        *,
        mode: str = "target",
        device_label: str,
        device_hash: Optional[str],
        resolve: NodeResolver,
        session: Any,
        trace: TraceOnce,
        compose_path: PathFlow,
        pick_width: PathFlow,
        pick_samples: PathFlow,
        width_bytes: Callable[[], int],
        sample_count: Callable[[], int],
        explore: Optional[PathFlow] = None,
        pace_s: float = 1.0,
        previous: Optional[TraceResult] = None,
        auto_spec: Callable[[], str] = lambda: "",
        auto_source: str = "",
    ) -> None:
        """Create the screen (nothing transmits until the user commits Trace).

        Args:
            target: The label traces persist under — the destination's name in target
                mode, :data:`~meshterm.core.models.PATH_TRACE_TARGET` in path mode.
            mode: ``"target"`` (symmetric, explorable) or ``"path"`` (hand-composed
                walk, no destination).
            device_label: Our own node's name, labelling the route's endpoints.
            device_hash: Our own public key, so the endpoints carry a hash like every hop.
            resolve: Maps a hop's raw hash to a friendly contact name when known.
            session: The running TUI session (for repaints and the floating dialog).
            trace: Runs exactly one trace, handing back the result (see
                :data:`TraceOnce`); the screen loops it for multi-sample runs.
            compose_path: Opens the hop-by-hop path composer over this screen, seeded
                with the current spec; resolves to the new spec or ``None`` if cancelled.
            pick_width: Floats the path-hash width dialog; resolves to the standing
                spec re-rendered at the chosen width, or ``None`` when nothing changes.
            pick_samples: Floats the sample-count dialog; always resolves ``None``
                (the count lives with the owner, read back via ``sample_count``).
            width_bytes: Reads the currently chosen per-hop width, for the action row's
                label (the owner holds the width, since the flows emit specs at it).
            sample_count: Reads the currently chosen sample count, for the action rows
                and for how many traces one Trace commit runs.
            explore: Opens the scenario browser/probe flow over this screen (target
                mode only); resolves to an adopted spec or ``None`` to keep the current.
            pace_s: Cooldown slept between the traces of a multi-sample run, so a
                sampling session never reads as a burst to the repeaters.
            previous: The most recent stored trace, if any — its route seeds the route
                line so the screen opens knowing the path history last saw.
            auto_spec: Renders the spec an *auto* trace (no composed path) actually
                walks right now — the session resolves it from the device's learned
                route or the stored history at the current width (in path mode, the
                last successful stored walk verbatim). ``""`` means a path-less
                trace (unaddressable target, or a path walk with no history).
            auto_source: Short provenance of the auto route (e.g. ``device route``,
                ``last trace · Jul 09 14:32``) for the route line and summary, so
                the screen never claims a route the radio wasn't given.
        """
        super().__init__()
        self.title = f"Trace — {target}" if mode == "target" else "Trace path"
        self._target = target
        self._mode = mode
        self._flight_label = target if mode == "target" else "path"
        self._device_label = device_label
        self._device_hash = device_hash
        self._resolve = resolve
        self._session = session
        self._trace_once = trace
        self._compose_path = compose_path
        self._explore = explore
        self._pick_width = pick_width
        self._pick_samples = pick_samples
        self._width_bytes = width_bytes
        self._sample_count = sample_count
        self._pace_s = pace_s
        self._previous = previous
        self._auto_spec = auto_spec
        self._auto_source = auto_source
        self._path_spec = ""
        #: Traces aggregated on screen — the current route's run. Adopting a
        #: different path clears it (old numbers describe the old route).
        self._traces: list[TraceResult] = []
        #: Every trace this screen ever ran, across path changes — the session
        #: count the owner reports, immune to the per-route clears above.
        self._total_traces = 0
        self._running = False
        self._dialog_open = False
        self._status = ""
        self._progress: Optional[tuple[int, int]] = None  # (current, total) mid-run
        self._spinner = Spinner()
        self._worker: Optional[asyncio.Task] = None
        self._flight: Optional[TracingDialog] = None
        # The action rows, in display order. Explore needs a destination to rank
        # candidates for, so path mode drops it; the cursor opens on Trace either way.
        actions = ["compose"] + (["explore"] if mode == "target" else [])
        actions += ["width", "samples", "trace", "back"]
        self._actions: tuple[str, ...] = tuple(actions)
        self._index = self._actions.index("trace")
        self._pin_cursor = False  # only pin the view while ↑/↓ are actually in use

    # --- state -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, tracking whether traces are in flight."""
        if self._running:
            if self._progress is not None and self._progress[1] > 1:
                done, total = self._progress
                return f"tracing {done}/{total}… · PgUp/PgDn scroll · Esc back"
            return "tracing… · PgUp/PgDn scroll · Esc back"
        return "↑↓ actions · Enter run · PgUp/PgDn scroll · Esc back"

    def start_trace(self) -> None:
        """Kick off a trace run in the background (no-op while one is already flying)."""
        if self._running:
            return
        self._running = True
        self._status = ""
        self._spinner.reset()
        self._worker = asyncio.ensure_future(self._run_trace())
        self._session.invalidate()

    async def _run_trace(self) -> None:
        """Drive one Trace commit — the chosen number of traces — under the dialog.

        Multi-sample runs are paced: :attr:`_pace_s` sleeps between transmissions so
        the repeaters never see a burst, and the dialog counts the run off as replies
        land. The dialog is pushed for the duration and popped however the run ends —
        completion, failure, or abort — and its Abort wires straight to :meth:`cancel`,
        so the cancellation path is the same whether Esc lands on the dialog or the
        screen. An aborted run keeps every trace already recorded.
        """
        total = max(1, int(self._sample_count()))
        dialog = TracingDialog(
            f"Tracing — {self._flight_label}", spinner=self._spinner, on_abort=self.cancel
        )
        self._flight = dialog
        self._session.push(dialog)
        ticker = asyncio.ensure_future(self._animate())
        try:
            for done in range(total):
                self._progress = (done + 1, total)
                if total > 1:
                    dialog.status = f"trace {done + 1}/{total} · transmitting…"
                    self._session.invalidate()
                await self._trace_once(self._path_spec, self._on_trace)
                if done + 1 < total and self._pace_s > 0:
                    dialog.status = f"trace {done + 1}/{total} landed · pacing…"
                    self._session.invalidate()
                    await asyncio.sleep(self._pace_s)
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
            self._progress = None
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
        self._total_traces += 1
        if self._flight is not None:
            self._flight.last = result
        self._session.invalidate()

    def cancel(self) -> None:
        """Cancel any in-flight trace run (already-recorded traces are kept)."""
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
            self._index = (self._index - 1) % len(self._actions)
            self._pin_cursor = True
        elif action == "down":
            self._index = (self._index + 1) % len(self._actions)
            self._pin_cursor = True
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
        key = self._actions[self._index]
        if key == "trace":
            # A path walk has nothing to transmit until a path exists (composed,
            # or the last stored walk) — with no target, an empty spec can't fall
            # back to device routing.
            if self._mode == "path" and not self._effective_spec()[0]:
                return
            self.start_trace()
        elif key == "width":
            self._open_flow(self._pick_width)
        elif key == "samples":
            self._open_flow(self._pick_samples)
        elif key == "compose":
            self._open_flow(self._compose_path)
        elif key == "explore" and self._explore is not None:
            self._open_flow(self._explore)
        elif key == "back":
            self.cancel()
            self.resolve(None)

    def _open_flow(self, flow: PathFlow) -> None:
        """Float a path-picking flow over the screen (one at a time, not mid-trace).

        The composer, the scenario explorer, and the width picker all resolve the
        same way: a new spec to adopt (``""`` returns routing to the device), or
        ``None`` to leave the current path untouched (which is all the sample-count
        flow ever resolves).

        Args:
            flow: The dialog flow to run with the current spec.
        """
        if self._dialog_open or self._running:
            return
        self._dialog_open = True

        async def run() -> None:
            try:
                spec = await flow(self._path_spec)
                if spec is not None and spec.strip() != self._path_spec:
                    # A different spec is a different measurement: the aggregates,
                    # per-hop medians, and log all belong to the old route, so the
                    # session restarts as clean as a fresh screen.
                    self._path_spec = spec.strip()
                    self._traces.clear()
                    self._status = ""
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
            Group(self._route_line(current), Text(), self._summary(stats, current)), width
        )
        lines.append("")
        self._cursor: Optional[int] = None
        for i, key in enumerate(self._actions):
            if key == "back":
                lines.append("")  # Back is its own group, set apart like the menus do
            selected = i == self._index
            text = self._action_text(key, selected)
            text.no_wrap = True
            text.truncate(width, overflow="ellipsis")
            if selected:
                self._cursor = len(lines)
            lines.append(render_to_ansi(text, width))
            if key in ("explore", "samples"):
                lines.append("")  # set the next group apart
        tail: list[RenderableType] = []
        if stats.hop_snrs:
            hash_bytes = current.path_hash_bytes if current is not None else None
            tail += [Text(), Text("Per-hop medians", style="accent")]
            tail.append(self._hops_table(stats, hash_bytes))
        if self._running or self._status or self._traces:
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
        """One action row: pointer, glyph, and label (current value inlined)."""
        text = Text("❯ " if selected else "  ", style="brand" if selected else "")
        if key == "compose":
            text.append("✎ ", style="brand")
            text.append("Compose path")
        elif key == "explore":
            text.append("⚡ ", style="warn")
            text.append("Explore paths")
        elif key == "width":
            w = self._width_bytes()
            text.append("⚙ ", style="accent")
            text.append(f"Path width — {w} byte{'s' if w != 1 else ''} per hop")
        elif key == "samples":
            n = self._sample_count()
            text.append("# ", style="accent")
            text.append(f"Sample count — {n} trace{'s' if n != 1 else ''}")
        elif key == "trace":
            text.append("▶ ", style="ok")
            if self._mode == "path" and not self._effective_spec()[0]:
                text.append("Trace — compose a path first", style="muted")
            else:
                n = self._sample_count()
                if n > 1:
                    text.append(f"Trace — {n} paced transmissions")
                else:
                    text.append("Trace — one transmission")
        else:
            text.append("Back")
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
            # On its own line so a long route never squeezes the stamp off the
            # right edge.
            line.append(f"\n(previous · {stamp})", style="faint")
            return line
        if self._mode == "path":
            line.append("none — compose a path to walk", style="muted")
        else:
            line.append("unknown — press Enter to trace", style="muted")
        return line

    def _effective_spec(self) -> tuple[str, bool]:
        """The wire spec the next Trace walks, and whether auto resolution supplied it.

        A composed/adopted spec wins verbatim; with none, the session's auto
        resolver says what it would force right now (the device's learned route or
        the stored history in target mode, the last successful stored walk in path
        mode) — the same call :func:`_open_session`'s ``trace_once`` makes, so the
        route on screen is the route on the air.
        """
        if self._path_spec:
            return self._path_spec, False
        spec = self._auto_spec()
        return spec, bool(spec)

    def _planned_route(self) -> Optional[Text]:
        """The route the next Trace walks as a preview, or ``None`` without one.

        Renders the literal wire spec — the whole walk, since the trace protocol
        has no separate return-path field. In target mode the spec is the symmetric
        boomerang (outbound hops, the target, those hops mirrored): its second half
        is dimmed, reading as "this part isn't yours to compose". A path walk's spec
        is the whole route (hand-composed, or the last stored walk), so every hop
        renders in full colour and only the automatic landing back on us stays
        faint. An auto-resolved spec carries a faint provenance line naming where
        the route came from.
        """
        spec, auto = self._effective_spec()
        tokens = [h.strip() for h in spec.split(",") if h.strip()]
        if not tokens:
            return None
        if self._mode == "target" and len(tokens) % 2 and tokens == tokens[::-1]:
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
        if auto and self._auto_source:
            # On its own line (like the previous-trace stamp) so a long route
            # never squeezes the provenance off the right edge.
            text.append(f"\n(auto · {self._auto_source})", style="faint")
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

    def _displayed_hop_count(self, current: Optional[TraceResult]) -> Optional[int]:
        """How many nodes the displayed route passes through, endpoints excluded.

        Follows the route line's precedence — the live route, else the planned spec,
        else the stored previous trace — counting exactly the items drawn between the
        two ``us`` endpoints (a walked trace's final hash-less hop *is* us, so it
        doesn't count).
        """
        if current is not None:
            return sum(1 for h in current.hops if h.node)
        spec, _ = self._effective_spec()
        tokens = [h for h in spec.split(",") if h.strip()]
        if tokens:
            return len(tokens)
        if self._previous is not None and self._previous.hops:
            return sum(1 for h in self._previous.hops if h.node)
        return None

    def _summary(self, stats: TraceStats, current: Optional[TraceResult]) -> Text:
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
        elif self._effective_spec()[1] and self._auto_source:
            summary.append(f"auto · {self._auto_source}", style="muted")
        elif self._mode == "path":
            summary.append("none — compose a path first", style="muted")
        else:
            summary.append("auto — path-less (unknown target)", style="muted")
        hops = self._displayed_hop_count(current)
        if hops is not None:
            summary.append(f"  · {hops} hop{'s' if hops != 1 else ''}", style="muted")
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
            if self._progress is not None and self._progress[1] > 1:
                spin.append(f"  tracing {self._progress[0]}/{self._progress[1]}…",
                            style="muted")
            else:
                spin.append("  tracing…", style="muted")
            rows.append(spin)
        elif self._status:
            rows.append(Text(self._status, style="err"))
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


def _previous_outbound(
    previous: Optional[TraceResult], target_hash: str
) -> Optional[tuple[str, ...]]:
    """Extract the outbound repeaters from the last successful walk to a target.

    A target-mode trace walks the symmetric boomerang, so its stored hop hashes
    (the final hash-less hop is us) read ``[out…, target, out reversed…]`` — an
    odd-length palindrome whose middle entry is the target. When the stored walk
    has that shape, its first half is a route the mesh has already proven, ready
    to force again.

    Args:
        previous: The most recent successful stored trace, if any.
        target_hash: The target's full hex hash, to confirm the walk really
            turned at this target (hop hashes are prefixes of it).

    Returns:
        The outbound repeater hashes in order from us outward (empty = the
        target answered directly), or ``None`` when there is no stored walk or
        it isn't a recognizable boomerang.
    """
    if previous is None or not previous.success:
        return None
    tokens = [h.node.lower() for h in previous.hops if h.node]
    if not tokens or len(tokens) % 2 == 0 or tokens != tokens[::-1]:
        return None
    mid = len(tokens) // 2
    if not target_hash.lower().startswith(tokens[mid]):
        return None
    return tuple(tokens[:mid])


def _previous_walk(previous: Optional[TraceResult]) -> Optional[tuple[str, ...]]:
    """Extract the whole walked route from the last successful stored path walk.

    A path walk has no destination to route to, but its stored spec is a route the
    mesh has already carried end to end — so the previous walk the screen opens
    showing is also a path Trace can immediately walk again. The hop hashes come
    back verbatim (the final hash-less hop is us and drops out): they were proven
    at the width they were transmitted, so no re-rendering is applied.

    Args:
        previous: The most recent stored path walk, if any.

    Returns:
        The walked hop hashes in transmit order, or ``None`` when there is no
        stored walk, it failed, or it recorded no addressable hops.
    """
    if previous is None or not previous.success:
        return None
    tokens = tuple(h.node.lower() for h in previous.hops if h.node)
    return tokens or None


async def open_trace(ctx: "AppContext", target: str) -> int:
    """Open the live *Trace target* screen for ``target`` and run it until dismissed.

    The symmetric feature: can I reach this node? Routes turn at the target and come
    home over the mirrored hops; *Explore paths* ranks candidate outbound legs from
    the observed evidence.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        target: The trace destination (contact name or key prefix).

    Returns:
        The number of traces run while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    return await _open_session(ctx, target)


async def open_trace_path(ctx: "AppContext") -> int:
    """Open the live *Trace path* screen and run it until dismissed.

    The hand-routed feature: how far can a route I build carry? There is no target —
    the whole walk is composed hop by hop and only has to end within our earshot — so
    there is no target picker, no device routing, and no scenario explorer. Traces
    record under :data:`~meshterm.core.models.PATH_TRACE_TARGET`.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).

    Returns:
        The number of traces run while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    return await _open_session(ctx, None)


async def _open_session(ctx: "AppContext", target: Optional[str]) -> int:
    """Wire and run one live trace session (both features share this plumbing).

    Wires the screen to the radio, the database, and the observed-topology services:
    each trace opens its own ``runs`` row and is recorded under it (same shape a
    scripted ``meshterm trace`` writes); the composer and scenario flows build a fresh
    :class:`~meshterm.services.topology.MeshTopology` from stored evidence on each open,
    so suggestions always reflect the latest received traffic. Nothing transmits until
    the user asks — the screen opens idle, its route line seeded from the most recent
    stored trace.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        target: The trace destination (contact name or key prefix), or ``None`` for a
            target-less path walk.

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

    mode = "target" if target is not None else "path"
    record_target = target if target is not None else PATH_TRACE_TARGET

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

    # How many traces one Trace commit runs. Session state like the width; the
    # screen's *Sample count* action changes it (see pick_samples).
    sample_count = 1

    # The target as an addressable hash: a known contact's full key, or the typed hex
    # prefix itself. A non-hex unknown target can still be traced device-routed, but
    # composing/exploring needs a destination hash to pin the path on. A path walk has
    # no target at all — every target_* stays None and the composer runs hand-routed.
    target_contact: Optional[Contact] = None
    target_hash: Optional[str] = None
    target_label: Optional[str] = None
    if target is not None:
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

    previous = ctx.repo.latest_trace(record_target)

    # What an *auto* trace (no composed path) forces on the air, and where that
    # route came from. A trace only replies when its destination is the path's
    # final outbound hop and nothing reflects it home, so auto must always spell
    # out a full boomerang — but the device rarely has one to offer: firmware
    # only learns a contact's ``out_path`` from two-way addressed traffic
    # (verified on hardware: every repeater contact reported ``out_path_len``
    # -1, flood), so a device-routed trace to anything further than a direct
    # neighbour would go out with no repeaters and die. Precedence: the device's
    # learned route when it genuinely has one, else the outbound leg of the last
    # successful stored walk (the route the screen shows), else the bare
    # destination — a direct attempt, honest about being one.
    device_route: Optional[tuple[str, ...]] = None
    if target_contact is not None and target_contact.route_hops is not None:
        topo0 = fresh_topology()
        device_route = tuple(topo0.canonical(h) or h for h in target_contact.route_hops)
    auto_hops: Optional[tuple[str, ...]] = None
    auto_source = ""
    if target_hash is not None:
        if device_route is not None:
            auto_hops, auto_source = device_route, "device route"
        else:
            auto_hops = _previous_outbound(previous, target_hash)
            if auto_hops is not None and previous is not None:
                stamp = previous.timestamp.astimezone().strftime("%b %d %H:%M")
                auto_source = f"last trace · {stamp}"
            else:
                auto_hops, auto_source = (), "direct — no known route"
    elif mode == "path":
        auto_hops = _previous_walk(previous)
        if auto_hops is not None and previous is not None:
            stamp = previous.timestamp.astimezone().strftime("%b %d %H:%M")
            auto_source = f"last walk · {stamp}"

    def auto_spec() -> str:
        """The spec auto forces at the session's current width (``""`` = path-less).

        A path walk's auto spec is the stored hops verbatim — they were proven at
        the width they were transmitted, so no re-rendering is applied to them.
        """
        if auto_hops is None:
            return ""
        if target_hash is None:
            return ",".join(auto_hops)
        return render_forced_spec(auto_hops, target_hash, width_bytes)

    async def unaddressable() -> None:
        """Explain why target-mode path features need a resolvable target."""
        await session.message_dialog(
            Text(
                f"{target!r} isn't a known contact or a hex key prefix, so a forced "
                "path can't end at it. Trace it device-routed, or pick a contact.",
            ),
            title="No destination hash",
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
            f"Fetch neighbours — {repeater.name}", spinner=spinner, on_abort=lambda: None
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
                Text(str(error), style="err"), title="Fetch neighbours"
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
                title="Fetch neighbours",
            )
            return False
        return True

    async def compose(current: str) -> Optional[str]:
        """Open the hop-by-hop composer seeded with the current spec's hops.

        Runs the composer in a loop: a :class:`FetchNeighbours` resolution performs the
        fetch, rebuilds the topology with the new evidence, and reopens the composer
        exactly where the user stood (same hops, refreshed suggestions).
        """
        if mode == "target" and target_hash is None:
            await unaddressable()
            return None
        topo = fresh_topology()
        target_id = (
            (topo.canonical(target_hash) or target_hash[:12])
            if target_hash is not None
            else None
        )
        # Re-seed from the current spec. A target-mode spec is the symmetric boomerang
        # (hops, target, mirror): seed just the outbound hops and let the composer
        # regenerate the rest. A path walk is its spec verbatim. Tokens no contact
        # matches are kept as they are rather than dropped — an adopted route must
        # survive a reopen even where the evidence graph is blind.
        tokens = [p.strip() for p in current.split(",") if p.strip()]
        ids = [topo.canonical(h) or h for h in tokens]
        if mode == "path":
            seed = ids
        elif ids and len(ids) % 2 == 1 and ids == ids[::-1]:
            seed = ids[: len(ids) // 2]
        elif target_id in ids:
            seed = ids[: ids.index(target_id)]  # a stale hand walk: keep the outbound
        else:
            seed = ids
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
                device_label=device_label,
                device_hash=device_hash,
                topology=topo,
                width_bytes=width_bytes,
                target_id=target_id,
                target_hash=target_hash,
                target_label=target_label,
                hops=seed,
                fetch_nodes=frozenset(fetchable),
            )
            result = await session.run_screen(screen)
            if result is CANCEL:
                return None
            if isinstance(result, FetchNeighbours):
                seed = screen.hops  # resume mid-thought after the fetch
                repeater = fetchable.get(result.node)
                if repeater is not None and await fetch_neighbours_via(
                    repeater, result.node
                ):
                    topo = fresh_topology()  # fold the new reports into suggestions
                continue
            return result

    async def pick_width(current: str) -> Optional[str]:
        """Float the path-hash width picker and re-render the standing spec to match.

        Each row previews a key with the addressed slice lit at that width, so the
        choice reads as "this much of every key goes on the air". The chosen width
        shapes every spec the composer/explorer emit afterwards; a standing forced
        path is re-rendered immediately — hops are widened back through their
        canonical hashes where known, then collapsed uniformly (a hop only ever known
        narrower keeps the whole spec at what it can honour).

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
                title.append_text(highlighted_hash(sample, w, width=16))
            if w == device_width:
                title.append("  · device default", style="muted")
            items.append(Choice(title=title, value=w))
        picked = await session.run_screen(
            SelectScreen(
                "Path width",
                items,
                prompt="Forced hops are addressed by this many leading key bytes.",
                default=width_bytes,
                footer_hint="↑↓ move · Enter set · Esc keep",
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

    async def pick_samples(current: str) -> Optional[str]:
        """Float the sample-count picker: how many traces one Trace action runs.

        Multi-trace runs are paced by the configured cooldown between transmissions,
        so choosing 8 is a deliberate sampling session, never a burst. Always resolves
        ``None`` — the count is session state the screen reads back, not a spec.
        """
        nonlocal sample_count
        pace = ctx.settings.trace_cooldown_s
        items: list = []
        for n in SAMPLE_CHOICES:
            title = Text(f"{n} trace{'s' if n > 1 else ' '}")
            if n == 1:
                title.append("  · a single transmission", style="muted")
            items.append(Choice(title=title, value=n))
        picked = await session.run_screen(
            SelectScreen(
                "Sample count",
                items,
                prompt=f"One Trace action runs this many traces, {pace:g} s apart.",
                default=sample_count,
                footer_hint="↑↓ move · Enter set · Esc keep",
                filterable=False,
                wrap=False,
            )
        )
        if picked is not CANCEL and picked is not None:
            sample_count = int(picked)
        return None

    def scenario_title(scenario: PathScenario, topo: MeshTopology) -> Text:
        """One scenario as a select row: the route itself, and its observed evidence.

        Observed candidates *are* their hop sequence, so the row leads with the route
        and no label; the device route and the direct shot keep their short labels —
        that provenance is the point of offering them.
        """
        names = [topo.display_name(h) or h for h in scenario.hops]
        if scenario.source == "observed":
            text = Text(" → ".join(names), style="brand")
        else:
            styles = {"device": "accent", "direct": "muted"}
            text = Text(scenario.label, style=styles.get(scenario.source, ""))
            if names:
                text.append("  via ", style="muted")
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
        Each candidate gets exactly one transmission (probing is comparison, not
        sampling); ``None`` when aborted.
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
        dialog = TracingDialog(
            f"Probing — {target_label}", spinner=spinner, on_abort=lambda: None
        )
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
                Text(f"probe failed: {error}", style="err"), title="Path probe"
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
        scenarios = topo.scenarios(target_id, device_route=device_route)
        if not scenarios:
            await session.message_dialog(
                Text("no observed evidence involving this target yet — run a trace or "
                     "let monitoring accumulate paths first.", style="muted"),
                title="Explore paths",
            )
            return None

        items: list = [
            Separator("── Candidate paths · from received evidence ──", style="accent")
        ]
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
        items.extend(back_rows(("back", None)))
        picked = await session.run_screen(
            SelectScreen(
                f"Explore paths — {target_label}",
                items,
                prompt="Choose the outbound leg — the return mirrors it.",
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
        result_items.append(Choice(title="Keep current path", value=None))  # the exit: no adoption
        adopted = await session.run_screen(
            SelectScreen(
                f"Probe results — {target_label}",
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

        The screen calls this once per sample; multi-trace runs are paced there, so
        the repeaters never see a burst regardless of the chosen count.
        """
        spec = path_spec.strip() or auto_spec()
        path = trace_runner.parse_trace_path(spec, contacts) if spec else None
        params: dict[str, Any] = {"target": record_target}
        if path:
            params["path"] = path
        run_id = ctx.repo.start_run("trace", params, ctx.profile_name)
        try:
            result = await device.run_trace(record_target, path=path)
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
                "target": record_target,
                "success": result.success,
                "min_snr": result.min_snr,
                "rtt_ms": result.round_trip_ms,
            },
        )

    screen = TraceScreen(
        record_target,
        mode=mode,
        device_label=device_label,
        device_hash=device_hash,
        resolve=resolve,
        session=session,
        trace=trace_once,
        compose_path=compose,
        explore=explore if mode == "target" else None,
        pick_width=pick_width,
        pick_samples=pick_samples,
        width_bytes=lambda: width_bytes,
        sample_count=lambda: sample_count,
        pace_s=ctx.settings.trace_cooldown_s,
        previous=previous,
        auto_spec=auto_spec,
        auto_source=auto_source,
    )
    try:
        await session.run_screen(screen)
    finally:
        screen.cancel()
    return screen._total_traces
