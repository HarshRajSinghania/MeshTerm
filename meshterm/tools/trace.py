"""The trace tools: path traces, live in the menu, one-shot on the CLI.

A trace is one walked path — the protocol has no destination field, so "target" is
purely a UX notion — and the menu splits the feature along exactly that line:

* **Trace target** (``trace``): *can I reach this node?* Pick a target from a
  recency-ordered list; the live screen composes/explores symmetric routes that turn
  at the target and come home over the mirrored hops.
* **Trace path** (``trace-path``): *how far can a route I build carry?* No target and
  no picker — the whole walk is composed hop by hop and only has to end within our
  own earshot. Traces record under the ``(path)`` sentinel, keeping composed walks
  out of the target picker's history.

Both open the live trace screen (:mod:`meshterm.ui.trace_screen`) armed but idle:
nothing transmits until Trace is committed, which runs the chosen sample count
(1–8 traces, paced between transmissions — repeaters penalize, and can blacklist,
nodes that burst traffic, so multi-trace runs always sleep a cooldown between sends).
On the CLI each stays a scriptable one-shot: run one trace, print the route and
per-hop SNR, exit.

Both front ends persist identically: one ``runs`` row per trace, recorded under it, so
stored history reads the same no matter where it came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import typer
from rich.text import Text

from ..context import AppContext
from ..core.models import LOCAL_DEVICE_LABEL, PATH_TRACE_TARGET, Contact, TraceStats
from ..services import trace_runner
from ..ui.widgets import stats_panel
from .base import Tool, ToolResult, register

#: The characters a stored trace target must consist of to be treated as a hex key
#: prefix when folding it back to a contact name in the target picker.
_HEX_DIGITS = frozenset("0123456789abcdef")


@register
class TraceTool(Tool):
    """Trace the path to a target and watch per-hop SNR, live or scripted."""

    name = "trace"
    title = "Trace target"
    icon = "🎯"
    help = "Trace a target over a mirrored route and watch per-hop SNR"
    category = "Tools"
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

        The live screen opens one ``runs`` row *per trace* (matching what a scripted
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
        """Open the live screen (menu) or run one persisted trace (CLI).

        Args:
            ctx: Shared application context.
            params: ``live`` + ``target`` from the menu picker; or ``target`` (str),
                optional ``path``, and the injected ``_run_id`` from the CLI.

        Returns:
            A :class:`ToolResult` with the trace's outcome.
        """
        if params.get("live"):
            return await self._run_live(ctx, str(params["target"]))
        return await self._run_cli(ctx, params)

    # -- interactive (menu) -------------------------------------------------------

    async def _run_live(self, ctx: AppContext, target: str) -> ToolResult:
        """Open the live screen for one target; backing out drops to the main menu.

        Args:
            ctx: Shared application context.
            target: The target to open.

        Returns:
            A :class:`ToolResult` counting the traces run.
        """
        from ..ui.trace_screen import open_trace

        traces = await open_trace(ctx, target)
        return ToolResult(summary={"sessions": 1, "traces": traces})

    async def _pick_target(self, ctx: AppContext) -> Optional[str]:
        """Pick a trace target from the shared sortable contact list.

        The same ``NAME · TRACED · HEARD · PKTS · KEY`` lanes, Ctrl+arrow sort ring, and
        type-to-filter the Contacts screen and the courier recipient draw — but with every
        node type listed (a trace answers *can I reach this node?* for a repeater or room
        just as for a companion) and an extra ``TRACED`` lane: how long ago each node was
        last traced. The list opens sorted by ``TRACED`` descending, so the most recently
        traced node leads and never-traced ones gather at the bottom. Enter commits the
        highlighted node's name as the target.

        With no known contacts at all the list would be empty, so a free-text prompt takes
        over instead — also the only way to trace a bare key prefix for a node the device
        doesn't carry as a contact.

        Args:
            ctx: Shared application context.

        Returns:
            The chosen target name, or ``None`` if cancelled.
        """
        from ..ui.contactlist import (
            TRACE_SORT_COLUMNS,
            TRACE_SORT_OPENS_ASCENDING,
            ContactListScreen,
            ContactRow,
        )
        from ..ui.surface import TuiUi
        from ..ui.timemachine_screen import _routing_prefix_bytes
        from ..ui.widgets import ContactsSort, _contact_pkts

        # Through the session cache: this picker runs on every Trace open, and the contacts
        # table is a slow round-trip on a busy node — re-reading it here (in front of the
        # already-cached trace screen) is what kept opening Trace feeling like a stall. See
        # :class:`~meshterm.services.device_state.DeviceState`.
        contacts = await ctx.devstate.contacts()
        if not contacts or not isinstance(ctx.ui, TuiUi):
            # No contacts to list (or no full-screen session): a free-text prompt is the
            # only way in — and the only way to trace a bare key prefix off-list.
            entered = await ctx.ui.text("Target node (name or key prefix):")
            return entered.strip() if entered else None

        traced = _last_traced_by_name(contacts, ctx.repo.target_last_traced())
        counts = {n.node: n.count for n in ctx.repo.heard_nodes() if n.node}
        prefix_bytes = await _routing_prefix_bytes(ctx)
        rows = [
            ContactRow(
                value=c,
                name=c.name,
                key=c.public_key or c.key_prefix or "",
                node_type=c.node_type,
                last_seen=c.last_seen,
                count=_contact_pkts(c, counts),
                last_traced=traced.get(c.name),
            )
            for c in contacts
        ]
        picker = ContactListScreen(
            "Trace target — pick a target",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=ContactsSort.from_name(
                "traced", TRACE_SORT_COLUMNS, TRACE_SORT_OPENS_ASCENDING
            ),
            footer_hint="↑↓ move · ^←→↑↓ sort · type to filter · Enter select · Esc back",
            show_traced=True,
        )
        chosen = await ctx.ui.session.run_screen(picker)
        return chosen.name if isinstance(chosen, Contact) else None

    # -- scripted (CLI) -------------------------------------------------------------

    async def _run_cli(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run one trace, persist it, and print the route and per-hop summary.

        One transmission per invocation is a hard rule on the CLI — repeaters
        penalize (and can blacklist) nodes that burst traffic — so scripted sampling
        means invoking again (with your own pacing between runs).

        Args:
            ctx: Shared application context.
            params: ``target`` (str), optional ``path``, and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the trace's outcome.
        """
        return await _trace_once_cli(
            ctx, params["_run_id"], target=params["target"], path_spec=params.get("path")
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``trace`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help="Run a single path trace to a target and show per-hop SNR")
        def _trace(
            target: str = typer.Option(..., "--target", "-t", help="Target node name/prefix"),
            path: Optional[str] = typer.Option(
                None,
                "--path",
                "-p",
                help="Force a route: comma-separated contact names/hex prefixes (e.g. 3d,f2,3d)",
            ),
        ) -> None:
            tool_params: dict[str, Any] = {"target": target}
            if path:
                tool_params["path"] = path
            run_tool_command(self, tool_params)


@register
class TracePathTool(Tool):
    """Walk a hand-composed route — no target — and see how far it carries."""

    name = "trace-path"
    title = "Trace path"
    icon = "👣"
    help = "Walk a hand-composed route — out and back your way"
    category = "Tools"
    order = 11

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """No parameters to gather — a path walk has no target to pick.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"live": True}`` — the route itself is composed on the screen.
        """
        return {"live": True}

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run — without the outer run row on the live path (see :class:`TraceTool`).

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
        """Open the live path-walk screen (menu) or run one persisted trace (CLI).

        Args:
            ctx: Shared application context.
            params: ``live`` from the menu; or ``path`` (str, required) and the
                injected ``_run_id`` from the CLI.

        Returns:
            A :class:`ToolResult` with the walk's outcome.
        """
        if params.get("live"):
            from ..ui.trace_screen import open_trace_path

            traces = await open_trace_path(ctx)
            return ToolResult(summary={"traces": traces})
        return await _trace_once_cli(
            ctx, params["_run_id"], target=PATH_TRACE_TARGET, path_spec=params["path"]
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``trace-path`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name,
            help="Walk a composed route once (no target) and show per-hop SNR",
        )
        def _trace_path(
            path: str = typer.Option(
                ...,
                "--path",
                "-p",
                help="The whole walk: comma-separated contact names/hex prefixes "
                "(must end within earshot of this node)",
            ),
        ) -> None:
            run_tool_command(self, {"path": path})


