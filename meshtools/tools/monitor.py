"""The ``monitor`` tool: passively log adverts/telemetry the mesh emits over time.

Standard MeshCore tooling is point-in-time. This tool sits on the companion radio and
records every advert and telemetry frame it overhears — with SNR, RSSI, and any shared
location — to the database, building the longitudinal history that the coverage map and
link-quality alerting read back. It transmits nothing; it only listens.
"""

from __future__ import annotations

from typing import Any, Optional

import questionary
import typer
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.models import HeardNode, Observation
from ..services import trace_runner
from ..ui.widgets import make_progress
from .base import Tool, ToolResult, register

#: Cap a single listening window so an interactive run can't block indefinitely; longer
#: passive logging is still possible by re-running or scripting the subcommand.
MAX_DURATION_S = 3600


@register
class MonitorTool(Tool):
    """Listen for a window and log every overheard advert/telemetry to the database."""

    name = "monitor"
    help = "Passively log adverts/telemetry heard on the mesh (longitudinal history)."
    category = "Diagnostics"
    order = 20

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Ask how long to listen.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict, or ``None`` if the user cancelled.
        """
        duration = await questionary.text(
            f"Listen for how many seconds? (1-{MAX_DURATION_S})",
            default="60",
            validate=_is_valid_duration,
        ).ask_async()
        if duration is None:
            return None
        return {"duration": int(duration)}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Capture observations for the window, persist them, and summarize heard nodes.

        Args:
            ctx: Shared application context.
            params: ``duration`` (seconds) and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` noting how many nodes/packets were heard.
        """
        duration = max(1, min(MAX_DURATION_S, int(params.get("duration", 60))))
        run_id = params["_run_id"]
        device = await ctx.device()

        # Resolve node hashes to friendly names where we know the contact.
        contacts = await device.get_contacts()
        resolve = trace_runner.make_node_resolver(contacts)

        observations: list[Observation] = []
        with make_progress(ctx.console) as progress:
            task = progress.add_task(f"listening {duration}s", total=duration)

            def on_observation(obs: Observation) -> None:
                observations.append(obs)
                ctx.repo.record_observation(run_id, obs)
                progress.update(
                    task,
                    description=f"heard {len(observations)} pkts · "
                    f"{_distinct(observations)} nodes",
                )

            await device.listen(duration, on_observation=on_observation)
            progress.update(task, completed=duration)

        heard = _aggregate(observations)
        if heard:
            ctx.console.print(_heard_table(heard, resolve))
        else:
            ctx.console.print("[muted]no packets heard during the window[/muted]")

        located = sum(1 for n in heard if n.has_location)
        return ToolResult(
            summary={
                "duration_s": duration,
                "observations": len(observations),
                "nodes_heard": len(heard),
                "nodes_with_location": located,
            },
            message=(
                f"[ok]✓[/ok] heard [brand]{len(observations)}[/brand] packets from "
                f"[brand]{len(heard)}[/brand] nodes in {duration}s"
            ),
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``monitor`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _monitor(
            duration: int = typer.Option(
                60, "--duration", "-d", help=f"Seconds to listen (max {MAX_DURATION_S})."
            ),
        ) -> None:
            run_tool_command(self, {"duration": duration})


def _distinct(observations: list[Observation]) -> int:
    """Return the number of distinct nodes among observations."""
    return len({o.node for o in observations})


def _aggregate(observations: list[Observation]) -> list[HeardNode]:
    """Group observations by node into :class:`HeardNode` records, freshest first.

    Args:
        observations: The observations captured this run.

    Returns:
        One aggregate per node, ordered by most-recently heard.
    """
    grouped: dict[Optional[str], list[Observation]] = {}
    for obs in observations:
        grouped.setdefault(obs.node, []).append(obs)
    nodes = [HeardNode.from_observations(node, obs) for node, obs in grouped.items()]
    return sorted(nodes, key=lambda n: n.last_seen, reverse=True)


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


def _is_valid_duration(value: str) -> bool | str:
    """Validate a listening duration in ``1..MAX_DURATION_S`` for a text answer.

    Args:
        value: Raw input.

    Returns:
        ``True`` if valid, otherwise an error message string.
    """
    try:
        seconds = int(value)
    except ValueError:
        return "Enter a whole number of seconds."
    if seconds < 1:
        return "Enter a number greater than zero."
    if seconds > MAX_DURATION_S:
        return f"Maximum {MAX_DURATION_S} seconds."
    return True
