"""The live TX-optimization screen: watch a remote node's power sweep land level by level.

This is the interactive face of the ``tx-optimize`` tool (the scripted CLI keeps its
one-shot table and HTML chart). Where the old flow front-loaded six prompts — including
"apply the winner?" before anything had been measured — this screen follows the app's
living-screen pattern: one prompt picks the path, the sweep starts immediately, and every
measured level lands in a bar chart as it completes, phase by phase (coarse → refine →
verify), with the current best starred. The apply decision moves to *after* the evidence
is in: when the sweep finishes, a floating dialog offers the winner (Cancel left, Apply
right, Enter commits — the platform-dialog convention), and ``a`` re-offers it later if
the user first backs out of the dialog.

Backing out mid-sweep is safe: the optimizer's unwind restores the admin node's original
transmit power, and every level already measured is persisted under the sweep's ``runs``
row exactly as a scripted run records it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Optional

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..core.models import Contact, TxLevelResult, TxOptResult
from ..services import tx_optimizer
from .theme import snr_style
from .trace_screen import snr_bar
from .tui.render import render_lines
from .tui.screen import Screen
from .tui.spinner import Spinner

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between spinner frames while the sweep is measuring.
_SPINNER_INTERVAL = 0.12

#: Human phrasing for each optimizer phase reported through ``on_phase``.
_PHASE_LABELS = {
    "coarse": "coarse sweep",
    "refine": "refining around the leader",
    "verify": "verifying the leader",
}


class TxSweepScreen(Screen):
    """A full-screen live view of one remote-admin TX-power sweep.

    The screen is fed by the optimizer's callbacks (each measured level, each phase
    change) and by the controller marking completion/failure/apply; it renders state and
    handles keys only. Esc backs out at any time — mid-sweep that cancels the search and
    restores the node's original power; after completion, ``a`` (re-)offers the apply
    dialog.
    """

    floating = False

    def __init__(
        self,
        *,
        admin_label: str,
        target_label: str,
        path: str,
        tx_min: int,
        tx_max: int,
        step: int,
        samples: int,
        session: Any,
        offer_apply: Callable[[], None],
    ) -> None:
        """Create the sweep screen (the controller starts the sweep alongside it).

        Args:
            admin_label: Display name of the node being tuned.
            target_label: Display name of the node the SNR is measured at.
            path: The forced one-way path (comma-separated hashes) out to the target.
            tx_min: Lowest TX power the sweep explores.
            tx_max: Highest TX power the sweep explores.
            step: Coarse-sweep grid spacing.
            samples: Traces measured per level.
            session: The running TUI session (for repaints).
            offer_apply: Re-opens the apply dialog (bound by the controller); wired to
                the ``a`` key once a completed sweep has a winner that wasn't applied.
        """
        super().__init__()
        self.title = f"tx optimize · {admin_label} → {target_label}"
        self._admin_label = admin_label
        self._target_label = target_label
        self._path = path
        self._tx_min = tx_min
        self._tx_max = tx_max
        self._step = step
        self._samples = samples
        self._session = session
        self._offer_apply = offer_apply
        self._spinner = Spinner()
        self._phase: Optional[str] = None
        self._done = 0
        self._total = 0
        self._levels: dict[int, TxLevelResult] = {}
        self._best_tx: Optional[int] = None
        self._result: Optional[TxOptResult] = None
        self._applied = False
        self._error: Optional[str] = None

    # --- controller feed --------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the sweep is still measuring."""
        return self._result is None and self._error is None

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, tracking sweep state."""
        if self.running:
            return "measuring… · ↑↓ scroll · Esc stop & restore"
        if self._can_apply():
            return "a apply winner · ↑↓ scroll · Esc back"
        return "↑↓ scroll · Esc back"

    def tick(self) -> None:
        """Advance the measuring spinner (driven by the controller's animation timer)."""
        self._spinner.tick()

    def on_phase(self, phase: str) -> None:
        """Record the optimizer entering a search phase (see ``tx_optimizer.PHASES``)."""
        self._phase = phase
        self._session.invalidate()

    def on_level(self, done: int, total: int, level: TxLevelResult) -> None:
        """Record one measured level (replacing any earlier pass at the same power)."""
        self._done, self._total = done, total
        self._levels[level.tx_power] = level
        self._best_tx = tx_optimizer.select_best(list(self._levels.values())).tx_power
        self._session.invalidate()

    def complete(self, result: TxOptResult) -> None:
        """Mark the sweep finished and adopt its final selection."""
        self._result = result
        self._best_tx = result.best_tx
        self._session.invalidate()

    def fail(self, error: str) -> None:
        """Mark the sweep failed, keeping whatever levels already landed on screen."""
        self._error = error
        self._session.invalidate()

    def mark_applied(self) -> None:
        """Record that the winner was written to the admin node."""
        self._applied = True
        self._session.invalidate()

    def _can_apply(self) -> bool:
        """Whether ``a`` should offer the winner (finished, got a result, not yet set)."""
        return (
            self._result is not None
            and self._result.best_snr is not None
            and not self._applied
        )

    # --- input --------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Offer the apply dialog, scroll, or dismiss."""
        if action == "text" and data.lower() == "a":
            if self._can_apply():
                self._offer_apply()
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
            self.resolve(None)

    # --- rendering ------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the link header, phase line, level chart, and outcome."""
        lines = render_lines(Group(*self._sections()), width)
        self._scroll_total = max(1, len(lines))
        return lines

    def _sections(self) -> list[RenderableType]:
        """Assemble the screen's stacked sections for the current state."""
        sections: list[RenderableType] = [self._header(), Text(), self._phase_line()]
        if self._levels:
            sections += [Text(), self._levels_table()]
        outcome = self._outcome()
        if outcome is not None:
            sections += [Text(), outcome]
        return sections

    def _header(self) -> Text:
        """The tuned link and the sweep's fixed parameters, label-aligned."""
        header = Text.assemble(
            ("tuning     ", "muted"), (self._admin_label, "brand"),
            (" → ", "muted"), (self._target_label, "brand"), ("\n", ""),
            ("via        ", "muted"), (self._path, ""), ("\n", ""),
            ("sweep      ", "muted"),
            (f"TX {self._tx_min}–{self._tx_max} · step {self._step} · "
             f"{self._samples} trace{'s' if self._samples != 1 else ''}/level", ""),
        )
        if self._result is not None and self._result.original_tx is not None:
            header.append("\n")
            header.append("was        ", style="muted")
            header.append(f"TX {self._result.original_tx}")
        return header

    def _phase_line(self) -> Text:
        """The live phase/progress line, or the terminal state once the sweep ends."""
        if self._error is not None:
            return Text(f"✗ sweep failed: {self._error}", style="err")
        if self._result is not None:
            return Text("✓ sweep complete", style="ok")
        line = self._spinner.text()
        label = _PHASE_LABELS.get(self._phase or "", "starting…")
        line.append(f"  {label}", style="muted")
        if self._total:
            line.append(f" · level {self._done}/{self._total}", style="muted")
        return line

    def _levels_table(self) -> Table:
        """Every measured level as a bar chart, ascending by TX, the best starred."""
        table = Table(box=None, padding=(0, 1, 0, 0), expand=False, header_style="muted")
        table.add_column("TX", justify="right", min_width=4)
        table.add_column("SNR AT TARGET")
        table.add_column("", justify="right")  # numeric SNR
        table.add_column("RELIABILITY", justify="right")
        table.add_column("TRACES", justify="right", style="muted")
        for level in sorted(self._levels.values(), key=lambda lv: lv.tx_power):
            is_best = level.tx_power == self._best_tx
            tx_cell = Text()
            tx_cell.append("★ " if is_best else "  ", style="ok")
            tx_cell.append(str(level.tx_power), style="brand" if is_best else "")
            snr = level.target_snr
            if snr is None:
                bar_cell: Text = Text("✗ no reply", style="err")
                snr_cell = Text("—", style="muted")
            else:
                bar_cell = snr_bar(snr)
                snr_cell = Text(f"{snr:+.1f} dB", style=snr_style(snr))
            rate = level.success_rate
            rate_style = "ok" if rate >= 1.0 else ("warn" if rate > 0 else "err")
            table.add_row(
                tx_cell,
                bar_cell,
                snr_cell,
                Text(f"{rate:.0%}", style=rate_style),
                f"{level.successes}/{level.samples}",
            )
        return table

    def _outcome(self) -> Optional[Text]:
        """The completed sweep's verdict and apply status, or ``None`` while measuring."""
        result = self._result
        if result is None:
            return None
        if result.best_snr is None:
            return Text.assemble(
                ("! ", "warn"),
                ("no traces reached ", ""),
                (self._target_label, "brand"),
                (" at any level — check the path ends at the target and is reachable.", ""),
            )
        snr_text = Text(f"{result.best_snr:+.1f} dB", style=snr_style(result.best_snr))
        outcome = Text.assemble(
            ("best       ", "muted"), (f"TX {result.best_tx}", "brand"), ("  ·  ", "muted"),
        )
        outcome.append_text(snr_text)
        outcome.append(f" at {self._target_label}  ·  ", style="muted")
        outcome.append(f"{result.best_success_rate:.0%} reliable")
        outcome.append("\n")
        outcome.append("status     ", style="muted")
        if self._applied:
            outcome.append(f"✓ TX {result.best_tx} set on {self._admin_label}", style="ok")
        else:
            restored = (
                f" (node left at TX {result.original_tx})"
                if result.original_tx is not None
                else ""
            )
            outcome.append(f"not applied{restored} — press a to apply", style="muted")
        return outcome


async def open_tx_optimize(
    ctx: "AppContext",
    *,
    admin_node: Contact,
    target_label: str,
    path: str,
) -> dict[str, Any]:
    """Run one live TX-power sweep for an already-authenticated admin node.

    The caller (the ``tx-optimize`` tool) has resolved the path and logged in to
    ``admin_node``; this opens the sweep screen, drives the optimizer against it with
    ``apply=False`` (the node is restored to its original power when the search ends or
    is cancelled), persists every level and trace under one ``runs`` row, and raises the
    apply dialog once the evidence is in. Cancelling mid-sweep (Esc) stops the search;
    the optimizer's unwind restores the original power before the screen returns.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        admin_node: The contact being tuned (already logged in).
        target_label: Display name of the node the SNR is measured at.
        path: The forced one-way path (comma-separated hashes) ending at the target.

    Returns:
        The summary dict recorded on the sweep's run row (empty if cancelled before any
        level landed).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the live TX sweep is only available in the menu")
    session = ctx.ui.session
    device = await ctx.device()

    tx_min = ctx.settings.tx_opt_min
    tx_max = ctx.settings.tx_opt_max
    step = 3
    samples = 3
    summary: dict[str, Any] = {}

    def offer_apply() -> None:
        asyncio.ensure_future(apply_dialog())

    screen = TxSweepScreen(
        admin_label=admin_node.name,
        target_label=target_label,
        path=path,
        tx_min=tx_min,
        tx_max=tx_max,
        step=step,
        samples=samples,
        session=session,
        offer_apply=offer_apply,
    )

    run_id = ctx.repo.start_run(
        "tx-optimize",
        {"path": path, "samples": samples, "step": step, "tx_min": tx_min, "tx_max": tx_max},
        ctx.profile_name,
    )

    async def apply_dialog() -> None:
        """Offer the winner in a floating dialog; write it to the node on Apply."""
        result = screen._result
        if result is None or result.best_snr is None or screen._applied:
            return
        was = f" (currently {result.original_tx})" if result.original_tx is not None else ""
        prompt = Text.assemble(
            ("Set TX ", ""), (str(result.best_tx), "brand"),
            (f" on {admin_node.name}?{was}", ""),
        )
        # Platform-dialog convention: the safe way out left, the committing action right
        # and default, so Enter applies and Esc backs out.
        choice = await session.button_dialog(
            prompt,
            [("Cancel", "cancel"), ("Apply", "apply")],
            title="Apply winner",
            default=1,
            footer_hint="Esc cancel · Enter apply",
        )
        if choice != "apply":
            return
        await device.set_remote_tx_power(admin_node, result.best_tx)
        ctx.log.info("set TX power %s on %s", result.best_tx, admin_node.name)
        screen.mark_applied()
        summary["applied"] = True
        ctx.repo.finish_run(run_id, "ok", summary)

    async def sweep() -> None:
        """Drive the optimizer to completion, then raise the apply dialog."""
        ticker = asyncio.ensure_future(_animate(session, screen))
        try:
            result = await tx_optimizer.optimize_tx_power(
                device,
                target_label,
                admin_node,
                path,
                tx_min=tx_min,
                tx_max=tx_max,
                coarse_step=step,
                samples_per_level=samples,
                apply=False,  # the decision moves to the dialog, after the evidence is in
                cooldown_s=ctx.settings.trace_cooldown_s,
                on_level=screen.on_level,
                on_phase=screen.on_phase,
                persist_level=lambda lv: ctx.repo.record_tx_sample(run_id, lv),
                persist_trace=lambda t: ctx.repo.record_trace(run_id, t),
            )
        except asyncio.CancelledError:
            ctx.repo.finish_run(run_id, "error", {"error": "cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 - shown on the screen, not crashed through
            screen.fail(str(exc))
            ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            return
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break the sweep
                pass
        summary.update(
            {
                "target": result.target,
                "admin_node": result.admin_node,
                "path": result.path,
                "best_tx": result.best_tx,
                "best_snr": result.best_snr,
                "best_success_rate": round(result.best_success_rate, 3),
                "original_tx": result.original_tx,
                "applied": False,
                "levels_measured": len(result.levels),
            }
        )
        ctx.repo.finish_run(run_id, "ok", summary)
        screen.complete(result)
        await apply_dialog()

    worker = asyncio.ensure_future(sweep())
    try:
        await session.run_screen(screen)
    finally:
        if not worker.done():
            # Esc mid-sweep: stop the search and let the optimizer's unwind restore the
            # node's original power. The ring spinner covers that last device command.
            worker.cancel()
            async with ctx.ui.busy_overlay():
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - the sweep is over; nothing to surface
                    pass
    return summary


async def _animate(session: Any, screen: TxSweepScreen) -> None:
    """Advance the measuring spinner and repaint on a steady cadence, until cancelled."""
    while True:
        await asyncio.sleep(_SPINNER_INTERVAL)
        screen.tick()
        session.invalidate()
