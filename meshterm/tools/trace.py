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

from typing import Any, Optional

import typer
from rich.text import Text

from ..context import AppContext
from ..core.models import LOCAL_DEVICE_LABEL, PATH_TRACE_TARGET, Contact, TraceStats
from ..services import trace_runner
from ..ui.widgets import stats_panel
from .base import Tool, ToolResult, register

_BACK = "__back__"

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
        """Pick a trace target: recently traced first, then contacts by recency.

        Args:
            ctx: Shared application context.

        Returns:
            The chosen target name, or ``None`` if cancelled. With no known contacts and
            no history, falls back to a free-text prompt so a key prefix can be typed.
        """
        from ..ui.tui import Choice, Separator
        from ..ui.widgets import _DEFAULT_GLYPH, _NODE_GLYPHS

        # Through the session cache: this picker runs on every Trace open, and the contacts
        # table is a slow round-trip on a busy node — re-reading it here (in front of the
        # already-cached trace screen) is what kept opening Trace feeling like a stall. See
        # the note in the ``nodes`` tool and :class:`~meshterm.services.device_state.DeviceState`.
        contacts = await ctx.devstate.contacts()
        recent = _recent_targets(ctx.repo.traced_targets(), contacts)

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
            "Trace target — pick a target", items, wrap=False
        )
        if choice in (None, _BACK):
            return None
        return str(choice)

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


def _recent_targets(stored: list[str], contacts: list[Contact]) -> list[str]:
    """Clean the stored recent-target names for the picker's *Recently traced* section.

    Targets are recorded exactly as the user addressed them, so the same node can
    appear once as a contact name and again as a raw hex prefix typed some other day.
    Each stored target is folded back to its contact's current name when it matches one
    (by name, case-insensitively, or as a prefix of a contact's public key), then
    deduplicated with order preserved — so every node shows once, under the name the
    rest of the picker uses.

    Args:
        stored: Recent trace destinations, most recent first, as recorded.
        contacts: The device's current contacts to fold names against.

    Returns:
        The display names, most recently traced first, one per node.
    """
    by_fold = {c.name.casefold(): c.name for c in contacts}

    def fold(target: str) -> str:
        named = by_fold.get(target.casefold())
        if named is not None:
            return named
        needle = target.lower().removeprefix("0x")
        # Only fold plausible key prefixes (≥2 bytes of hex) — a short hex-looking
        # *name* like "ace" must not be mistaken for an address.
        if len(needle) >= 4 and all(ch in _HEX_DIGITS for ch in needle):
            for c in contacts:
                if (c.public_key or "").lower().startswith(needle):
                    return c.name
        return target

    names: list[str] = []
    seen: set[str] = set()
    for target in stored:
        name = fold(target)
        if name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    return names


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
