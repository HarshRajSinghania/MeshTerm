"""The trace tools: path traces, live in the menu, one-shot on the CLI.

A trace is one walked path — the protocol has no destination field, so "target" is
purely a UX notion — and the menu splits the feature along exactly that line:

* **Trace target** (``trace``): *can I reach this node?* Pick a target from a
  recency-ordered list; the live screen composes/explores symmetric routes that turn
  at the target and come home over the mirrored hops. The list stays pushed underneath
  for the whole visit, so Esc from a walk lands back on the row that launched it —
  freshly re-sorted by what was just traced — and the next node is one Enter away.
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
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.models import (
    LOCAL_DEVICE_LABEL,
    PATH_TRACE_TARGET,
    Contact,
    TraceResult,
    TraceStats,
)
from ..services import trace_runner
from ..ui.widgets import stats_panel
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.contactlist import ContactListScreen, ContactRow

#: The characters a stored trace target must consist of to be treated as a hex key
#: prefix when folding it back to a contact name in the target picker.
_HEX_DIGITS = frozenset("0123456789abcdef")


@register
class TraceTool(Tool):
    """Trace the path to a target and watch per-hop SNR, live or scripted."""

    name = "trace"
    title = "Trace target"
    icon = "🎯"
    help = "Trace a target and watch per-hop SNR"
    category = "Explore"
    order = 30  # see a node above, walk to it here

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Nothing to gather here — the target picker lives inside :meth:`run`.

        The pick and the screen it opens share the contacts read and the routing width,
        and the live path skips the base class's run row (see :meth:`execute`), so the
        whole entry flow sits in :meth:`_run_live` rather than half of it here.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"live": True}`` — the menu's marker for the interactive path.
        """
        return {"live": True}

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
            params: ``live`` from the menu (the target is picked on the screen); or
                ``target`` (str), optional ``path``, and the injected ``_run_id``
                from the CLI.

        Returns:
            A :class:`ToolResult` with the trace's outcome.
        """
        if params.get("live"):
            return await self._run_live(ctx)
        return await self._run_cli(ctx, params)

    # -- interactive (menu) -------------------------------------------------------

    async def _run_live(self, ctx: AppContext) -> ToolResult:
        """Ask for the target in a popup, then run the live screen over the menu.

        The picker is a *question*, not a place: a floating list over the menu, gone the
        moment a target is picked, so the trace screen opens straight over the menu and
        Esc from it lands there. It used to stay pushed as a hub under the trace screen —
        Esc out of a trace landed back on the list, which read as two places where the
        trace is the whole visit; tracing another node is opening the tool again.

        Args:
            ctx: Shared application context.

        Returns:
            A :class:`ToolResult` counting the screen opened and the traces run.
        """
        from ..ui.trace_screen import open_trace
        from ..ui.tui.screen import CANCEL

        picker = await self._build_picker(ctx)
        if picker is None:
            # Nothing to list (or no full-screen session): the target is typed instead.
            target = await self._prompt_target(ctx)
        else:
            chosen = await ctx.ui.session.run_dialog(picker)
            target = None if chosen is CANCEL or chosen is None else chosen.name
        if target is None:
            return ToolResult(summary={})
        return ToolResult(summary={"sessions": 1, "traces": await open_trace(ctx, target)})

    async def _prompt_target(self, ctx: AppContext) -> str | None:
        """Ask for a target by hand — the way in when there is no list to pick from.

        Reached with no known contacts at all (an empty list would be nothing to pick from)
        and on any surface without a full-screen session. It is also the only way to trace a
        bare key prefix for a node the device doesn't carry as a contact. A question like
        the list it stands in for, so it floats over the menu rather than filling the frame.

        Args:
            ctx: Shared application context.

        Returns:
            The typed target, or ``None`` if it was left blank or cancelled.
        """
        entered = await ctx.ui.text("Target node (name or key prefix):", floating=True)
        return entered.strip() if entered else None

    async def _build_picker(self, ctx: AppContext) -> ContactListScreen | None:
        """Build the trace-target picker, or ``None`` when there is no list to draw.

        The same ``NAME · TRACED · HEARD · PKTS · KEY`` lanes, Ctrl+arrow sort ring, and
        type-to-filter the Contacts screen and the courier recipient draw — but with every
        node type listed (a trace answers *can I reach this node?* for a repeater or room
        just as for a companion) and an extra ``TRACED`` lane: how long ago each node was
        last traced. The list opens sorted by ``TRACED`` descending, so the most recently
        traced node leads and never-traced ones gather at the bottom. Enter commits the
        highlighted node as the target.

        Args:
            ctx: Shared application context.

        Returns:
            The picker, or ``None`` when there is nothing to list — see
            :meth:`_prompt_target`.
        """
        from ..ui.contactlist import (
            TRACE_LANES,
            TRACE_SORT_COLUMNS,
            TRACE_SORT_OPENS_ASCENDING,
            ContactListScreen,
        )
        from ..ui.surface import TuiUi
        from ..ui.widgets import ContactsSort

        # Through the session cache: this picker runs on every Trace open, and the contacts
        # table is a slow round-trip on a busy node — re-reading it here (in front of the
        # already-cached trace screen) is what kept opening Trace feeling like a stall. See
        # :class:`~meshterm.services.device_state.DeviceState`.
        contacts = await ctx.devstate.contacts()
        if not contacts or not isinstance(ctx.ui, TuiUi):
            return None

        picker = ContactListScreen(
            "Trace target — pick a target",
            rows=self._picker_rows(ctx, contacts),
            prefix_bytes=await ctx.devstate.routing_prefix_bytes(),
            sort=ContactsSort.from_name("traced", TRACE_SORT_COLUMNS, TRACE_SORT_OPENS_ASCENDING),
            footer_hint="↑↓ move · ^←→↑↓ sort · type to filter · Enter select · Esc back",
            lanes=TRACE_LANES,
        )
        # The contact list is a full-screen page everywhere else; here it is a question
        # asked on the way in, so it floats over the menu like every other lead-in pick.
        picker.floating = True
        return picker

    @staticmethod
    def _picker_rows(ctx: AppContext, contacts: list[Contact]) -> list[ContactRow]:
        """The picker's lane data for ``contacts``, read fresh from stored history.

        Both the ``TRACED`` ages and the packet counts come from the repository, so this is
        cheap enough to redo every time a trace hands the picker back — which is what keeps
        the lane the list is *sorted* by honest about the walk that just happened.

        Args:
            ctx: Shared application context.
            contacts: The device's contacts, as listed.

        Returns:
            One :class:`~meshterm.ui.contactlist.ContactRow` per contact, in the order
            given (the screen sorts them).
        """
        from ..ui.contactlist import ContactRow
        from ..ui.widgets import contact_packets

        traced = _last_traced_by_name(contacts, ctx.repo.target_last_traced())
        counts = {n.node: n.count for n in ctx.repo.heard_nodes() if n.node}
        return [
            ContactRow(
                value=c,
                name=c.name,
                key=c.public_key or c.key_prefix or "",
                node_type=c.node_type,
                last_seen=c.last_seen,
                count=contact_packets(c, counts),
                last_traced=traced.get(c.name),
            )
            for c in contacts
        ]

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

        @app.command(
            name=self.name, help="Run a single path trace to a target and show per-hop SNR"
        )
        def _trace(
            target: str = typer.Option(..., "--target", "-t", help="Target node name/prefix"),
            path: str | None = typer.Option(
                None,
                "--path",
                # No ``-p`` short form: ``-p`` is the global ``--profile``, and
                # ``_globals_first`` lifts a group option ahead of the subcommand wherever it
                # is typed — so a leaf ``-p`` could never reach this option, only shadow it.
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
    help = "Walk a route you compose, hop by hop"
    category = "Explore"
    order = 32

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
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
                # No ``-p`` short form: ``-p`` is the global ``--profile``, and
                # ``_globals_first`` lifts a group option ahead of the subcommand wherever it
                # is typed — so a leaf ``-p`` could never reach this option, only shadow it.
                help="The whole walk: comma-separated contact names/hex prefixes "
                "(must end within earshot of this node)",
            ),
        ) -> None:
            run_tool_command(self, {"path": path})


async def _trace_once_cli(
    ctx: AppContext, run_id: int, *, target: str, path_spec: str | None
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
    from ..ui.surface import TuiUi

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
    path: str | None = None
    if path_spec:
        path = trace_runner.parse_trace_path(path_spec, contacts)

    with ctx.ui.progress("trace") as progress:
        task = progress.add_task("walking the path" if is_walk else f"tracing {target}", total=1)
        result = await device.run_trace(target, path=path)
        ctx.repo.record_trace(run_id, result)
        progress.advance(task)

    stats = TraceStats.from_traces(target, [result])
    if isinstance(ctx.ui, TuiUi):
        # The stats panel renders route + per-hop readings for the single trace (its
        # medians collapse to the readings themselves).
        ctx.ui.show(
            stats_panel(
                stats,
                device_label,
                resolve,
                route=result if result.success else None,
                device_hash=device_hash,
            )
        )
    report = None
    if not isinstance(ctx.ui, TuiUi):
        report = _trace_report(result, target, path, device_label, resolve, device_hash)

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
        message = (
            f"[ok]✓[/ok] the path came home{via}"
            if result.success
            else f"[err]✗[/err] no reply{via}"
        )
    elif result.success:
        message = f"[ok]✓[/ok] traced [brand]{target}[/brand]{via}"
    else:
        message = f"[err]✗[/err] no reply from [brand]{target}[/brand]{via}"
    # A trace that never came home is not a failure of the command — the radio did
    # transmit and the walk did run. It is a walk with nothing to report, which is what
    # NO_RESULT says, and it is the answer a script most often branches on.
    return ToolResult(
        summary=summary,
        message=message,
        report=report,
        exit_code=exitcodes.OK if result.success else exitcodes.NO_RESULT,
    )


def _trace_report(
    result: TraceResult,
    target: str,
    path: str | None,
    device_label: str,
    resolve: Any,
    device_hash: str | None,
) -> tuple:
    """State one trace: the walk's outcome, then its per-hop readings.

    Two blocks. The first is what the walk *did* — one fact per line, ``route`` among them
    as a drawn line, every node named and carrying the hash it was addressed by. The
    second is a record per hop, and it names its two ends by that same **hash** rather than
    repeating the names: the route line above is where the names are, and the hash is what
    joins the two blocks (it is what carries a node's identity here, the way its colour
    does on a screen). The document embeds the whole node at both ends instead, because a
    structural join needs no key and each edge should be readable on its own.

    Our own node is a hop like any other, named and hashed. The menu draws it as ``★``
    because a reader never has to be told which node is theirs; this line is as often read
    out of a file by somebody who was not at the prompt when it ran.

    Args:
        result: The trace that ran.
        target: The label it was addressed to.
        path: The forced route as hex, or ``None`` when the device routed it.
        device_label: Our own node's name, at both ends of the walk.
        resolve: Maps a hop hash to a friendly name when known.
        device_hash: Our own public key, so our ends carry a hash like every other hop.

    Returns:
        The report's blocks.
    """
    from ..ui import fields
    from ..ui.fields import NodeRef
    from ..ui.report import Facts, Listing

    hash_bytes = result.path_hash_bytes

    def short(value: str | None) -> str | None:
        """A hash at the width this trace addressed nodes by."""
        if not value:
            return None
        raw = value.lower().removeprefix("0x")
        return raw[: hash_bytes * 2] if hash_bytes else raw

    def node(label: str) -> NodeRef:
        """One end of a hop as the shared node shape."""
        if not label or label == device_label:
            return NodeRef(name=device_label, hash=short(device_hash), is_self=True)
        return NodeRef(name=resolve(label) or None, hash=short(label))

    edges = result.edges(device_label)
    route = None
    if edges:
        route = [node(edges[0].origin)] + [node(edge.destination) for edge in edges]

    facts = Facts(
        key="trace",
        fields=(
            fields.word("target", "target"),
            fields.spec(),
            fields.flag("success", "success"),
            fields.integer("hops", "hops"),
            fields.snr("min_snr_db", "min_snr_db"),
            fields.decimal("rtt_ms", "rtt_ms", ".0f"),
            # New surface on the machine face: the model has always carried the TX power
            # the walk ran at, and the plain facts block has no room for it.
            fields.hidden("tx_dbm"),
            # What `hash` means on this walk's nodes, so a consumer can join two traces
            # that addressed the same node at different widths.
            fields.hidden("hash_bytes"),
            fields.route("route", "route"),
        ),
        values={
            "target": target if target != PATH_TRACE_TARGET else None,
            "path": path,
            "success": result.success,
            "hops": result.hop_count if result.success else None,
            "min_snr_db": result.min_snr,
            "rtt_ms": result.round_trip_ms,
            "tx_dbm": result.tx_power,
            "hash_bytes": hash_bytes,
            "route": route,
        },
    )
    hops = Listing(
        key="edges",
        columns=(
            fields.integer("index", "HOP"),
            fields.node("from", lanes=(("hash", "FROM"),)),
            fields.node("to", lanes=(("hash", "TO"),)),
            fields.snr("snr_db", "SNR_DB"),
        ),
        rows=[
            {
                "index": edge.index,
                "from": node(edge.origin),
                "to": node(edge.destination),
                "snr_db": edge.snr,
            }
            for edge in edges
        ],
    )
    return (facts, hops)


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
