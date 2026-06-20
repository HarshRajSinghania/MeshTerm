"""The ``trace`` tool: run repeated path traces and report aggregated SNR.

This is the measurement workhorse of the skeleton and the template every other tool
follows: it gathers params (interactively or from CLI flags), runs a service, persists
results, and renders modern Rich output.
"""

from __future__ import annotations

from typing import Any, Optional

import questionary
import typer

from ..context import AppContext
from ..services import trace_runner
from ..ui.widgets import make_progress, stats_panel, trace_table
from .base import Tool, ToolResult, register


@register
class TraceTool(Tool):
    """Trace the path to a target N times and summarize per-hop SNR."""

    name = "trace"
    help = "Run repeated path traces to a target and aggregate SNR."
    category = "Diagnostics"
    order = 10

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Interactively choose a target contact and sample count.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict, or ``None`` if the user cancelled.
        """
        device = await ctx.device()
        contacts = await device.get_contacts()
        choices = [c.name for c in contacts]
        prompt = "Target node (name or key prefix):"
        # ``autocomplete`` requires a non-empty choice list; with no known contacts fall
        # back to a free-text entry so the user can still type a name or key prefix.
        if choices:
            target = await questionary.autocomplete(
                prompt, choices=choices, ignore_case=True
            ).ask_async()
        else:
            target = await questionary.text(prompt).ask_async()
        if not target:
            return None

        samples_raw = await questionary.text(
            "How many traces?", default="5", validate=_is_positive_int
        ).ask_async()
        if samples_raw is None:
            return None
        return {"target": target.strip(), "samples": int(samples_raw)}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run the traces, persist each one, and render the summary.

        Args:
            ctx: Shared application context.
            params: ``target`` (str), ``samples`` (int), and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the aggregated statistics.
        """
        target = params["target"]
        samples = int(params.get("samples", 5))
        run_id = params["_run_id"]
        device = await ctx.device()

        last_trace = None
        with make_progress(ctx.console) as progress:
            task = progress.add_task(f"tracing {target}", total=samples)

            def on_result(done: int, total: int, result) -> None:  # noqa: ANN001
                nonlocal last_trace
                last_trace = result
                progress.advance(task)

            stats = await trace_runner.measure(
                device,
                target,
                samples=samples,
                cooldown_s=ctx.settings.trace_cooldown_s,
                on_result=on_result,
                persist=lambda t: ctx.repo.record_trace(run_id, t),
            )

        if last_trace is not None and last_trace.success:
            ctx.console.print(trace_table(last_trace))
        ctx.console.print(stats_panel(stats))

        return ToolResult(
            summary={
                "target": stats.target,
                "samples": stats.samples,
                "success_rate": round(stats.success_rate, 3),
                "median_min_snr": stats.median_min_snr,
                "median_rtt_ms": stats.median_rtt_ms,
            },
            message=f"[ok]✓[/ok] traced [brand]{target}[/brand] x{samples}",
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``trace`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _trace(
            target: str = typer.Option(..., "--target", "-t", help="Target node name/prefix."),
            samples: int = typer.Option(5, "--samples", "-n", help="Number of traces."),
        ) -> None:
            run_tool_command(self, {"target": target, "samples": samples})


def _is_positive_int(value: str) -> bool | str:
    """Validate that a questionary text answer is a positive integer.

    Args:
        value: The raw user input.

    Returns:
        ``True`` if valid, otherwise an error message string.
    """
    try:
        return int(value) > 0 or "Enter a number greater than zero."
    except ValueError:
        return "Enter a whole number."