async def _trace_once_cli(
    ctx: AppContext, run_id: int, *, target: str, path_spec: Optional[str]
) -> ToolResult:
    """Run one persisted trace and print the route and per-hop summary (CLI body).

    Shared by both scripted trace commands: ``trace`` passes its target (path
    optional — the device routes without one), ``trace-path`` passes the
    :data:`~meshterm.core.models.PATH_TRACE_TARGET` sentinel with a required path.

    Args:
        ctx: Shared application context.
        run_id: The already-open run row to record under.
        target: The label the trace persists under.
        path_spec: The forced route as typed (names/hex, comma-separated), if any.

    Returns:
        A :class:`ToolResult` with the trace's outcome.
    """
    is_walk = target == PATH_TRACE_TARGET
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
    # Leaving it blank is fully supported for a *target* trace: the device reuses the
    # route it already learned (or floods if it has none). A path walk always has one.
    path: Optional[str] = None
    if path_spec:
        path = trace_runner.parse_trace_path(path_spec, contacts)
        ctx.ui.note(f"[muted]forcing path:[/muted] [brand]{path}[/brand]")
    else:
        ctx.ui.note("[muted]path: auto (device-routed)[/muted]")

    with ctx.ui.progress("trace") as progress:
        task = progress.add_task(
            "walking the path" if is_walk else f"tracing {target}", total=1
        )
        result = await device.run_trace(target, path=path)
        ctx.repo.record_trace(run_id, result)
        progress.advance(task)

    # The stats panel renders route + per-hop readings for the single trace (its
    # medians collapse to the readings themselves).
    stats = TraceStats.from_traces(target, [result])
    ctx.ui.show(
        stats_panel(
            stats,
            device_label,
            resolve,
            route=result if result.success else None,
            device_hash=device_hash,
        )
    )

    summary: dict[str, Any] = {
        "target": target,
        "success": result.success,
        "hops": result.hop_count if result.success else None,
        "min_snr": result.min_snr,
        "rtt_ms": result.round_trip_ms,
    }
    if path:
        summary["path"] = path
    via = f" via [brand]{path}[/brand]" if path else ""
    if is_walk:
        if result.success:
            message = f"[ok]✓[/ok] the path came home{via}"
        else:
            message = f"[err]✗[/err] no reply{via}"
    elif result.success:
        message = f"[ok]✓[/ok] traced [brand]{target}[/brand]{via}"
    else:
        message = f"[err]✗[/err] no reply from [brand]{target}[/brand]{via}"
    return ToolResult(summary=summary, message=message)


