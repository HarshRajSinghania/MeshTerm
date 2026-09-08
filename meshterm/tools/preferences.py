"""The ``preferences`` tool: how MeshTerm itself behaves.

Interactively it opens the Preferences page (see :mod:`meshterm.ui.preferences`), which
stages changes and hands them back here to write. On the CLI it exposes the same
preferences as ``show`` / ``get`` / ``set`` / ``reset``, so a value can be changed from a
shell — or from a script setting a machine up — without launching the full-screen session.

Everything funnels through :meth:`PreferencesTool.run`, which applies a list of operations
and saves the file once at the end: one write per invocation, whether it came from the page
staging eleven changes or from a single ``preferences set``.

This is deliberately the *app's* tool, not the radio's. It reads nothing from the
companion, transmits nothing, and works with no device attached at all — which is why it
leads the **This app** section (the third scope, after this node and other nodes) rather
than sitting beside Device config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core.preferences import (
    PREFERENCES,
    PreferenceError,
    Preferences,
    PrefSpec,
    format_value,
    get_spec,
)
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.table import Table


@register
class PreferencesTool(Tool):
    """View and change MeshTerm's own preferences, saved to ``preferences.yaml``."""

    name = "preferences"
    title = "Preferences"
    icon = "⚙"
    help = "How MeshTerm behaves — startup, sending, history, …"
    category = "This app"
    order = 5  # leads the section: the one row there that changes MeshTerm, not describes it

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the Preferences page and collect the changes it staged.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"ops": [("set", key, value), …]}`` to write, or ``None`` if the reader left
            with nothing staged.
        """
        from ..ui.preferences import edit_preferences

        staged = await edit_preferences(ctx)
        if not staged:
            return None
        return {"ops": [("set", key, value) for key, value in staged.items()]}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Apply a list of preference operations, saving the file once if anything changed.

        Operations are ``("show",)``, ``("get", key)``, ``("set", key, value)``, and
        ``("reset",)``. They run in order, so a scripted ``set`` after a ``reset`` lands on
        top of the defaults rather than under them.

        Args:
            ctx: Shared application context.
            params: ``ops`` — the operations to perform.

        Returns:
            A :class:`ToolResult` counting the preferences changed.

        Raises:
            PreferenceError: If a key is unknown or a value fails its spec — reported
                cleanly by the CLI rather than as a traceback.
        """
        from ..ui.preferences import preferences_table
        from ..ui.surface import TuiUi

        prefs = ctx.preferences
        scripted = not isinstance(ctx.ui, TuiUi)
        ops: list[tuple] = list(params.get("ops") or [])
        changes = 0
        for op in ops:
            action = op[0]
            if action == "show":
                ctx.ui.show(
                    _listing(prefs) if scripted else preferences_table(prefs, ctx.console.width)
                )
            elif action == "get":
                # The bare value and nothing else — the caller named the key, and
                # whether it is overridden is what `show`'s DEFAULT lane answers.
                spec = get_spec(op[1])
                if scripted:
                    ctx.ui.note(_script_value(spec, prefs.get(spec.key)))
                else:
                    where = "changed" if prefs.is_overridden(spec.key) else "default"
                    shown = format_value(spec, prefs.get(spec.key))
                    ctx.ui.note(f"[accent]{spec.key}[/accent] = [brand]{shown}[/brand] ({where})")
            elif action == "set":
                before = prefs.get(op[1])
                after = prefs.set(op[1], op[2])
                if after != before:
                    changes += 1
            elif action == "reset":
                changes += prefs.reset()

        if changes:
            prefs.save()

        plural = "" if changes == 1 else "s"
        message = (
            f"[ok]✓[/ok] saved [brand]{changes}[/brand] preference{plural} to "
            f"[accent]{prefs.path}[/accent]"
            if changes
            else None
        )
        return ToolResult(summary={"changes": changes}, message=message)

    # -- CLI --------------------------------------------------------------------

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``preferences`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        prefs_app = typer.Typer(help=self.help, no_args_is_help=False, rich_markup_mode=None)

        @prefs_app.callback(invoke_without_command=True)
        def _root(ctx: typer.Context) -> None:
            """Show every preference when no subcommand is given."""
            if ctx.invoked_subcommand is None:
                run_tool_command(self, {"ops": [("show",)]})

        @prefs_app.command("show", help="Show every preference, its value, and its default")
        def _show_cmd() -> None:
            run_tool_command(self, {"ops": [("show",)]})

        @prefs_app.command("get", help="Print one preference's value")
        def _get_cmd(key: str = typer.Argument(..., help="Preference key")) -> None:
            run_tool_command(self, {"ops": [("get", key)]})

        @prefs_app.command("set", help="Set one preference to a value")
        def _set_cmd(
            key: str = typer.Argument(..., help="Preference key"),
            value: str = typer.Argument(..., help="New value"),
        ) -> None:
            run_tool_command(self, {"ops": [("set", key, value)]})

        @prefs_app.command("reset", help="Return every preference to its built-in default")
        def _reset_cmd(
            yes: bool = typer.Option(False, "--yes", help="Skip the confirmation"),
        ) -> None:
            if not yes:
                raise typer.BadParameter(
                    "resetting drops every preference you have changed; pass --yes to confirm"
                )
            run_tool_command(self, {"ops": [("reset",)]})

        app.add_typer(prefs_app, name=self.name)


__all__ = ["PreferencesTool", "PreferenceError"]


def _script_value(spec: PrefSpec, value: Any) -> str:
    """A preference's value in the form ``preferences set`` accepts back.

    :func:`~meshterm.core.preferences.format_value` writes for the page: a number carries
    its unit (``5 s``), so a bare figure in the VALUE lane still says what it counts. That
    unit is exactly what :func:`~meshterm.core.preferences.parse_value` will not take
    back, so the scripted dump drops it. Booleans keep ``on``/``off`` and an enum keeps
    its own key, both of which do round-trip.

    Args:
        spec: The preference the value belongs to.
        value: Its current value.

    Returns:
        The scripted rendering.
    """
    if spec.value_type == "bool":
        return "on" if value else "off"
    if spec.value_type in ("int", "float"):
        return f"{value:g}"
    return str(value)


def _listing(prefs: Preferences) -> Table:
    """Every preference, its current value, and the built-in it would fall back to.

    Three facts per line and no fourth: the descriptions the page carries explain a
    preference to someone deciding whether to change it, and they are what forces the
    table to wrap. A scripted reader has decided.

    Args:
        prefs: The preference set to report.

    Returns:
        The scripted table (see :func:`meshterm.ui.script.columns`).
    """
    from ..ui import script

    table = script.columns("PREFERENCE", "VALUE", "DEFAULT")
    for spec in PREFERENCES:
        table.add_row(
            spec.key,
            _script_value(spec, prefs.get(spec.key)),
            _script_value(spec, spec.default),
        )
    return table
