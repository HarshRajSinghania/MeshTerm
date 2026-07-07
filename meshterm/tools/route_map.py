"""The ``route-map`` tool: visualize how stable mesh routing is over time.

Where the app's traceroute is a single snapshot, this aggregates a target's (or the whole
mesh's) trace history into an interactive graph and churn metrics: which links are always
used, which the mesh flaps between, and how often the single most-common route actually
wins. It reads stored traces and can optionally take a few fresh ones first.
"""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..services import route_stability, trace_runner
from ..services.route_stability import RouteGraph
from ..ui.theme import snr_style
from ..ui.tui import Choice, Separator
from ..viz.route_graph import render_route_graph
from .base import Tool, ToolResult, register

_WHOLE_MESH = "(whole mesh)"
_BACK = "__back__"


@register
class RouteMapTool(Tool):
    """Aggregate trace history into a route-stability graph and churn metrics."""

    name = "route-map"
    help = "Map how stable mesh routing is over time and render an interactive graph."
    category = "Optimization"
    order = 30

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Choose a target (or the whole mesh) and an optional fresh-sample count.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict, or ``None`` if the user cancelled.
        """
        targets = ctx.repo.traced_targets()
        choice = await ctx.ui.select(
            "Map routing for:",
            [
                Choice(_WHOLE_MESH, _WHOLE_MESH),
                *(Choice(t, t) for t in targets),
                Separator(" "),
                Choice("Back", _BACK),
            ],
        )
        if choice in (None, _BACK):
            return None
        samples = await ctx.ui.text(
            "Take how many fresh traces first? (0 = use stored history only)",
            default="0",
            validate=_is_nonneg_int,
        )
        if samples is None:
            return None
        params: dict[str, Any] = {"samples": int(samples)}
        if choice != _WHOLE_MESH:
            params["target"] = choice
        return params

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Optionally trace, load history, build the graph, render it, and summarize.

        Args:
            ctx: Shared application context.
            params: optional ``target``/``samples``/``path``/``viz`` and ``_run_id``.

        Returns:
            A :class:`ToolResult` with the churn metrics and the graph path.
        """
        target = params.get("target")
        run_id = params["_run_id"]
        samples = int(params.get("samples", 0))
        resolve = None

        if samples > 0:
            if not target:
                return ToolResult(
                    message="[err]✗[/err] --samples requires a --target to trace.",
                )
            device = await ctx.device()
            contacts = await device.get_contacts()
            resolve = trace_runner.make_node_resolver(contacts)
            path = (
                trace_runner.parse_trace_path(params["path"], contacts)
                if params.get("path")
                else None
            )
            with ctx.ui.progress("route-map") as progress:
                task = progress.add_task(f"tracing {target}", total=samples)
                await trace_runner.run_traces(
                    device, target, samples=samples, path=path,
                    cooldown_s=ctx.settings.trace_cooldown_s,
                    on_result=lambda *_: progress.advance(task),
                    persist=lambda t: ctx.repo.record_trace(run_id, t),
                )

        traces = ctx.repo.recent_traces(target) if target else ctx.repo.all_traces()
        if resolve is None:
            try:
                contacts = await (await ctx.device()).get_contacts()
                resolve = trace_runner.make_node_resolver(contacts)
            except Exception:  # noqa: BLE001 - the graph reads fine with raw hashes
                resolve = None

        graph = route_stability.build_route_graph(traces, target=target, resolve=resolve)

        ctx.ui.show(_graph_panel(graph))

        artifacts: list[str] = []
        if graph.total_routes and params.get("viz", True) and ctx.settings.output_dir:
            out = render_route_graph(graph, ctx.settings.output_dir)
            artifacts.append(str(out))

        scope = target or "the mesh"
        if not graph.total_routes:
            message = (
                f"[muted]no successful traces stored for[/muted] [brand]{scope}[/brand] — "
                "run some traces first."
            )
        else:
            message = (
                f"[ok]✓[/ok] [brand]{scope}[/brand]: {graph.stability:.0%} of "
                f"{graph.total_routes} traces took the top route "
                f"({graph.distinct_routes} distinct)"
            )

        return ToolResult(
            summary={
                "target": target,
                "total_routes": graph.total_routes,
                "distinct_routes": graph.distinct_routes,
                "stability": round(graph.stability, 3),
                "churn_entropy_bits": round(graph.churn_entropy, 3),
                "nodes": len(graph.nodes),
                "links": len(graph.links),
            },
            message=message,
            artifacts=artifacts,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``route-map`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _route_map(
            target: Optional[str] = typer.Option(
                None, "--target", "-t", help="Target to map (omit for the whole mesh)."
            ),
            samples: int = typer.Option(
                0, "--samples", "-n", help="Fresh traces to take first (requires --target)."
            ),
            path: Optional[str] = typer.Option(
                None, "--path", "-p", help="Force a path for the fresh traces."
            ),
            viz: bool = typer.Option(True, "--viz/--no-viz", help="Render the HTML graph."),
        ) -> None:
            tool_params: dict[str, Any] = {"samples": samples, "viz": viz}
            if target:
                tool_params["target"] = target
            if path:
                tool_params["path"] = path
            run_tool_command(self, tool_params)


def _graph_panel(graph: RouteGraph) -> Panel:
    """Render churn metrics and the busiest links as a panel.

    Args:
        graph: The aggregated route graph.

    Returns:
        A Rich :class:`Panel` summarizing stability and top links.
    """
    scope = graph.target or "whole mesh"
    stab_style = "ok" if graph.stability >= 0.8 else ("warn" if graph.stability >= 0.5 else "err")
    header = Text.assemble(
        ("scope          ", "muted"), (f"{scope}\n", "brand"),
        ("traces         ", "muted"), (f"{graph.total_routes}\n", ""),
        ("distinct routes ", "muted"), (f"{graph.distinct_routes}\n", ""),
        ("stability      ", "muted"), (f"{graph.stability:.0%}", stab_style), ("\n", ""),
        ("route entropy  ", "muted"), (f"{graph.churn_entropy:.2f} bits", ""),
    )
    if not graph.links:
        return Panel(
            header, title="[accent]route stability[/accent]", border_style="muted", expand=False
        )

    table = Table(box=None, padding=(0, 2, 0, 0), expand=False)
    table.add_column("link")
    table.add_column("uses", justify="right")
    table.add_column("median SNR", justify="right")
    for link in graph.links[:10]:
        table.add_row(
            f"{link.origin} → {link.destination}",
            str(link.count),
            Text(f"{link.median_snr:+.1f} dB", style=snr_style(link.median_snr)),
        )

    from rich.console import Group

    body = Group(header, Text("\nbusiest links", style="muted"), table)
    return Panel(
        body, title="[accent]route stability[/accent]", border_style="accent", expand=False
    )


def _is_nonneg_int(value: str) -> bool | str:
    """Validate a non-negative integer for a questionary text answer.

    Args:
        value: Raw input.

    Returns:
        ``True`` if valid, otherwise an error message string.
    """
    try:
        return True if int(value) >= 0 else "Enter zero or a positive number."
    except ValueError:
        return "Enter a whole number."