def _last_traced_by_name(
    contacts: list[Contact], traced: dict[str, datetime]
) -> dict[str, datetime]:
    """Map each contact's name to when that node was last traced.

    The Trace picker's ``TRACED`` lane and default sort read this. Stored trace targets are
    filed exactly as the user addressed them — a contact name one day, a raw hex key prefix
    another — so each target is folded onto the contact it names (by name, case-insensitively,
    or as a prefix of a contact's public key) and the *latest* trace time wins, so a node
    traced under both spellings still shows one honest "last traced" age. This is the inverse
    of the picker's old recent-targets fold, kept per-contact rather than as a name list.

    Args:
        contacts: The device's current contacts.
        traced: ``target → last-traced time`` from
            :meth:`~meshterm.persistence.repository.Repository.target_last_traced`.

    Returns:
        ``contact name → last-traced time`` for every contact the history can place.
    """
    by_fold = {c.name.casefold(): c.name for c in contacts}
    out: dict[str, datetime] = {}

    def note(name: str, when: datetime) -> None:
        current = out.get(name)
        if current is None or when > current:
            out[name] = when

    for target, when in traced.items():
        named = by_fold.get(target.casefold())
        if named is not None:
            note(named, when)
            continue
        needle = target.lower().removeprefix("0x")
        # Only fold plausible key prefixes (≥2 bytes of hex) — a short hex-looking *name*
        # like "ace" must not be mistaken for an address.
        if len(needle) >= 4 and all(ch in _HEX_DIGITS for ch in needle):
            for contact in contacts:
                if (contact.public_key or "").lower().startswith(needle):
                    note(contact.name, when)
    return out
