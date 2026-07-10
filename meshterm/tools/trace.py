"""The ``trace`` tool: run repeated path traces and report aggregated SNR.

This is the measurement workhorse of the skeleton and the template every other tool
follows: it gathers params (interactively or from CLI flags), runs a service, persists
results, and renders modern Rich output.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from ..core.models import LOCAL_DEVICE_LABEL
from ..services import trace_runner
from ..ui.widgets import stats_panel, traces_table
from .base import Tool, ToolResult, register

#: Upper bound on traces per run — the per-trace table renders one column each, so
#: this keeps the output readable and the radio's duty cycle in check.
MAX_TRACES = 9


@register
class TraceTool(Tool):
    """Trace the path to a target N times and summarize per-hop SNR."""

    name = "trace"
    title = "Trace"
    help = "Run repeated path traces to a target and aggregate SNR"
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
        # With no known contacts, fall back to a free-text entry so the user can still
        # type a name or key prefix; otherwise suggest the known contacts.
        if choices:
            target = await ctx.ui.autocomplete(prompt, choices)
        else:
            target = await ctx.ui.text(prompt)
        if not target:
            return None

        samples_raw = await ctx.ui.text(
            f"How many traces? (1-{MAX_TRACES})", default="3", validate=_is_valid_sample_count
        )
        if samples_raw is None:
            return None

        # Optional explicit path, MeshCore-app style: comma-separated contact names
        # and/or hex key prefixes (e.g. ``3d,f2,3d``). Blank lets the device route.
        path_prompt = "Force a path? (comma-separated contacts/hex prefixes, blank = auto):"
        validate_path = lambda v: _validate_path(v, contacts)  # noqa: E731
        if choices:
            path_spec = await ctx.ui.autocomplete(path_prompt, choices, validate=validate_path)
        else:
            path_spec = await ctx.ui.text(path_prompt, validate=validate_path)
        if path_spec is None:
            return None

        params: dict[str, Any] = {"target": target.strip(), "samples": int(samples_raw)}
        if path_spec.strip():
            params["path"] = path_spec.strip()
        return params

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run the traces, persist each one, and render the summary.

        Args:
            ctx: Shared application context.
            params: ``target`` (str), ``samples`` (int), and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the aggregated statistics.
        """
        target = params["target"]
        samples = max(1, min(MAX_TRACES, int(params.get("samples", 3))))
        if int(params.get("samples", 3)) > MAX_TRACES:
            ctx.ui.note(f"[warn]capping at {MAX_TRACES} traces[/warn]")
        run_id = params["_run_id"]
        device = await ctx.device()

        # Our own device's name labels both endpoints of the path (first hop's origin,
        # last hop's destination); fall back to a neutral label if it's unavailable.
        self_info = await device.get_self_info()
        device_label = str(self_info.get("name") or LOCAL_DEVICE_LABEL)
        # Our own public key, so the route's endpoints (us) carry a hash like every
        # other hop, addressed at the same path-hash width.
        device_hash = str(self_info.get("public_key") or "") or None

        # Resolve repeater hashes in the results to contact names where we know them,
        # so the tables read as names instead of opaque hex prefixes.
        contacts = await device.get_contacts()
        resolve = trace_runner.make_node_resolver(contacts)

        # Resolve an optional forced path (names/hex) to the hex string the radio wants.
        # Leaving it blank is fully supported: the device reuses the route it already
        # learned for the target (or floods if it has none).
        path: Optional[str] = None
        path_spec = params.get("path")
        if path_spec:
            path = trace_runner.parse_trace_path(path_spec, contacts)
            ctx.ui.note(f"[muted]forcing path:[/muted] [brand]{path}[/brand]")
        else:
            ctx.ui.note("[muted]path: auto (device-routed)[/muted]")

        traces: list = []
        with ctx.ui.progress("trace") as progress:
            task = progress.add_task(f"tracing {target}", total=samples)

            def on_result(done: int, total: int, result) -> None:  # noqa: ANN001
                traces.append(result)
                progress.advance(task)

            stats = await trace_runner.measure(
                device,
                target,
                samples=samples,
                path=path,
                cooldown_s=ctx.settings.trace_cooldown_s,
                on_result=on_result,
                persist=lambda t: ctx.repo.record_trace(run_id, t),
            )

        # Use the first reply we got as the representative route shown in the summary
        # (forced path, or the route the device resolved when auto-routing).
        current = next((t for t in traces if t.success), None)
        if traces:
            ctx.ui.show(traces_table(traces, device_label, resolve, device_hash))
        ctx.ui.show(
            stats_panel(stats, device_label, resolve, route=current, device_hash=device_hash)
        )

        summary: dict[str, Any] = {
            "target": stats.target,
            "samples": stats.samples,
            "success_rate": round(stats.success_rate, 3),
            "median_min_snr": stats.median_min_snr,
            "median_rtt_ms": stats.median_rtt_ms,
            "median_hop_snrs": [
                {
                    "hop": agg.index,
                    "from": agg.origin or device_label,
                    "to": agg.destination or device_label,
                    "median_snr": agg.median_snr,
                }
                for agg in stats.hop_snrs
            ],
        }
        if path:
            summary["path"] = path
        via = f" via [brand]{path}[/brand]" if path else ""
        return ToolResult(
            summary=summary,
            message=f"[ok]✓[/ok] traced [brand]{target}[/brand] x{samples}{via}",
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``trace`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _trace(
            target: str = typer.Option(..., "--target", "-t", help="Target node name/prefix"),
            samples: int = typer.Option(
                3, "--samples", "-n", help=f"Number of traces (max {MAX_TRACES})"
            ),
            path: Optional[str] = typer.Option(
                None,
                "--path",
                "-p",
                help="Force a route: comma-separated contact names/hex prefixes (e.g. 3d,f2,3d)",
            ),
        ) -> None:
            tool_params: dict[str, Any] = {"target": target, "samples": samples}
            if path:
                tool_params["path"] = path
            run_tool_command(self, tool_params)


def _validate_path(value: str, contacts: list[Any]) -> bool | str:
    """Validate an optional forced-path spec for the interactive prompt.

    Args:
        value: The raw comma-separated path entry; blank means "let the device route".
        contacts: Known contacts used to resolve names in the spec.

    Returns:
        ``True`` if valid (including blank), otherwise an error message string.
    """
    if not value.strip():
        return True
    try:
        trace_runner.parse_trace_path(value, contacts)
        return True
    except ValueError as exc:
        return str(exc)


def _is_valid_sample_count(value: str) -> bool | str:
    """Validate that a questionary text answer is a trace count in ``1..MAX_TRACES``.

    Args:
        value: The raw user input.

    Returns:
        ``True`` if valid, otherwise an error message string.
    """
    try:
        count = int(value)
    except ValueError:
        return "Enter a whole number."
    if count < 1:
        return "Enter a number greater than zero."
    if count > MAX_TRACES:
        return f"Maximum {MAX_TRACES} traces."
    return True
