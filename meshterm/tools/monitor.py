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

from ..context import AppContext
from ..core import exitcodes
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
        from ..ui import script

        seconds = int(params.get("seconds") or 0)
        await ctx.device()  # surface connection problems before announcing the capture
        await ctx.monitor.start()  # register history recording before the hub pumps
        await ctx.events.start()
        # The announcement is about the run, not part of its answer, so it goes to stderr
        # and stays out of `meshterm monitor -s 60 > packets.txt`.
        script.stderr_console().print(
            "monitoring" + (f" for {seconds}s" if seconds else " — press Ctrl-C to stop"),
            style="muted",
            highlight=False,
        )
        seen: list[Observation] = []
        # One header for the live stream, then a record per packet as it arrives. The
        # stream cannot be column-aligned — the widths are not known until it ends — so
        # the fields are separated by the gutter and the name carries its quotes, which is
        # what keeps the line splittable.
        ctx.console.print("TIME  NODE  NAME  SNR_DB  RSSI_DBM  LAT  LON", highlight=False)

        def on_observation(event: MeshEvent) -> None:
            obs = event.observation
            if obs is None:
                return
            seen.append(obs)
            fields = [
                script.stamp(obs.observed_at),
                obs.node or script.NONE,
                script.name(obs.name),
                script.number(obs.snr, "+.1f"),
                script.number(obs.rssi, ".0f"),
                script.number(obs.lat, ".5f"),
                script.number(obs.lon, ".5f"),
            ]
            ctx.console.print((" " * script.GUTTER).join(fields), highlight=False)

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
            ctx.ui.show(script.blank())
            ctx.ui.show(_heard_table(nodes))
        return ToolResult(
            summary={"seconds": seconds, "packets": len(seen), "nodes": len(nodes)},
            exit_code=exitcodes.OK if seen else exitcodes.NO_RESULT,
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
    """The capture window's per-node summary: what each node did over the whole window.

    The aggregate the live stream cannot give — a count, a median, a best — one record per
    node heard, most recently heard first.

    Args:
        heard: Aggregated per-node statistics for the window.

    Returns:
        The scripted table (see :func:`meshterm.ui.script.columns`).
    """
    from ..ui import script

    table = script.columns(
        "NODE",
        "NAME",
        "PKTS",
        "MEDIAN_SNR_DB",
        "BEST_SNR_DB",
        "RSSI_DBM",
        "LAST_HEARD",
        right=("PKTS", "MEDIAN_SNR_DB", "BEST_SNR_DB", "RSSI_DBM"),
    )
    for node in heard:
        table.add_row(
            node.node or script.NONE,
            script.name(node.name),
            str(node.count),
            script.number(node.median_snr, "+.1f"),
            script.number(node.best_snr, "+.1f"),
            script.number(node.last_rssi, ".0f"),
            script.stamp(node.last_seen),
        )
    return table
