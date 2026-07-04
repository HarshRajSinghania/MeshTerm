"""The ``monitor`` tool: control the passive background logger and review its history.

Passive monitoring records every advert and telemetry frame the companion overhears —
with SNR, RSSI, and any shared location — to the database, building the longitudinal
history that the coverage map and link-quality alerting read back. It transmits nothing;
it only listens.

Capture itself runs as a non-blocking background subscription owned by
:class:`~meshtools.services.monitor_service.MonitorService` (``ctx.monitor``), so it never
blocks the menu. This tool is the control panel for it: toggling capture on/off (a
preference remembered between sessions) and reviewing the heard-node history. The live
packet counters are shown in the main-menu header, not here.

The CLI subcommand additionally offers a one-shot foreground capture (``--seconds``) for
scripting, since a short-lived CLI process has no long-running menu to host a background
subscription.
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

#: Cap a single foreground capture window so a scripted run can't block indefinitely;
#: longer passive logging is done by enabling the background monitor instead.
MAX_DURATION_S = 3600


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
        choice = await questionary.select(
            f"Passive monitor — {monitor.status_text()}",
            choices=[
                questionary.Choice(toggle_label, value="toggle"),
                questionary.Choice("View heard nodes (all time)", value="view"),
                questionary.Choice("Back", value="__back__"),
            ],
        ).ask_async()
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
        state = "[ok]● ON[/ok]" if now_on else "[muted]○ OFF[/muted]"
        return ToolResult(
            summary={"enabled": now_on, "active": ctx.monitor.active},
            message=f"passive monitor is now {state}",
        )

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
            ctx.console.print(_heard_table(heard, lambda _node: None))
        else:
            ctx.console.print("[muted]no packets heard yet — turn monitoring on[/muted]")

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
            seconds: Optional[int] = typer.Option(
                None,
                "--seconds",
                "-d",
                help=f"Capture in the foreground for N seconds now, then exit "
                f"(1-{MAX_DURATION_S}).",
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
            if seconds is not None:
                _run_foreground_capture(ctx, seconds)
                return
            # No flags: report the current preference and history size.
            enabled = "[ok]on[/ok]" if store.load_enabled() else "[muted]off[/muted]"
            ctx.console.print(
                f"background monitoring is {enabled} · "
                f"[brand]{ctx.repo.observation_count()}[/brand] observations logged all-time"
            )


def _run_foreground_capture(ctx: AppContext, seconds: int) -> None:
    """Drive a one-shot foreground capture on its own event loop.

    Args:
        ctx: Shared application context.
        seconds: Requested capture duration (clamped to ``1..MAX_DURATION_S``).
    """
    import asyncio

    from ..cli import _drive

    asyncio.run(_drive(_foreground_capture(ctx, seconds), ctx))


async def _foreground_capture(ctx: AppContext, seconds: int) -> None:
    """Capture overheard packets for a bounded window and print a heard-node table.

    This is the scripted, blocking counterpart to the background monitor: it records to
    its own ``monitor`` run and returns when the window elapses. Used by ``monitor
    --seconds``.

    Args:
        ctx: Shared application context.
        seconds: Requested capture duration (clamped to ``1..MAX_DURATION_S``).
    """
    duration = max(1, min(MAX_DURATION_S, int(seconds)))
    run_id = ctx.repo.start_run(
        "monitor", {"mode": "foreground", "duration": duration}, ctx.profile_name
    )
    observations: list[Observation] = []
    try:
        device = await ctx.device()
        resolve = trace_runner.make_node_resolver(await device.get_contacts())
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
    except Exception as exc:  # noqa: BLE001 - record then re-raise for the CLI handler
        ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
        raise

    heard = _aggregate(observations)
    if heard:
        ctx.console.print(_heard_table(heard, resolve))
    else:
        ctx.console.print("[muted]no packets heard during the window[/muted]")

    located = sum(1 for n in heard if n.has_location)
    ctx.repo.finish_run(
        run_id,
        "ok",
        {
            "duration_s": duration,
            "observations": len(observations),
            "nodes_heard": len(heard),
            "nodes_with_location": located,
        },
    )
    ctx.console.print(
        f"[ok]✓[/ok] heard [brand]{len(observations)}[/brand] packets from "
        f"[brand]{len(heard)}[/brand] nodes in {duration}s"
    )


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
