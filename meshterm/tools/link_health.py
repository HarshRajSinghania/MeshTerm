"""The ``link-health`` tool: flag links that have degraded against their own baseline.

Every trace MeshTerm runs is persisted, so each link has a history. This tool splits a
target's trace history into a recent window and an older baseline and reports any
end-to-end or per-hop metric that has dropped beyond a threshold — turning the live SNR
number the app shows into "this hop is 6 dB below its usual median". Optionally it takes a
few fresh traces first, so it can be run standalone rather than only over stored data.
"""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..services import link_quality, trace_runner
from ..services.link_quality import HealthReport, MetricChange
from .base import Tool, ToolResult, register

_STATUS_STYLE = {
    "ok": "ok",
    "degraded": "warn",
    "critical": "err",
    "insufficient-data": "muted",
}
_SEVERITY_STYLE = {"critical": "err", "warn": "warn", "ok": "ok"}


@register
class LinkHealthTool(Tool):
    """Detect link-quality regressions for a target against its rolling baseline."""

    name = "link-health"
    title = "Link health"
    help = "Flag links whose SNR or reliability dropped below their baseline"
    category = "Diagnostics"
    order = 30

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Choose a target (from history or contacts) and an optional fresh-sample count.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict, or ``None`` if the user cancelled.
        """
        # Offer targets we already have history for first, then known contacts.
        history = ctx.repo.traced_targets()
        try:
            contacts = [c.name for c in await (await ctx.device()).get_contacts()]
        except Exception:  # noqa: BLE001 - history-only analysis needs no device
            contacts = []
        choices = list(dict.fromkeys(history + contacts))
        prompt = "Target to check:"
        if choices:
            target = await ctx.ui.autocomplete(prompt, choices)
        else:
            target = await ctx.ui.text(prompt)
        if not target:
            return None

        samples = await ctx.ui.text(
            "Take how many fresh traces first? (0 = use stored history only)",
            default="0",
            validate=_is_nonneg_int,
        )
        if samples is None:
            return None
        return {"target": target.strip(), "samples": int(samples)}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Optionally trace, load history, analyze regressions, and render the report.

        Args:
            ctx: Shared application context.
            params: ``target``, optional ``samples``/``path``/``recent``/``snr_drop``/
                ``success_drop``/``min_baseline``, and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` carrying the verdict and any regressions.
        """
        target = params["target"]
        run_id = params["_run_id"]
        samples = int(params.get("samples", 0))
        resolve = None

        if samples > 0:
            device = await ctx.device()
            contacts = await device.get_contacts()
            resolve = trace_runner.make_node_resolver(contacts)
            path = None
            if params.get("path"):
                path = trace_runner.parse_trace_path(params["path"], contacts)
            with ctx.ui.progress("link-health") as progress:
                task = progress.add_task(f"tracing {target}", total=samples)
                await trace_runner.run_traces(
                    device,
                    target,
                    samples=samples,
                    path=path,
                    cooldown_s=ctx.settings.trace_cooldown_s,
                    on_result=lambda *_: progress.advance(task),
                    persist=lambda t: ctx.repo.record_trace(run_id, t),
                )

        traces = ctx.repo.recent_traces(target)
        if resolve is None:
            # No fresh run: still try to name hops, but tolerate having no device.
            try:
                contacts = await (await ctx.device()).get_contacts()
                resolve = trace_runner.make_node_resolver(contacts)
            except Exception:  # noqa: BLE001 - analysis works on raw hashes too
                resolve = None

        report = link_quality.analyze_target(
            target,
            traces,
            recent_count=int(params.get("recent", link_quality.DEFAULT_RECENT_COUNT)),
            min_baseline=int(params.get("min_baseline", link_quality.DEFAULT_MIN_BASELINE)),
            snr_drop_db=float(params.get("snr_drop", link_quality.DEFAULT_SNR_DROP_DB)),
            success_drop=float(params.get("success_drop", link_quality.DEFAULT_SUCCESS_DROP)),
            resolve=resolve,
        )

        ctx.ui.show(_report_panel(report))
        if report.status == "insufficient-data":
            message = (
                f"[muted]not enough history for[/muted] [brand]{target}[/brand] "
                f"({report.baseline_n} baseline + {report.recent_n} recent traces); "
                "run more traces or the monitor first."
            )
        elif report.status == "ok":
            message = f"[ok]✓[/ok] [brand]{target}[/brand] is healthy vs its baseline"
        else:
            n = len(report.regressions)
            style = _STATUS_STYLE[report.status]
            message = (
                f"[{style}]![/{style}] [brand]{target}[/brand] shows "
                f"[{style}]{n} regression{'s' if n != 1 else ''}[/{style}] "
                f"({report.status})"
            )

        return ToolResult(
            summary={
                "target": report.target,
                "status": report.status,
                "baseline_n": report.baseline_n,
                "recent_n": report.recent_n,
                "regressions": [
                    {
                        "subject": c.subject,
                        "metric": c.metric,
                        "baseline": round(c.baseline, 2),
                        "recent": round(c.recent, 2),
                        "delta": round(c.delta, 2),
                        "severity": c.severity,
                    }
                    for c in report.regressions
                ],
            },
            message=message,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``link-health`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _link_health(
            target: str = typer.Option(..., "--target", "-t", help="Target node name/prefix"),
            samples: int = typer.Option(
                0, "--samples", "-n", help="Fresh traces to take before analyzing"
            ),
            path: Optional[str] = typer.Option(
                None, "--path", "-p", help="Force a path for the fresh traces"
            ),
            recent: int = typer.Option(
                link_quality.DEFAULT_RECENT_COUNT, "--recent", help="Newest traces as 'recent'"
            ),
            snr_drop: float = typer.Option(
                link_quality.DEFAULT_SNR_DROP_DB, "--snr-drop", help="SNR drop (dB) to flag"
            ),
            success_drop: float = typer.Option(
                link_quality.DEFAULT_SUCCESS_DROP,
                "--success-drop",
                help="Success-rate drop (fraction) to flag",
            ),
        ) -> None:
            tool_params: dict[str, Any] = {
                "target": target, "samples": samples, "recent": recent,
                "snr_drop": snr_drop, "success_drop": success_drop,
            }
            if path:
                tool_params["path"] = path
            run_tool_command(self, tool_params)


def _report_panel(report: HealthReport) -> Panel:
    """Render the health report as a status header plus a table of changes.

    Args:
        report: The analyzed report.

    Returns:
        A Rich :class:`Panel` summarizing the verdict and metric changes.
    """
    style = _STATUS_STYLE.get(report.status, "muted")
    header = Text.assemble(
        ("target   ", "muted"), (f"{report.target}\n", "brand"),
        ("status   ", "muted"), (report.status, style), ("\n", ""),
        ("window   ", "muted"),
        (f"{report.recent_n} recent vs {report.baseline_n} baseline traces", ""),
    )
    if not report.changes:
        return Panel(
            header, title="[accent]LINK HEALTH[/accent]", border_style=style, expand=False
        )

    table = Table(box=None, padding=(0, 2, 0, 0), expand=False)
    table.add_column("METRIC", style="muted")
    table.add_column("SUBJECT")
    table.add_column("BASELINE", justify="right")
    table.add_column("RECENT", justify="right")
    table.add_column("Δ", justify="right")
    # Worst regressions first, then the still-healthy metrics for context.
    for change in sorted(report.changes, key=lambda c: (not c.regressed, c.delta)):
        table.add_row(*_change_row(change))

    from rich.console import Group

    body = Group(header, Text(), table)
    return Panel(body, title="[accent]LINK HEALTH[/accent]", border_style=style, expand=False)


def _change_row(change: MetricChange) -> tuple[str, Text, str, str, Text]:
    """Format one metric change as a styled table row.

    Args:
        change: The metric change to render.

    Returns:
        The five cell values for the changes table.
    """
    sev_style = _SEVERITY_STYLE.get(change.severity, "muted")
    marker = "● " if change.regressed else "  "
    delta_cell = Text(f"{marker}{change.delta:+.2f}", style=sev_style)
    subject = Text(change.subject, style="brand" if change.regressed else None)
    return (
        change.metric,
        subject,
        f"{change.baseline:.2f}",
        f"{change.recent:.2f}",
        delta_cell,
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
