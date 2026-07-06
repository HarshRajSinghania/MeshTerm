"""The ``monitor`` tool: control the passive background logger and review its history.

Passive monitoring records every advert and telemetry frame the companion overhears —
with SNR, RSSI, and any shared location — to the database, building the longitudinal
history that the coverage map and link-quality alerting read back. It transmits nothing;
it only listens.

Listening itself is always on: the session-wide
:class:`~meshtools.services.event_hub.EventHub` overhears every packet, and
:class:`~meshtools.services.monitor_service.MonitorService` (``ctx.monitor``) records
them to history as one of its subscribers. This tool is the control panel for that
recording: toggling it on/off (a preference remembered between sessions) and reviewing the
heard-node history. The live packet counters are shown in the main-menu header, not here.
"""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.models import HeardNode
from ..ui.tui import Choice
from .base import Tool, ToolResult, register


@register
class MonitorTool(Tool):
    """Toggle the passive background monitor and review the nodes it has heard."""

    name = "monitor"
    help = "Toggle passive background monitoring on/off and review heard nodes."
    category = "Diagnostics"
    order = 20

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Show the monitor status and offer to toggle it or review heard nodes.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict naming the chosen ``action``, or ``None`` if cancelled.
        """
        monitor = ctx.monitor
        toggle_label = "Turn monitoring OFF" if monitor.enabled else "Turn monitoring ON"
        choice = await ctx.ui.select(
            f"Passive monitor — {monitor.status_text()}",
            [
                Choice(toggle_label, value="toggle"),
                Choice("View heard nodes (all time)", value="view"),
                Choice("Back", value="__back__"),
            ],
        )
        if choice in (None, "__back__"):
            return None
        return {"action": choice}

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run the control-panel action directly, without a logged ``runs`` row.

        The monitor entry is a control panel, not a measurement, so it is deliberately
        not wrapped in run-logging (unlike the base :meth:`Tool.execute`). The background
        capture session records its own ``monitor`` run instead.

        Args:
            ctx: Shared application context.
            params: The chosen ``action``.

        Returns:
            The :class:`ToolResult` from :meth:`run`.
        """
        return await self.run(ctx, params)

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Dispatch the selected control-panel action.

        Args:
            ctx: Shared application context.
            params: ``action`` — ``"toggle"`` or ``"view"``.

        Returns:
            A :class:`ToolResult` describing the outcome.
        """
        if params.get("action") == "view":
            return self._view(ctx)
        return await self._toggle(ctx)

    @staticmethod
    async def _toggle(ctx: AppContext) -> ToolResult:
        """Flip background monitoring on or off, surfacing any start failure.

        Args:
            ctx: Shared application context.

        Returns:
            A :class:`ToolResult` reporting the new state.
        """
        try:
            now_on = await ctx.monitor.toggle()
        except Exception as exc:  # noqa: BLE001 - report cleanly, keep the menu alive
            return ToolResult(
                summary={"enabled": ctx.monitor.enabled, "active": ctx.monitor.active,
                         "error": str(exc)},
                message=f"[warn]monitoring enabled but capture couldn't start:[/warn] {exc}",
            )
        # No confirmation message: the new state is already shown live in the header's
        # monitor indicator, so a result window here would just be redundant noise.
        return ToolResult(summary={"enabled": now_on, "active": ctx.monitor.active})

    @staticmethod
    def _view(ctx: AppContext) -> ToolResult:
        """Render the all-time heard-node summary from stored observations.

        Reads only the database, so it needs no device connection and works whether or
        not monitoring is currently active.

        Args:
            ctx: Shared application context.

        Returns:
            A :class:`ToolResult` summarizing the heard nodes.
        """
        heard = ctx.repo.heard_nodes()
        if heard:
            # Names come from the stored observations, so no device lookup is needed.
            ctx.ui.show(_heard_table(heard, lambda _node: None))
        else:
            ctx.ui.note("[muted]no packets heard yet — turn monitoring on[/muted]")

        packets = sum(n.count for n in heard)
        located = sum(1 for n in heard if n.has_location)
        return ToolResult(
            summary={
                "nodes_heard": len(heard),
                "observations": packets,
                "nodes_with_location": located,
            },
            message=(
                f"[ok]✓[/ok] heard [brand]{packets}[/brand] packets from "
                f"[brand]{len(heard)}[/brand] nodes (all time)"
            ),
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``monitor`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..core.monitor_store import MonitorStore

        @app.command(name=self.name, help=self.help)
        def _monitor(
            on: bool = typer.Option(
                False, "--on", help="Enable background monitoring for future sessions."
            ),
            off: bool = typer.Option(
                False, "--off", help="Disable background monitoring."
            ),
        ) -> None:
            from ..cli import _state

            if on and off:
                raise typer.BadParameter("Pass only one of --on / --off.")
            assert _state is not None  # set by the callback before any subcommand runs
            ctx = _state
            store = MonitorStore(ctx.settings.config_dir / "monitor.json")

            if on or off:
                store.save_enabled(on)
                state = "[ok]on[/ok]" if on else "[muted]off[/muted]"
                ctx.console.print(
                    f"[ok]✓[/ok] background monitoring set {state} for future "
                    "interactive sessions"
                )
                return
            # No flags: report the current preference and history size.
            enabled = "[ok]on[/ok]" if store.load_enabled() else "[muted]off[/muted]"
            ctx.console.print(
                f"background monitoring is {enabled} · "
                f"[brand]{ctx.repo.observation_count()}[/brand] observations logged all-time"
            )


def _heard_table(heard: list[HeardNode], resolve) -> Table:  # noqa: ANN001
    """Render the heard-node summary table.

    Args:
        heard: Aggregated per-node statistics.
        resolve: Maps a raw node hash to a friendly contact name when known.

    Returns:
        A Rich :class:`Table` of node, packet count, SNR, RSSI, and location.
    """
    from ..ui.theme import snr_style

    table = Table(title=f"Heard nodes ({len(heard)})", border_style="muted", expand=False)
    table.add_column("Node")
    table.add_column("Pkts", justify="right")
    table.add_column("Median SNR", justify="right")
    table.add_column("Best SNR", justify="right")
    table.add_column("RSSI", justify="right")
    table.add_column("Loc", justify="center")
    for node in heard:
        label = node.name or resolve(node.node) or node.node or "?"
        median = node.median_snr
        best = node.best_snr
        median_cell = (
            Text(f"{median:+.1f}", style=snr_style(median)) if median is not None else Text("—")
        )
        best_cell = (
            Text(f"{best:+.1f}", style=snr_style(best)) if best is not None else Text("—")
        )
        rssi_cell = f"{node.last_rssi:.0f}" if node.last_rssi is not None else "—"
        loc_cell = Text("●", style="ok") if node.has_location else Text("·", style="muted")
        table.add_row(
            str(label), str(node.count), median_cell, best_cell, rssi_cell, loc_cell
        )
    return table
