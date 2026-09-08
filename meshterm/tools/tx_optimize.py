"""The ``tx-optimize`` tool: tune a remote node's TX power for the best signal at a target.

You force a path (just like ``trace``) ending at the **target** node where SNR is
measured. The node one hop *before* the target — one you hold admin rights on — is the
node whose transmit power gets swept and tuned. Admin passwords are remembered between
runs.

In the interactive menu two pickers choose the link — the node to tune (repeaters
you hold credentials for lead the list), then the target whose reception is optimized —
and the live sweep screen (:mod:`meshterm.ui.tx_screen`) takes it from there, armed but
idle: route, range, step, and samples are adjusted in place, nothing transmits until
Sweep is committed, levels land in a bar chart as they are measured, and the apply
decision is made *after* the sweep, over the evidence. On the CLI it stays a scriptable
one-shot with the full flag set (``--path``, range, step, samples, ``--apply``), and
progress streams like a trace.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.connection import DeviceCommandError
from ..core.models import NODE_TYPE_LABELS, Contact, LoginResult, TxOptResult
from ..services import trace_runner, tx_optimizer
from ..ui.widgets import tx_opt_summary, tx_opt_table
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
    help = "Tune a remote node's TX power for a target"
    category = "Other nodes"
    order = 20  # like Repeater admin: a remote radio, changed over the mesh

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Nothing to gather here — both pickers live inside :meth:`run`.

        Each has to *stay pushed* while what it opens runs, so that Esc walks back down the
        entry flow one list at a time instead of dropping to the menu from wherever it is
        pressed. A prompt gathered here would resolve — and pop — before the tool ran, so
        the two pickers moved into :meth:`_run_live` with the loops that own them.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"live": True}`` — the menu's marker for the interactive path.
        """
        return {"live": True}

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
            params: ``live`` from the menu (both nodes are picked on their own
                screens); or ``path``,
                ``samples``, ``tx_min``, ``tx_max``, ``step``, ``apply``, optional
                ``password``/``viz``, and the injected ``_run_id`` from the CLI.

        Returns:
            A :class:`ToolResult` with the optimum and any chart path.
        """
        if params.get("live"):
            return await self._run_live(ctx)
        return await self._run_cli(ctx, params)

    # -- interactive (menu) ---------------------------------------------------------

    async def _run_live(self, ctx: AppContext) -> ToolResult:
        """Walk the entry flow as a stack: node list, target list, then the sweep screen.

        Both pickers **stay pushed** for the whole visit, so the flow reads back the way it
        was entered: Esc from the sweep lands on the *measure at* list it was launched from,
        Esc there lands on the node list, and Esc there leaves for the menu. Each list keeps
        its cursor, its filter and its scroll, because it is the same screen object
        throughout. They used to be one-shot prompts gathered in ``prompt_params``, both
        popped before the sweep opened — so a single Esc from anywhere in the flow landed on
        the main menu, and tuning a second target meant reopening the tool and re-picking
        the node.

        No login here: the screen logs in inside the first Sweep commit, so backing
        out of an idle screen never touched the radio beyond the contact reads.

        Args:
            ctx: Shared application context.

        Returns:
            A :class:`ToolResult` echoing the last sweep's recorded summary.
        """
        from ..ui.admin_picker import admin_node_visit

        # Through the session cache: this entry flow runs on every open, and the contacts
        # table is a slow read on a busy node (see
        # :class:`~meshterm.services.device_state.DeviceState`).
        contacts = await ctx.devstate.contacts()
        summary: dict[str, Any] = {}
        async with admin_node_visit(
            ctx,
            contacts,
            title="TX optimize — node to tune",
            prompt="Whose transmit power gets tuned (you need its admin password):",
        ) as picker:
            if picker is None:  # nothing offerable — the visit has already said so
                return ToolResult(summary={})
            while True:
                admin_node = await picker.pick()
                if admin_node is None:  # Esc — out to the menu
                    return ToolResult(summary=summary)
                summary = await self._measure_visit(ctx, contacts, admin_node) or summary

    async def _measure_visit(
        self, ctx: AppContext, contacts: list[Contact], admin_node: Contact
    ) -> dict[str, Any]:
        """Keep the *measure at* list pushed while sweeps run above it.

        The middle frame of the entry flow: one round per target, so sweeping the same tuned
        node against a second target is a pick, not a re-entry. Esc closes the list and hands
        back to the node picker underneath.

        Args:
            ctx: Shared application context.
            contacts: The device's known contacts.
            admin_node: The already-picked node whose power gets swept.

        Returns:
            The last sweep's summary, or an empty dict if none ran.
        """
        from ..ui.surface import TuiUi
        from ..ui.tui import SelectScreen
        from ..ui.tui.screen import CANCEL

        items = _target_items(contacts, admin_node)
        if not items or not isinstance(ctx.ui, TuiUi):
            # No other contact to measure at: a typed hex key prefix is the only way in, and
            # a one-shot prompt has no list under it to come back to.
            entered = await ctx.ui.text(
                "Target node",
                prompt=f"Name or hex key prefix of the node that hears {admin_node.name}:",
            )
            target = entered.strip() if entered else ""
            return await self._sweep(ctx, contacts, admin_node, target) if target else {}

        screen = SelectScreen(
            "TX optimize — measure at",
            items,
            prompt=f"The node whose reception of {admin_node.name} gets optimized:",
        )
        summary: dict[str, Any] = {}
        async with ctx.ui.session.stay(screen) as visit:
            while True:
                chosen = await visit.result()
                if chosen is CANCEL or chosen is None:  # Esc — back to the node list
                    return summary
                summary = await self._sweep(ctx, contacts, admin_node, str(chosen)) or summary

    @staticmethod
    async def _sweep(
        ctx: AppContext, contacts: list[Contact], admin_node: Contact, target: str
    ) -> dict[str, Any]:
        """Open the armed-idle sweep screen for one picked link and return its summary.

        Args:
            ctx: Shared application context.
            contacts: The device's known contacts (to resolve the target's key).
            admin_node: The node whose transmit power gets swept.
            target: The target's contact name, or a typed hex key prefix.

        Returns:
            The sweep screen's recorded summary (empty if nothing was swept).
        """
        from ..ui.tx_screen import open_tx_optimize

        target_contact = next((c for c in contacts if c.name == target), None)
        if target_contact is not None:
            target_label = target_contact.name
            target_hash = target_contact.public_key or target_contact.key_prefix
        else:
            target_label = target
            target_hash = target  # a typed hex prefix stands for itself

        return await open_tx_optimize(
            ctx,
            admin_node=admin_node,
            target_label=target_label,
            target_hash=target_hash,
        )

    async def _login(self, ctx: AppContext, admin_node: Contact, params: dict[str, Any]) -> None:
        """Authenticate against the admin node, remembering a working password.

        Args:
            ctx: Shared application context.
            admin_node: The node we're about to tune.
            params: Tool params (may carry an explicit ``password`` on the CLI).

        Raises:
            DeviceCommandError: If the node rejected the login (the stored password, now
                known bad, is forgotten so the next run asks fresh) — or if it never
                answered, in which case the password is untested and is kept.
        """
        device = await ctx.device()
        password = await self._resolve_password(ctx, admin_node, params)
        outcome = await device.admin_login(admin_node, password)
        ctx.admin_store.record(admin_node, password, outcome)
        if outcome is LoginResult.REFUSED:
            raise DeviceCommandError(
                f"admin login to {admin_node.name!r} failed (wrong password?). "
                "The saved password was cleared; re-run to enter a new one."
            )
        if not outcome:
            raise DeviceCommandError(
                f"{admin_node.name!r} did not answer the admin login — it may be out of "
                "reach or asleep. The saved password was kept; re-run when it answers."
            )

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
        from ..ui.surface import TuiUi

        run_id = params["_run_id"]
        device = await ctx.device()
        contacts = await device.get_contacts()

        # Resolve the forced path to hashes, then pull out the target (last hop) and the
        # admin node we tune (the hop before it).
        path = trace_runner.parse_trace_path(params["path"], contacts)
        admin_node, target_label = _resolve_link(path, contacts)

        samples = max(1, min(MAX_SAMPLES, int(params.get("samples", 3))))
        if int(params.get("samples", 3)) > MAX_SAMPLES:
            ctx.ui.ack(f"[warn]capping at {MAX_SAMPLES} traces per level[/warn]")

        ctx.ui.ack(
            f"[muted]logging in to[/muted] [brand]{admin_node.name}[/brand] [muted]…[/muted]"
        )
        await self._login(ctx, admin_node, params)

        ctx.ui.ack(
            f"[muted]tuning[/muted] [brand]{admin_node.name}[/brand] "
            f"[muted]→ target[/muted] [brand]{target_label}[/brand]  "
            f"[muted]via {path}[/muted]"
        )

        with ctx.ui.progress("tx-optimize") as progress:
            task = progress.add_task(f"optimizing TX -> {target_label}", total=None)

            def on_level(done: int, total: int, level) -> None:  # noqa: ANN001
                progress.update(
                    task,
                    total=total,
                    completed=done,
                    description=f"TX {level.tx_power:>2}  "
                    f"SNR {_fmt_snr(level.target_snr)}  {level.success_rate:.0%}",
                )

            result = await tx_optimizer.optimize_tx_power(
                device,
                target_label,
                admin_node,
                path,
                tx_min=int(params.get("tx_min", ctx.preferences.tx_opt_min)),
                tx_max=int(params.get("tx_max", ctx.preferences.tx_opt_max)),
                coarse_step=int(params.get("step", 3)),
                samples_per_level=samples,
                apply=bool(params.get("apply", True)),
                cooldown_s=ctx.preferences.trace_cooldown_s,
                snr_tolerance=ctx.preferences.tx_snr_tolerance_db,
                on_level=on_level,
                persist_level=lambda lv: ctx.repo.record_tx_sample(run_id, lv),
                persist_trace=lambda t: ctx.repo.record_trace(run_id, t),
            )

        # No trace got through at any TX level: nothing was tuned, the node was left at
        # its original power. Usually a wrong/unreachable path rather than a weak link.
        no_result = result.best_snr is None
        if result.applied:
            ctx.log.info("set TX power %s on %s", result.best_tx, admin_node.name)

        report = None
        if isinstance(ctx.ui, TuiUi):
            ctx.ui.show(tx_opt_table(result))
            ctx.ui.show(tx_opt_summary(result))
        else:
            report = _sweep_report(result, path, admin_node, contacts)

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
                f"[brand]{result.target}[/brand] is [brand]{result.best_tx}[/brand]" + applied_note
            )

        return ToolResult(
            report=report,
            # No level got a trace through, so nothing was measured and nothing was tuned:
            # the sweep ran and has nothing to report.
            exit_code=exitcodes.NO_RESULT if no_result else exitcodes.OK,
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
        # This resolver only runs on the scripted CLI path (the menu logs in inside the pushed
        # sweep screen), so the surface here is PlainUi — no screen stack, no floating.
        entered = await ctx.ui.text(f"Admin password for {admin_node.name}:", password=True)
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
                ...,
                "--path",
                "-p",
                help="Forced path ending at the target (e.g. 'Repeater,Target' or '3d,f2')",
            ),
            samples: int = typer.Option(3, "--samples", "-n", help="Traces per TX level"),
            step: int = typer.Option(3, "--step", help="Coarse sweep step"),
            tx_min: int | None = typer.Option(None, "--min", help="Lowest TX power"),
            tx_max: int | None = typer.Option(None, "--max", help="Highest TX power"),
            password: str | None = typer.Option(
                None, "--password", help="Admin password (else remembered/prompted)"
            ),
            apply: bool = typer.Option(True, "--apply/--no-apply", help="Set the winner"),
        ) -> None:
            tool_params: dict[str, Any] = {
                "path": path,
                "samples": samples,
                "step": step,
                "apply": apply,
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


def _contact_for_hash(hash_hex: str, contacts: list[Contact]) -> Contact | None:
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


def _target_items(contacts: list[Contact], admin: Contact) -> list:
    """The *measure at* list's rows, ordered by how recently each node was heard.

    Rows carry the contact's **name** as their value, since a typed hex prefix stands in the
    same place (see :meth:`TxOptimizeTool._sweep`). Empty when the tuned node is the only
    contact there is — the caller falls back to a typed prefix.

    Args:
        contacts: The device's known contacts.
        admin: The already-picked tuned node (excluded — it can't measure itself).

    Returns:
        One :class:`~meshterm.ui.tui.select.Choice` per other contact, freshest first.
    """
    from rich.text import Text

    from ..ui.theme import name_style
    from ..ui.tui import Choice
    from ..ui.widgets import _DEFAULT_GLYPH, _NODE_GLYPHS

    def row(contact: Contact) -> Any:
        # The type mark keeps its own fixed hue; the *name* takes the node's key-derived
        # colour like every other list of nodes (a style on the Text itself would be the
        # row's base and would paint the name the type's colour too).
        glyph, glyph_style = _NODE_GLYPHS.get(contact.node_type, _DEFAULT_GLYPH)
        label = Text()
        label.append(f"{glyph} ", style=glyph_style)
        label.append(
            contact.name,
            style=name_style(contact.name, contact.public_key or contact.key_prefix),
        )
        return Choice(title=label, value=contact.name)

    return [
        row(c)
        for c in sorted(
            (c for c in contacts if c.name != admin.name),
            key=lambda c: -(c.last_seen.timestamp() if c.last_seen else 0.0),
        )
    ]


def _fmt_snr(snr: float | None) -> str:
    """Format an optional SNR for the progress description.

    Args:
        snr: SNR in dB, or ``None``.

    Returns:
        A short fixed-width string like ``+5.1`` or ``  n/a``.
    """
    return f"{snr:+.1f}" if snr is not None else " n/a"


def _sweep_report(
    result: TxOptResult, path: str, admin_node: Contact, contacts: list[Contact]
) -> tuple:
    """State a TX sweep: the outcome, then every level measured.

    The winner comes first, because it is what the command was asked for and what a caller
    acts on; the per-level records follow, so the choice can be checked against the
    measurements it was made from. The menu's ``★`` on the winning row has no column here
    — ``optimal_tx_dbm`` above the table already names it, and a mark is something to look
    at rather than something to test.

    The two nodes become the shared node shape, and here that matters more than anywhere
    else: on the plain line they are told apart by nothing but their names, and a caller
    correlating a sweep with a trace has no key to join on.

    Args:
        result: The completed sweep.
        path: The forced route the traces walked, as hex hops.
        admin_node: The node whose TX power was tuned.
        contacts: The contact list, for placing the target by name.

    Returns:
        The report's blocks.
    """
    from ..ui import fields
    from ..ui.fields import NodeRef
    from ..ui.report import Facts, Listing

    def node(name: str) -> NodeRef | None:
        """One end of the tuned link as the shared node shape."""
        contact = next((c for c in contacts if c.name == name), None)
        if contact is None:
            return NodeRef(name=name) if name else None
        return NodeRef(
            name=contact.name,
            key=(contact.public_key or "").lower() or None,
            hash=(contact.key_prefix or "").lower() or None,
            type=NODE_TYPE_LABELS.get(contact.node_type),
        )

    facts = Facts(
        key="tx_optimize",
        fields=(
            fields.node("tuning_node", lanes=(("name", "tuning_node"),)),
            fields.node("target", lanes=(("name", "target"),)),
            fields.spec(),
            fields.integer("optimal_tx_dbm", "optimal_tx_dbm"),
            fields.snr("target_snr_db", "target_snr_db"),
            fields.decimal("reliability", "reliability", ".2f"),
            fields.integer("previous_tx_dbm", "previous_tx_dbm"),
            fields.flag("applied", "applied"),
        ),
        values={
            "tuning_node": node(result.admin_node) or NodeRef(name=admin_node.name),
            "target": node(result.target),
            "path": path,
            "optimal_tx_dbm": result.best_tx,
            "target_snr_db": result.best_snr,
            # A fraction in [0, 1], not a formatted "1.00": the plain column rounds for
            # the eye and the document keeps the number a number.
            "reliability": result.best_success_rate,
            "previous_tx_dbm": result.original_tx,
            "applied": result.applied,
        },
    )
    levels = Listing(
        key="levels",
        columns=(
            fields.integer("tx_dbm", "TX_DBM"),
            fields.snr("target_snr_db", "TARGET_SNR_DB"),
            fields.integer("successes", "SUCCESSES"),
            fields.integer("samples", "SAMPLES"),
        ),
        rows=[
            {
                "tx_dbm": level.tx_power,
                "target_snr_db": level.target_snr,
                "successes": level.successes,
                "samples": level.samples,
            }
            for level in result.sorted_by_tx()
        ],
    )
    return (facts, levels)
