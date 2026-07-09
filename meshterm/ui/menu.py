"""The interactive main menu, built dynamically from the tool registry.

The menu groups registered tools by category and runs the selected tool's interactive flow
inside the full-screen :class:`~meshterm.ui.tui.session.TuiSession`. Because it is generated
from the registry, adding a tool automatically adds a menu entry with no changes here. The
persistent header shows the connection target and live passive-monitor counters; each tool's
prompts layer as dialogs, and its output appears in a bounded, scrollable result window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
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
from .tui import CANCEL, Choice, SelectScreen, Separator, TuiSession
from .tui.emoji_width import calibrate as calibrate_emoji_width


#: Grace period (seconds) allowed for the whole exit sequence — the full-screen unwind,
#: the device disconnect, and the interpreter's atexit thread joins — once the user has
#: committed to quitting. Comfortably longer than a healthy teardown (~1 s), so it only
#: ever fires when exit has genuinely wedged.
_EXIT_WATCHDOG_S = 5.0


def _arm_exit_watchdog(seconds: float = _EXIT_WATCHDOG_S) -> None:
    """Guarantee the process terminates even if the exit path wedges.

    Called once, at the start of teardown, after the app's own async resources have been
    released. If a clean exit completes within ``seconds`` the process is already gone and
    this daemon thread dies with it, so the happy path is untouched. It only bites when exit
    hangs — most often on prompt_toolkit's Windows input reader, a *non-daemon* executor
    thread that in rare teardown races stays blocked in a Win32 wait and stalls the
    interpreter's exit-time thread joins (it also backstops any stall in the device
    disconnect). A daemon thread can still run while the main thread is blocked in that join,
    so it force-exits the process.

    Committed database writes are durable regardless (each is committed as it happens), so a
    hard exit here loses nothing.

    Args:
        seconds: Grace period before forcing termination.
    """

    def _bail() -> None:
        time.sleep(seconds)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(0)

    threading.Thread(target=_bail, name="meshterm-exit-watchdog", daemon=True).start()


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
        f"[brand]MeshTerm[/brand] [muted]v{__version__}[/muted]  ·  "
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
    # Measure how this terminal renders emoji and align Rich to it, before
    # prompt_toolkit takes over the screen. This keeps every panel border — and
    # every chat bubble — aligned regardless of the terminal's emoji widths.
    calibrate_emoji_width()

    session = TuiSession(header=lambda: _header(ctx))
    ctx.ui = TuiUi(session)

    async def main() -> None:
        try:
            if await _startup(ctx):
                await _menu_loop(ctx, session)
        finally:
            # Stop history + chat recording (closing their run records) and the always-on
            # event hub, even on an unexpected exit.
            await ctx.monitor.aclose()
            await ctx.chat.aclose()
            await ctx.events.aclose()
            # Everything the app owns is released; the remaining exit steps (prompt_toolkit's
            # full-screen unwind, the device disconnect in the CLI driver, the interpreter's
            # atexit thread joins) must never be able to hang the process. See
            # _arm_exit_watchdog.
            _arm_exit_watchdog()

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
    loop = asyncio.get_running_loop()
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

        # Drive the menu list ourselves (rather than via session.select) so it stays on the
        # stack while the quit dialog floats over it: the confirm is drawn as a centered box
        # on top of the still-visible menu, not as a screen that replaces it. Re-highlight the
        # tool the user just backed out of so returning lands the cursor where they left.
        menu = SelectScreen(
            "What would you like to do?",
            items,
            default=last_selection,
            footer_hint="↑↓ move · type to filter · Enter select · Esc quit",
        )
        menu.future = loop.create_future()
        session.push(menu)
        try:
            selection = await menu.future
            if selection is CANCEL:  # Esc at the top level
                selection = None
            # Both picking "quit" and pressing Esc ask to leave; confirm on a dialog floating
            # over the (still-pushed) menu so a stray key doesn't drop the user out. Cancel
            # (Esc) sits left of Quit (Enter); Quit starts highlighted so Enter commits it.
            if selection in (None, "__quit__"):
                leave = await session.button_dialog(
                    "Are you sure you want to quit?",
                    [("Cancel", False), ("Quit", True)],
                    title="Quit MeshTerm",
                    default=1,
                    footer_hint="Esc cancel · Enter quit",
                    prompt_style="warn",
                    button_style="brand",
                    button_idle_style="muted",
                    border_style="warn",
                )
                if leave:
                    return
                # Keep the cursor on "quit" when that is what they chose, so a follow-up
                # attempt lands where they expect; a stray Esc leaves it where it was.
                if selection == "__quit__":
                    last_selection = "__quit__"
                continue
            last_selection = selection
        finally:
            session.pop(menu)
        await _run_selection(ctx, selection)


async def _startup(ctx: AppContext) -> bool:
    """Pick a companion device (if needed) and resume passive monitoring.

    Args:
        ctx: The shared application context to update with the selection.

    Returns:
        ``True`` to enter the menu, or ``False`` if the user chose to exit at the startup
        splash (pressed Esc or picked Quit) — in which case the caller skips the menu loop.
    """
    if not (ctx.mock or ctx.explicit_selection):
        from ..core.connection import probe_meshcore
        from ..core.discovery import DiscoveredDevice, discover_devices
        from .device_picker import prompt_device

        devices = discover_devices()
        baudrate = ctx.profile.baudrate if ctx.profile else 115200
        # The smoke test opens the radio; on success we keep that live connection and reuse
        # it for the session rather than reopening (boards often reset on each serial open).
        probed: dict = {}

        async def verify(device: DiscoveredDevice):
            result = await probe_meshcore(device.port, baudrate)
            if result is None:
                return None
            connection, info = result
            probed["port"] = device.port
            probed["device"] = connection
            return info

        chosen = await prompt_device(ctx.ui, devices, ctx.device_store, verify)
        if chosen is None:
            return False  # the user quit at the splash — exit without opening the menu
        ctx.selected_device = chosen
        ctx.port_override = chosen.port
        if probed.get("port") == chosen.port:
            ctx.adopt_device(probed["device"])
    await _resume_monitor(ctx)
    return True


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
