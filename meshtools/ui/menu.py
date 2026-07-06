"""The interactive main menu, built dynamically from the tool registry.

The menu groups registered tools by category and runs the selected tool's interactive flow
inside the full-screen :class:`~meshtools.ui.tui.session.TuiSession`. Because it is generated
from the registry, adding a tool automatically adds a menu entry with no changes here. The
persistent header shows the connection target and live passive-monitor counters; each tool's
prompts layer as dialogs, and its output appears in a bounded, scrollable result window.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from rich.logging import RichHandler
from rich.text import Text

from .. import __version__
from ..context import AppContext
from ..persistence.logging import get_logger
from ..tools import all_tools
from .surface import TuiUi
from .theme import make_console
from .tui import Choice, Separator, TuiSession


@contextmanager
def _silence_console_logging() -> Iterator[None]:
    """Detach console log handlers for the life of the full-screen TUI.

    The full-screen session owns the terminal via prompt_toolkit; any handler that
    writes log lines to the same console (the :class:`RichHandler` installed by
    :func:`configure_logging`) corrupts the frame — most visibly when toggling the
    monitor, which connects the device and emits INFO records on demand. File logging is
    untouched, so the JSON-lines record stays complete; the handlers are restored on exit.
    """
    logger = get_logger()
    detached = [h for h in logger.handlers if isinstance(h, RichHandler)]
    for handler in detached:
        logger.removeHandler(handler)
    try:
        yield
    finally:
        for handler in detached:
            logger.addHandler(handler)


def _header(ctx: AppContext) -> Text:
    """Build the persistent one-line header (target + live monitor status).

    Args:
        ctx: The shared application context, read for the target and monitor counters.

    Returns:
        A Rich :class:`Text` shown at the top of every screen; re-read on each repaint so
        the passive-monitor counters tick live.
    """
    if ctx.mock:
        target = "[warn]simulator[/warn]"
    elif ctx.profile_name:
        target = ctx.profile_name
    elif ctx.selected_device is not None:
        target = ctx.selected_device.label
    else:
        target = "[muted]no device[/muted]"
    unread = ctx.chat.unread_total()
    chat_segment = f"  ·  [accent]✉ {unread} unread[/accent]" if unread else ""
    # Colour only the leading status glyph (● / ○) — green when the monitor is on
    # (enabled), red when off — leaving the rest of the text muted as before.
    status = ctx.monitor.status_text()
    glyph, rest = status[:1], status[1:]
    glyph_style = "ok" if ctx.monitor.enabled else "err"
    # The frame crops this to a single line (see frame.compose_base), so a narrow terminal
    # shows what fits and chops the rest rather than wrapping onto a second row.
    return Text.from_markup(
        f"[brand]MeshTools[/brand] [muted]v{__version__}[/muted]  ·  "
        f"[muted]device:[/muted] {target}  ·  "
        f"[{glyph_style}]{glyph}[/{glyph_style}][muted]{rest}[/muted]"
        f"{chat_segment}"
    )


async def run_menu(ctx: AppContext) -> None:
    """Run the interactive menu loop inside the full-screen session until the user quits.

    Installs the TUI surface, starts the session, and drives device selection, monitor
    resume, and the menu loop on the session's event loop. The full-screen frame is torn
    down before the closing message prints.

    Args:
        ctx: The shared application context.
    """
    session = TuiSession(header=lambda: _header(ctx))
    ctx.ui = TuiUi(session)

    async def main() -> None:
        try:
            await _startup(ctx)
            await _menu_loop(ctx, session)
        finally:
            # Stop history + chat recording (closing their run records) and the always-on
            # event hub, even on an unexpected exit.
            await ctx.monitor.aclose()
            await ctx.chat.aclose()
            await ctx.events.aclose()

    with _silence_console_logging():
        await session.run(main())
    ctx.console.print("[muted]bye 73![/muted]")


async def _menu_loop(ctx: AppContext, session: TuiSession) -> None:
    """Show the tool menu and run selections until the user quits.

    Args:
        ctx: The shared application context.
        session: The running TUI session.
    """
    last_selection: str | None = None
    while True:
        items: list = []
        current_category: str | None = None
        for tool in all_tools():
            if tool.category != current_category:
                current_category = tool.category
                items.append(Separator(f"── {current_category} ──"))
            items.append(Choice(title=f"{tool.name}  —  {tool.help}", value=tool.name))
        items.append(Separator(" "))
        items.append(Choice(title="quit", value="__quit__"))

        # Re-highlight the tool the user just backed out of, so returning to the menu
        # lands the cursor where they left rather than at the top.
        selection = await session.select(
            "What would you like to do?", items, default=last_selection
        )
        if selection in (None, "__quit__"):
            return
        last_selection = selection
        await _run_selection(ctx, selection)


async def _startup(ctx: AppContext) -> None:
    """Pick a companion device (if needed) and resume passive monitoring.

    Args:
        ctx: The shared application context to update with the selection.
    """
    if not (ctx.mock or ctx.explicit_selection):
        from ..core.discovery import discover_devices
        from .device_picker import prompt_device

        devices = discover_devices()
        chosen = await prompt_device(ctx.ui, devices, ctx.device_store.load())
        if chosen is not None:
            ctx.selected_device = chosen
            ctx.port_override = chosen.port
    await _resume_monitor(ctx)


async def _resume_monitor(ctx: AppContext) -> None:
    """Start always-on background listening, and resume history recording if enabled.

    A MeshCore client always listens while it runs, so by default the event hub is
    started unconditionally at launch; if the passive-monitor preference is on, history
    recording is resumed on top of it. The ``connect_on_start`` setting can defer that
    eager connect — when it is off, the hub is left idle here and opens lazily instead
    (when recording is turned on, or a tool first needs the radio). A failure to start
    (typically no companion device selected) is non-fatal: the preference stays on, so
    recording resumes automatically once a device is available. The header reflects the
    resulting state, so nothing needs to be printed here.

    Args:
        ctx: The shared application context.
    """
    if ctx.settings.connect_on_start:
        try:
            await ctx.events.start()
        except Exception:  # noqa: BLE001 - surface via the header, don't crash the menu
            pass
        # Record inbound messages from launch so the inbox and unread badge stay current
        # even before the chat screen is opened. Deferred (like the hub) when connect on
        # start is off; the chat screen starts it lazily then.
        try:
            await ctx.chat.start()
        except Exception:  # noqa: BLE001 - surface via the header, don't crash the menu
            pass
    if not ctx.monitor.enabled:
        return
    try:
        await ctx.monitor.start()
    except Exception:  # noqa: BLE001 - surface via the header, don't crash the menu
        pass


async def _run_selection(ctx: AppContext, name: str) -> None:
    """Gather parameters for, execute, and present a single tool from the menu.

    Args:
        ctx: The shared application context.
        name: The selected tool's name.
    """
    from ..tools import get_tool

    tool = get_tool(name)
    if tool is None:  # pragma: no cover - registry and menu are always in sync
        ctx.ui.note(f"[err]Unknown tool: {name}[/err]")
        await ctx.ui.present(title=name)
        return

    try:
        params = await tool.prompt_params(ctx)
        if params is None:  # user cancelled a prompt
            ctx.ui.discard()
            return
        result = await tool.execute(ctx, params)
    except Exception as exc:  # noqa: BLE001 - surface errors without crashing the menu
        ctx.ui.note(f"[err]✗ {tool.name} failed:[/err] {exc}")
        await ctx.ui.present(title=tool.name)
        return

    if result.message:
        ctx.ui.note(result.message)
    for artifact in result.artifacts:
        ctx.ui.note(f"[ok]●[/ok] wrote [accent]{artifact}[/accent]")
    await ctx.ui.present(title=tool.name)


# Exposed for tools that need a standalone console outside a context (rare).
default_console = make_console
