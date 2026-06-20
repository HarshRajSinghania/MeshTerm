"""The ``tx-optimize`` tool: sweep transmit power and converge on the best setting.

Wraps :func:`meshtools.services.tx_optimizer.optimize_tx_power` with interactive prompts,
a live progress bar, persistence of every level, an interactive HTML chart, and an opt-in
apply step (changing the radio's TX power is a hardware change, so it is never implicit).
"""

from __future__ import annotations

from typing import Any, Optional

import questionary
import typer

from ..context import AppContext
from ..core.connection import TX_POWER_MAX, TX_POWER_MIN
from ..services import tx_optimizer
from ..ui.widgets import make_progress, tx_opt_summary, tx_opt_table
from ..viz.tx_plot import render_tx_optimization
from .base import Tool, ToolResult, register


@register
class TxOptimizeTool(Tool):
    """Find the transmit power with the strongest, most reliable signal to a target."""

    name = "tx-optimize"
    help = "Sweep TX power and converge on the value with the best signal."
    category = "Optimization"
    order = 10

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Interactively gather target, range, sampling, and apply choices.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict, or ``None`` if the user cancelled.
        """
        device = await ctx.device()
        contacts = await device.get_contacts()
        choices = [c.name for c in contacts]
        prompt = "Target node to optimize against:"
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

        samples = await questionary.text(
            "Traces per TX level:", default="5", validate=_positive_int
        ).ask_async()
        if samples is None:
            return None
        step = await questionary.text(
            "Coarse step:", default="3", validate=_positive_int
        ).ask_async()
        if step is None:
            return None
        refine = await questionary.confirm(
            "Refine around the best level?", default=True
        ).ask_async()
        apply = await questionary.confirm(
            "Apply the winning TX power to the device afterward?", default=False
        ).ask_async()

        return {
            "target": target.strip(),
            "samples": int(samples),
            "step": int(step),
            "refine": bool(refine),
            "apply": bool(apply),
            "tx_min": TX_POWER_MIN,
            "tx_max": TX_POWER_MAX,
            "viz": True,
        }

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run the sweep, persist levels, render results, and optionally apply the winner.

        Args:
            ctx: Shared application context.
            params: ``target``, ``samples``, ``step``, ``refine``, ``apply``,
                ``tx_min``, ``tx_max``, ``viz``, and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the optimum and the chart path.
        """
        target = params["target"]
        run_id = params["_run_id"]
        device = await ctx.device()

        with make_progress(ctx.console) as progress:
            task = progress.add_task(f"optimizing TX -> {target}", total=None)

            def on_level(done: int, total: int, level) -> None:  # noqa: ANN001
                progress.update(
                    task, total=total, completed=done,
                    description=f"TX {level.tx_power:>2}  "
                    f"SNR {_fmt_snr(level.stats.median_min_snr)}",
                )

            result = await tx_optimizer.optimize_tx_power(
                device,
                target,
                tx_min=int(params.get("tx_min", TX_POWER_MIN)),
                tx_max=int(params.get("tx_max", TX_POWER_MAX)),
                coarse_step=int(params.get("step", 3)),
                samples_per_level=int(params.get("samples", 5)),
                refine=bool(params.get("refine", True)),
                cooldown_s=ctx.settings.trace_cooldown_s,
                on_level=on_level,
                persist_level=lambda stats: ctx.repo.record_tx_sample(run_id, stats),
                persist_trace=lambda t: ctx.repo.record_trace(run_id, t),
            )

        if params.get("apply"):
            await device.set_tx_power(result.best_tx)
            result.applied = True
            ctx.log.info("applied TX power %s to device", result.best_tx)

        ctx.console.print(tx_opt_table(result))
        ctx.console.print(tx_opt_summary(result))

        artifacts: list[str] = []
        if params.get("viz", True):
            assert ctx.settings.output_dir is not None
            path = render_tx_optimization(result, ctx.settings.output_dir)
            artifacts.append(str(path))

        return ToolResult(
            summary={
                "target": target,
                "best_tx": result.best_tx,
                "best_score": round(result.best_score, 3),
                "original_tx": result.original_tx,
                "applied": result.applied,
                "levels_measured": len(result.levels),
            },
            message=f"[ok]✓[/ok] optimum TX for [brand]{target}[/brand] is "
            f"[brand]{result.best_tx}[/brand]"
            + ("  [ok](applied)[/ok]" if result.applied else ""),
            artifacts=artifacts,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``tx-optimize`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _tx_optimize(
            target: str = typer.Option(..., "--target", "-t", help="Target node."),
            samples: int = typer.Option(5, "--samples", "-n", help="Traces per level."),
            step: int = typer.Option(3, "--step", help="Coarse sweep step."),
            tx_min: int = typer.Option(TX_POWER_MIN, "--min", help="Lowest TX power."),
            tx_max: int = typer.Option(TX_POWER_MAX, "--max", help="Highest TX power."),
            refine: bool = typer.Option(True, "--refine/--no-refine", help="Local refine."),
            apply: bool = typer.Option(False, "--apply", help="Write the winner to device."),
            viz: bool = typer.Option(True, "--viz/--no-viz", help="Generate HTML chart."),
        ) -> None:
            run_tool_command(
                self,
                {
                    "target": target, "samples": samples, "step": step,
                    "tx_min": tx_min, "tx_max": tx_max, "refine": refine,
                    "apply": apply, "viz": viz,
                },
            )


def _positive_int(value: str) -> bool | str:
    """Validate a positive-integer questionary answer.

    Args:
        value: Raw input.

    Returns:
        ``True`` if valid, else an error message.
    """
    try:
        return int(value) > 0 or "Enter a number greater than zero."
    except ValueError:
        return "Enter a whole number."


def _fmt_snr(snr: Optional[float]) -> str:
    """Format an optional SNR for the progress description.

    Args:
        snr: SNR in dB, or ``None``.

    Returns:
        A short fixed-width string like ``+5.1`` or ``  n/a``.
    """
    return f"{snr:+.1f}" if snr is not None else " n/a"
