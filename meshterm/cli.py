"""Typer entry point.

Global options are parsed once in the callback, which builds the shared
:class:`~meshterm.context.AppContext`. Running with no subcommand launches the
interactive menu; otherwise the selected tool's subcommand runs. Both paths funnel
through :func:`run_tool_command` / :func:`run_menu`, so behavior stays identical.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer

from .context import AppContext
from .core.admin_store import AdminStore
from .core.config import Settings
from .core.connection import DeviceCommandError, is_connection_lost
from .core.device_config import DeviceConfigError
from .core.device_store import DeviceStore
from .core.selection import DeviceSelectionError
from .persistence.logging import configure_logging
from .persistence.repository import Repository
from .tools import all_tools
from .tools.base import Tool, ToolResult
from .ui.theme import make_console

def _unframe_help_panels() -> None:
    """Render Typer's help/error sections as coloured headings instead of boxed panels.

    Typer's rich help gives us the colour we want — yellow usage, cyan options, green
    switches — but wraps each Options/Commands/Error section in a rounded :class:`Panel`,
    the boxed framing scripted output is meant to be free of. There's no constant to drop
    that box, so we swap the ``Panel`` the help formatter calls for a thin stand-in that
    keeps the colour and the section title (as a bold heading, indigo like the app accent,
    red for errors) but no border — the same heading-over-content shape tool results use
    (see :func:`~meshterm.ui.surface._deframe`). The inner tables are already box-less.
    """
    import typer.rich_utils as rich_utils
    from rich.console import Group
    from rich.text import Text

    def _bare_panel(
        renderable: object,
        *,
        title: object = None,
        border_style: str = "",
        **_: object,
    ) -> Group:
        style = "bold red" if border_style == "red" else "bold #818cf8"
        heading = Text(str(title), style=style) if title else Text("")
        return Group(Text(""), heading, renderable)  # type: ignore[list-item]

    rich_utils.Panel = _bare_panel  # type: ignore[assignment,misc]


_unframe_help_panels()

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    # Rich help for its colour (usage, option flags, metavars), but with the boxed
    # section panels flattened to plain headings by _unframe_help_panels() above — a
    # splash of colour, none of the framing.
    rich_markup_mode="rich",
    help="MeshTerm — a modern toolkit for tuning and exploring your MeshCore mesh",
)

# The context built by the callback and consumed by subcommands within one process.
_state: Optional[AppContext] = None


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Device profile"),
    port: Optional[str] = typer.Option(None, "--port", help="Serial port override"),
    ble: Optional[str] = typer.Option(
        None, "--ble", help="Bluetooth address of a companion device (selects the BLE transport)"
    ),
    ble_pin: Optional[str] = typer.Option(
        None, "--ble-pin", help="BLE pairing PIN, if the Bluetooth companion requires one"
    ),
    tcp: Optional[str] = typer.Option(
        None,
        "--tcp",
        help="Network address host[:port] of a TCP companion (selects the TCP transport)",
    ),
    mock: bool = typer.Option(False, "--mock", help="Use the built-in simulator"),
    db_path: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress console logging"),
) -> None:
    """Build the application context and dispatch to the menu or a subcommand.

    Args:
        ctx: The Click/Typer context.
        profile: Named device profile to use.
        port: Explicit serial port, overriding the profile.
        ble: Explicit Bluetooth address, selecting the BLE transport.
        ble_pin: Optional BLE pairing PIN for the Bluetooth companion.
        tcp: Explicit network address ``host[:port]``, selecting the TCP transport.
        mock: Whether to use the simulator instead of real hardware.
        db_path: Override the database location.
        json_output: Request machine-readable output from tools.
        quiet: Suppress console logging (file logging continues).
    """
    global _state

    settings = Settings.load()
    if db_path is not None:
        settings.db_path = db_path

    console = make_console()
    configure_logging(
        console, settings.config_dir, level=logging.INFO, quiet=quiet or json_output
    )

    app_ctx = AppContext(
        console=console,
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(settings.config_dir / "devices.json"),
        admin_store=AdminStore(settings.config_dir / "admin.json"),
        profile=settings.resolve_profile(profile),
        mock=mock,
        port_override=port,
        ble_override=ble,
        tcp_override=tcp,
        ble_pin=ble_pin,
        json_output=json_output,
        explicit_selection=(
            profile is not None or port is not None or ble is not None or tcp is not None
        ),
    )
    _state = app_ctx
    ctx.call_on_close(app_ctx.repo.close)

    if ctx.invoked_subcommand is None:
        from .ui.menu import run_menu

        asyncio.run(_drive(run_menu(app_ctx), app_ctx))


def run_tool_command(tool: Tool, params: dict) -> None:
    """Execute a tool from a CLI subcommand and render its result.

    Builds and tears down the device connection within a single event loop so the
    ``meshcore`` client is never used across loops.

    Args:
        tool: The tool to run.
        params: Parameters parsed from the subcommand's options.
    """
    assert _state is not None  # set by the callback before any subcommand runs
    try:
        asyncio.run(_drive(_execute_and_render(tool, params, _state), _state))
    except (DeviceSelectionError, DeviceConfigError, DeviceCommandError) as exc:
        # Expected user-facing error (ambiguous/absent device, bad config value, or a
        # transient command failure): show the message, not a traceback.
        _state.console.print(f"[err]✗[/err] {exc}")
        raise typer.Exit(1) from exc
    except Exception as exc:
        # A dropped serial link (device unplugged/powered off mid-command) can't be recovered
        # from in a one-shot scripted run the way the interactive menu offers — but it should
        # still read as a clean message, not a traceback. Anything else propagates as before.
        if not is_connection_lost(exc):
            raise
        _state.console.print(
            "[err]✗[/err] the connection to your device was lost "
            "(it may have been unplugged or powered off)."
        )
        raise typer.Exit(1) from exc


async def _drive(coro, ctx: AppContext) -> None:
    """Await a coroutine then disconnect the device (but keep the repo open).

    Args:
        coro: The coroutine to run (a tool execution or the menu loop).
        ctx: The application context whose device should be closed afterward.
    """
    try:
        await coro
    finally:
        if ctx._device is not None:
            try:
                await ctx._device.disconnect()
            except Exception:  # noqa: BLE001 - a dead/lost link must not crash teardown
                pass
            ctx._device = None


async def _execute_and_render(tool: Tool, params: dict, ctx: AppContext) -> ToolResult:
    """Run a tool and print its closing message and artifact list.

    Args:
        tool: The tool to execute.
        params: Parameters for the tool.
        ctx: The application context.

    Returns:
        The tool's :class:`ToolResult`.
    """
    result = await tool.execute(ctx, params)
    if result.message:
        ctx.console.print(result.message)
    for artifact in result.artifacts:
        ctx.console.print(f"[ok]●[/ok] wrote [accent]{artifact}[/accent]")
    return result


def _register_all() -> None:
    """Register every tool's CLI subcommand with the Typer app."""
    for tool in all_tools():
        tool.register_cli(app)


_register_all()


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
