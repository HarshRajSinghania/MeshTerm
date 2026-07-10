"""The ``trace`` tool: path traces to a target, live in the menu, one-shot on the CLI.

In the interactive menu this opens the live trace screen
(:mod:`meshterm.ui.trace_screen`): pick a target from a recency-ordered list and traces
start streaming immediately — further bursts, sample counts, and a forced path are
keystrokes inside the screen, following the same living-screen pattern as chat and the
nodes list. On the CLI it stays a scriptable one-shot: run N traces, print the per-trace
table and the aggregate summary, exit.

Both front ends persist identically: one ``runs`` row per burst with every trace recorded
under it, so stored history reads the same no matter where it came from.
"""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.text import Text

from ..context import AppContext
from ..core.models import LOCAL_DEVICE_LABEL, Contact
from ..services import trace_runner
from ..ui.widgets import stats_panel, traces_table
from .base import Tool, ToolResult, register

#: Upper bound on traces per scripted run — the per-trace table renders one column each,
#: so this keeps the output readable and the radio's duty cycle in check.
MAX_TRACES = 9

_BACK = "__back__"


@register
class TraceTool(Tool):
    """Trace the path to a target and watch per-hop SNR, live or scripted."""

    name = "trace"
    title = "Trace"
    help = "Trace the path to a target and watch per-hop SNR live"
    category = "Diagnostics"
    order = 10

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Pick the first target for the live screen.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"live": True, "target": name}``, or ``None`` if the user backed out.
        """
        target = await self._pick_target(ctx)
        if target is None:
            return None
        return {"live": True, "target": target}

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run — without the outer run row on the live path.

        The live screen opens one ``runs`` row *per burst* (matching what a scripted
        invocation records), so wrapping the whole screen session in another row would
        double-log it. Scripted runs keep the base class's logging.

        Args:
            ctx: Shared application context.
            params: Parameters for this invocation.

        Returns:
            The :class:`ToolResult` from :meth:`run`.
        """
        if params.get("live"):
            return await self.run(ctx, params)
        return await super().execute(ctx, params)

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open the live screen (menu) or run a persisted one-shot burst (CLI).

        Args:
            ctx: Shared application context.
            params: ``live`` + ``target`` from the menu picker; or ``target`` (str),
                ``samples`` (int), optional ``path``, and the injected ``_run_id`` from
                the CLI.

        Returns:
            A :class:`ToolResult` with the aggregated statistics.
        """
        if params.get("live"):
            return await self._run_live(ctx, str(params["target"]))
        return await self._run_cli(ctx, params)

    # -- interactive (menu) -------------------------------------------------------

    async def _run_live(self, ctx: AppContext, target: str) -> ToolResult:
        """Loop the live screen and the target picker until the user backs out.

        Backing out of a trace session (Esc) steps back to the target picker rather than
        all the way to the main menu — the chat pattern — with the just-closed target
        pre-highlighted; Esc from the picker returns to the menu.

        Args:
            ctx: Shared application context.
            target: The first target to open.

        Returns:
            A :class:`ToolResult` counting the sessions and traces run.
        """
        from ..ui.trace_screen import open_trace

        sessions = 0
        traces = 0
        current: Optional[str] = target
        while current is not None:
            traces += await open_trace(ctx, current)
            sessions += 1
            current = await self._pick_target(ctx, default=current)
        return ToolResult(summary={"sessions": sessions, "traces": traces})

    async def _pick_target(
        self, ctx: AppContext, *, default: Optional[str] = None
    ) -> Optional[str]:
        """Pick a trace target: recently traced first, then contacts by recency.

        Args:
            ctx: Shared application context.
            default: A target name to pre-highlight (the one just traced), so the cursor
                lands where the user left.

        Returns:
            The chosen target name, or ``None`` if cancelled. With no known contacts and
            no history, falls back to a free-text prompt so a key prefix can be typed.
        """
        from ..ui.tui import Choice, Separator
        from ..ui.widgets import _DEFAULT_GLYPH, _NODE_GLYPHS

        device = await ctx.device()
        contacts = await device.get_contacts()
        recent = ctx.repo.traced_targets()

        if not contacts and not recent:
            entered = await ctx.ui.text("Target node (name or key prefix):")
            return entered.strip() if entered else None

        by_name = {c.name: c for c in contacts}

        def row(name: str) -> Choice:
            contact = by_name.get(name)
            glyph, style = (
                _NODE_GLYPHS.get(contact.node_type, _DEFAULT_GLYPH)
                if contact is not None
                else _DEFAULT_GLYPH
            )
            label = Text(glyph, style=style)
            label.append(f" {name}")
            return Choice(title=label, value=name)

        items: list = []
        listed: set[str] = set()
        if recent:
            items.append(Separator("── ⏱ Recently traced ──", style="accent"))
            for name in recent:
                items.append(row(name))
                listed.add(name)
        remaining = [c for c in contacts if c.name not in listed]
        if remaining:
            items.append(Separator("── 👤 Contacts ──", style="accent"))
            for contact in _by_recency(remaining):
                items.append(row(contact.name))
        items.append(Separator(" "))
        items.append(Choice(title="Back", value=_BACK))

        choice = await ctx.ui.select(
            "Trace — pick a target", items, default=default, wrap=False
        )
        if choice in (None, _BACK):
            return None
        return str(choice)

    # -- scripted (CLI) -------------------------------------------------------------

    async def _run_cli(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run the traces, persist each one, and print the summary tables.

        Args:
            ctx: Shared application context.
            params: ``target`` (str), ``samples`` (int), optional ``path``, and the
                injected ``_run_id``.

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

        @app.command(name=self.name, help="Run repeated path traces to a target and aggregate SNR")
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


def _by_recency(contacts: list[Contact]) -> list[Contact]:
    """Order contacts most-recently-heard first (never-heard last, alphabetically).

    Args:
        contacts: The contacts to order.

    Returns:
        A new sorted list; the input is left untouched.
    """

    def key(c: Contact) -> tuple:
        heard = c.last_seen is not None and getattr(c.last_seen, "tzinfo", None) is not None
        stamp = c.last_seen.timestamp() if heard else 0.0
        return (not heard, -stamp, c.name.casefold())

    return sorted(contacts, key=key)
