"""The ``monitor`` tool: a bounded foreground capture of what the mesh is saying.

Passive monitoring records every advert and telemetry frame the companion overhears —
with SNR, RSSI, and any shared location — to the database, building the longitudinal
history that the map's packet counts and the nodes list read back. It transmits nothing;
it only listens.

Recording is always on: the session-wide
:class:`~meshterm.services.event_hub.EventHub` overhears every packet, and
:class:`~meshterm.services.monitor_service.MonitorService` (``ctx.monitor``) writes them
to history as one of its subscribers, from the moment the radio opens. In the menu the
live packet counters show in the persistent header and the accumulated data surfaces
through Nodes and Map, so there is no separate screen here — this tool is CLI-only:
``meshterm monitor --seconds 60`` tails each overheard packet to the console and
summarizes the window when it ends.
"""

from __future__ import annotations

import asyncio
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.events import EventKind, MeshEvent
from ..core.models import HeardNode, Observation
from .base import Tool, ToolResult, register


@register
class MonitorTool(Tool):
    """Capture overheard packets in the foreground for a bounded window (CLI only)."""

    name = "monitor"
    title = "Monitor"
    icon = "🎧"
    help = "Capture overheard packets live for a while and summarize them"
    category = "Watch"
    order = 50
    menu_visible = False  # recording is always on; in the menu the header/Nodes/Map show it

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run directly, without a logged ``runs`` row.

        The capture's observations are recorded under the monitor service's own
        ``monitor`` run, so wrapping this invocation in a second run row (as the base
        :meth:`Tool.execute` would) would double-log the session.

        Args:
            ctx: Shared application context.
            params: Parameters for this invocation.

        Returns:
            The :class:`ToolResult` from :meth:`run`.
        """
        return await self.run(ctx, params)

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Record overheard packets in the foreground for a bounded window.

        Connects the device, ensures history recording and the event hub are running,
        and tails each overheard packet to the console until the window ends (or the
        user interrupts). Observations are persisted by the monitor service exactly as
        in an interactive session; this adds a live console view and a window-scoped
        heard-node summary on top.

        Args:
            ctx: Shared application context.
            params: ``seconds`` — how long to capture (``0``/``None`` = until Ctrl-C).

        Returns:
            A :class:`ToolResult` with the window's packet and node counts.
        """
        seconds = int(params.get("seconds") or 0)
        await ctx.device()  # surface connection problems before announcing the capture
        await ctx.monitor.start()  # register history recording before the hub pumps
        await ctx.events.start()
        ctx.console.print(
            "[muted]monitoring"
            f"{f' for {seconds}s' if seconds else ' — press Ctrl-C to stop'}…[/muted]"
        )
        seen: list[Observation] = []

        def on_observation(event: MeshEvent) -> None:
            obs = event.observation
            if obs is None:
                return
            seen.append(obs)
            stamp = obs.observed_at.astimezone().strftime("%H:%M:%S")
            who = obs.name or obs.node or "?"
            snr = f" [muted]{obs.snr:+.1f} dB[/muted]" if obs.snr is not None else ""
            rssi = f" [muted]{obs.rssi:.0f} dBm[/muted]" if obs.rssi is not None else ""
            loc = " [ok]●[/ok]" if obs.lat is not None else ""
            ctx.console.print(
                f"[muted]{stamp}[/muted] [accent]{who}[/accent]{snr}{rssi}{loc}"
            )

        unsubscribe = ctx.events.subscribe(on_observation, EventKind.OBSERVATION)
        try:
            if seconds:
                await asyncio.sleep(seconds)
            else:
                await asyncio.Event().wait()  # until Ctrl-C / cancellation
        except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - interactive
            pass
        finally:
            unsubscribe()
            await ctx.monitor.stop()  # close the run row so the capture is a full record

        by_node: dict[str, list[Observation]] = {}
        for obs in seen:
            by_node.setdefault(obs.node, []).append(obs)
        nodes = [HeardNode.from_observations(node, group) for node, group in by_node.items()]
        if nodes:
            nodes.sort(key=lambda n: n.last_seen, reverse=True)
            ctx.ui.show(_heard_table(nodes))
        return ToolResult(
            summary={"seconds": seconds, "packets": len(seen), "nodes": len(nodes)},
            message=(
                f"[ok]✓[/ok] heard [brand]{len(seen)}[/brand] "
                f"packet{'' if len(seen) == 1 else 's'} from "
                f"[brand]{len(nodes)}[/brand] node{'' if len(nodes) == 1 else 's'}"
            ),
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``monitor`` subcommand (the bounded foreground capture).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _monitor(
            seconds: int = typer.Option(
                0, "--seconds", "-s", help="How long to capture (0 = until Ctrl-C)"
            ),
        ) -> None:
            run_tool_command(self, {"seconds": seconds})


def _heard_table(heard: list[HeardNode]) -> Table:
    """Render the capture window's heard-node summary table.

    Args:
        heard: Aggregated per-node statistics for the window.

    Returns:
        A Rich :class:`Table` of node, packet count, SNR, RSSI, and location.
    """
    from ..ui.theme import snr_style

    table = Table(title=f"Heard nodes ({len(heard)})", border_style="muted", expand=False)
    table.add_column("NODE")
    table.add_column("PKTS", justify="right")
    table.add_column("MEDIAN SNR", justify="right")
    table.add_column("BEST SNR", justify="right")
    table.add_column("RSSI", justify="right")
    table.add_column("LOC", justify="center")
    for node in heard:
        label = node.name or node.node or "?"
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
