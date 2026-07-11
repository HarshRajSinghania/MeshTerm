"""The ``tx-optimize`` tool: tune a remote node's TX power for the best signal at a target.

You force a path (just like ``trace``) ending at the **target** node where SNR is
measured. The node one hop *before* the target — one you hold admin rights on — is the
node whose transmit power gets swept and tuned. Admin passwords are remembered between
runs.

In the interactive menu one prompt picks the path and the live sweep screen
(:mod:`meshterm.ui.tx_screen`) takes it from there: levels land in a bar chart as they
are measured, and the apply decision is made *after* the sweep, over the evidence. On the
CLI it stays a scriptable one-shot with the full flag set (range, step, samples,
``--apply``, the HTML chart), and progress streams like a trace.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from ..core.connection import DeviceCommandError
from ..core.models import Contact
from ..services import trace_runner, tx_optimizer
from ..ui.widgets import tx_opt_summary, tx_opt_table
from ..viz.tx_plot import render_tx_optimization
from .base import Tool, ToolResult, register

#: Trace count per TX level — kept to single digits so the radio's duty cycle stays sane
#: and the per-level sweep finishes in reasonable time.
MAX_SAMPLES = 9


@register
class TxOptimizeTool(Tool):
    """Tune a remote node's TX power for the strongest, most reliable signal at a target."""

    name = "tx-optimize"
    title = "TX optimize"
    icon = "📶"
    help = "Tune a remote node's TX power for the best signal at a target"
    category = "Tools"
    order = 15  # right after Trace, its measurement sibling

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Pick the path for the live sweep (everything else defaults, tuned in-screen).

        Args:
            ctx: Shared application context.

        Returns:
            ``{"live": True, "path": spec}``, or ``None`` if the user cancelled.
        """
        device = await ctx.device()
        contacts = await device.get_contacts()
        choices = [c.name for c in contacts]

        prompt = (
            "Comma-separated contacts/hex, ending at the target — "
            "the node just before it is tuned"
        )
        validate_path = lambda v: _validate_link_path(v, contacts)  # noqa: E731
        if choices:
            path_spec = await ctx.ui.autocomplete(
                "Path to the target", choices, prompt=prompt, validate=validate_path
            )
        else:
            path_spec = await ctx.ui.text(
                "Path to the target", prompt=prompt, validate=validate_path
            )
        if not path_spec:
            return None
        return {"live": True, "path": path_spec.strip()}

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run — without the outer run row on the live path.

        The live screen opens its own ``runs`` row for the sweep (matching what a
        scripted invocation records), so wrapping the screen session in another row
        would double-log it. Scripted runs keep the base class's logging.

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
        """Open the live sweep (menu) or run the scripted optimization (CLI).

        Args:
            ctx: Shared application context.
            params: ``live`` + ``path`` from the menu prompt; or ``path``, ``samples``,
                ``tx_min``, ``tx_max``, ``step``, ``apply``, optional ``password``/
                ``viz``, and the injected ``_run_id`` from the CLI.

        Returns:
            A :class:`ToolResult` with the optimum and any chart path.
        """
        if params.get("live"):
            return await self._run_live(ctx, str(params["path"]))
        return await self._run_cli(ctx, params)

    # -- interactive (menu) ---------------------------------------------------------

    async def _run_live(self, ctx: AppContext, path_spec: str) -> ToolResult:
        """Resolve the link, log in, and hand off to the live sweep screen.

        Args:
            ctx: Shared application context.
            path_spec: The user's comma-separated path entry (names and/or hex).

        Returns:
            A :class:`ToolResult` echoing the sweep's recorded summary.
        """
        from ..ui.tx_screen import open_tx_optimize

        device = await ctx.device()
        contacts = await device.get_contacts()
        path = trace_runner.parse_trace_path(path_spec, contacts)
        admin_node, target_label = _resolve_link(path, contacts)
        await self._login(ctx, admin_node, {})

        summary = await open_tx_optimize(
            ctx, admin_node=admin_node, target_label=target_label, path=path
        )
        return ToolResult(summary=summary)

    async def _login(
        self, ctx: AppContext, admin_node: Contact, params: dict[str, Any]
    ) -> None:
        """Authenticate against the admin node, remembering a working password.

        Args:
            ctx: Shared application context.
            admin_node: The node we're about to tune.
            params: Tool params (may carry an explicit ``password`` on the CLI).

        Raises:
            DeviceCommandError: If the login is rejected (the stored password, now
                known bad, is forgotten so the next run asks fresh).
        """
        device = await ctx.device()
        password = await self._resolve_password(ctx, admin_node, params)
        if not await device.admin_login(admin_node, password):
            ctx.admin_store.forget(admin_node)  # bad password: don't keep reusing it
            raise DeviceCommandError(
                f"admin login to {admin_node.name!r} failed (wrong password?). "
                "The saved password was cleared; re-run to enter a new one."
            )
        ctx.admin_store.remember(admin_node, password)

    # -- scripted (CLI) ---------------------------------------------------------------

    async def _run_cli(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Resolve the link, log in, sweep TX power, render results, and apply the winner.

        Args:
            ctx: Shared application context.
            params: ``path``, ``samples``, ``tx_min``, ``tx_max``, ``step``, ``apply``,
                optional ``password``/``viz``, and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the optimum and any chart path.
        """
        run_id = params["_run_id"]
        device = await ctx.device()
        contacts = await device.get_contacts()

        # Resolve the forced path to hashes, then pull out the target (last hop) and the
        # admin node we tune (the hop before it).
        path = trace_runner.parse_trace_path(params["path"], contacts)
        admin_node, target_label = _resolve_link(path, contacts)

        samples = max(1, min(MAX_SAMPLES, int(params.get("samples", 3))))
        if int(params.get("samples", 3)) > MAX_SAMPLES:
            ctx.ui.note(f"[warn]capping at {MAX_SAMPLES} traces per level[/warn]")

        ctx.ui.note(f"[muted]logging in to[/muted] [brand]{admin_node.name}[/brand] [muted]…[/muted]")
        await self._login(ctx, admin_node, params)

        ctx.ui.note(
            f"[muted]tuning[/muted] [brand]{admin_node.name}[/brand] "
            f"[muted]→ target[/muted] [brand]{target_label}[/brand]  "
            f"[muted]via {path}[/muted]"
        )

        with ctx.ui.progress("tx-optimize") as progress:
            task = progress.add_task(f"optimizing TX -> {target_label}", total=None)

            def on_level(done: int, total: int, level) -> None:  # noqa: ANN001
                progress.update(
                    task, total=total, completed=done,
                    description=f"TX {level.tx_power:>2}  "
                    f"SNR {_fmt_snr(level.target_snr)}  {level.success_rate:.0%}",
                )

            result = await tx_optimizer.optimize_tx_power(
                device,
                target_label,
                admin_node,
                path,
                tx_min=int(params.get("tx_min", ctx.settings.tx_opt_min)),
                tx_max=int(params.get("tx_max", ctx.settings.tx_opt_max)),
                coarse_step=int(params.get("step", 3)),
                samples_per_level=samples,
                apply=bool(params.get("apply", True)),
                cooldown_s=ctx.settings.trace_cooldown_s,
                on_level=on_level,
                persist_level=lambda lv: ctx.repo.record_tx_sample(run_id, lv),
                persist_trace=lambda t: ctx.repo.record_trace(run_id, t),
            )

        # No trace got through at any TX level: nothing was tuned, the node was left at
        # its original power. Usually a wrong/unreachable path rather than a weak link.
        no_result = result.best_snr is None
        if result.applied:
            ctx.log.info("set TX power %s on %s", result.best_tx, admin_node.name)

        ctx.ui.show(tx_opt_table(result))
        ctx.ui.show(tx_opt_summary(result))

        artifacts: list[str] = []
        if params.get("viz", True):
            assert ctx.settings.output_dir is not None
            path_out = render_tx_optimization(result, ctx.settings.output_dir)
            artifacts.append(str(path_out))

        if no_result:
            restored = (
                f" Restored TX to {result.original_tx}." if result.original_tx is not None else ""
            )
            message = (
                f"[warn]![/warn] no traces reached [brand]{result.target}[/brand] at any TX "
                f"level — check the path ends at the target and is reachable.{restored}"
            )
        else:
            applied_note = f"  [ok](set on {admin_node.name})[/ok]" if result.applied else ""
            message = (
                f"[ok]✓[/ok] optimal TX for [brand]{admin_node.name}[/brand] → "
                f"[brand]{result.target}[/brand] is [brand]{result.best_tx}[/brand]"
                + applied_note
            )

        return ToolResult(
            summary={
                "target": result.target,
                "admin_node": result.admin_node,
                "path": result.path,
                "best_tx": result.best_tx,
                "best_snr": result.best_snr,
                "best_success_rate": round(result.best_success_rate, 3),
                "original_tx": result.original_tx,
                "applied": result.applied,
                "levels_measured": len(result.levels),
            },
            message=message,
            artifacts=artifacts,
        )

    async def _resolve_password(
        self, ctx: AppContext, admin_node: Contact, params: dict[str, Any]
    ) -> str:
        """Find the admin password: explicit flag, remembered, or an interactive prompt.

        Args:
            ctx: Shared application context.
            admin_node: The node we're about to log in to.
            params: Tool params (may carry an explicit ``password``).

        Returns:
            The password to log in with.

        Raises:
            DeviceCommandError: If no password is available and we can't prompt (JSON mode).
        """
        password = params.get("password") or ctx.admin_store.get(admin_node)
        if password:
            return str(password)
        if ctx.json_output:
            raise DeviceCommandError(
                f"no admin password for {admin_node.name!r}; pass --password or run once "
                "interactively to store it."
            )
        entered = await ctx.ui.text(
            f"Admin password for {admin_node.name}:", password=True
        )
        if not entered:
            raise DeviceCommandError("an admin password is required to tune a remote node.")
        return entered

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``tx-optimize`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _tx_optimize(
            path: str = typer.Option(
                ..., "--path", "-p",
                help="Forced path ending at the target (e.g. 'Repeater,Target' or '3d,f2')",
            ),
            samples: int = typer.Option(3, "--samples", "-n", help="Traces per TX level"),
            step: int = typer.Option(3, "--step", help="Coarse sweep step"),
            tx_min: Optional[int] = typer.Option(None, "--min", help="Lowest TX power"),
            tx_max: Optional[int] = typer.Option(None, "--max", help="Highest TX power"),
            password: Optional[str] = typer.Option(
                None, "--password", help="Admin password (else remembered/prompted)"
            ),
            apply: bool = typer.Option(True, "--apply/--no-apply", help="Set the winner"),
            viz: bool = typer.Option(True, "--viz/--no-viz", help="Generate HTML chart"),
        ) -> None:
            tool_params: dict[str, Any] = {
                "path": path, "samples": samples, "step": step,
                "apply": apply, "viz": viz,
            }
            if tx_min is not None:
                tool_params["tx_min"] = tx_min
            if tx_max is not None:
                tool_params["tx_max"] = tx_max
            if password is not None:
                tool_params["password"] = password
            run_tool_command(self, tool_params)


def _resolve_link(path: str, contacts: list[Contact]) -> tuple[Contact, str]:
    """Split a forced path into the admin node we tune and the target's display label.

    Args:
        path: The comma-separated hash path (output of ``parse_trace_path``).
        contacts: Known contacts, used to map hashes back to names/keys.

    Returns:
        ``(admin_node, target_label)`` — the second-to-last hop as a full
        :class:`Contact` (needed for login), and a friendly name for the last hop.

    Raises:
        DeviceCommandError: If the path has fewer than two hops, or the admin hop can't
            be matched to a known contact carrying a public key.
    """
    hops = [h for h in path.split(",") if h]
    if len(hops) < 2:
        raise DeviceCommandError(
            "the path needs at least two hops: the node to tune and the target after it "
            "(e.g. 'AdminNode,Target')."
        )
    admin = _contact_for_hash(hops[-2], contacts)
    if admin is None or not (admin.public_key or "").strip():
        raise DeviceCommandError(
            f"the node before the target ({hops[-2]}) isn't a known contact with a public "
            "key, so we can't log in to tune it. Receive an advert from it first."
        )
    target = _contact_for_hash(hops[-1], contacts)
    return admin, (target.name if target else hops[-1])


def _contact_for_hash(hash_hex: str, contacts: list[Contact]) -> Optional[Contact]:
    """Return the contact whose key matches a path-hop hash, if any.

    Args:
        hash_hex: A path hop hash (a leading slice of the node's public key).
        contacts: Known contacts to match against.

    Returns:
        The matching :class:`Contact`, or ``None``.
    """
    needle = hash_hex.lower().removeprefix("0x")
    for c in contacts:
        pub = (c.public_key or "").lower().removeprefix("0x")
        prefix = (c.key_prefix or "").lower().removeprefix("0x")
        if pub.startswith(needle):
            return c
        if prefix and (prefix.startswith(needle) or needle.startswith(prefix)):
            return c
    return None


def _validate_link_path(value: str, contacts: list[Contact]) -> bool | str:
    """Validate the interactive path entry: parseable and at least two hops.

    Args:
        value: The raw comma-separated path entry.
        contacts: Known contacts used to resolve names.

    Returns:
        ``True`` if valid, otherwise an error message string.
    """
    if not value.strip():
        return "Enter a path ending at the target node."
    try:
        path = trace_runner.parse_trace_path(value, contacts)
    except ValueError as exc:
        return str(exc)
    if len([h for h in path.split(",") if h]) < 2:
        return "Need at least two hops: the node to tune, then the target."
    return True


def _fmt_snr(snr: Optional[float]) -> str:
    """Format an optional SNR for the progress description.

    Args:
        snr: SNR in dB, or ``None``.

    Returns:
        A short fixed-width string like ``+5.1`` or ``  n/a``.
    """
    return f"{snr:+.1f}" if snr is not None else " n/a"
