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
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator, Optional

from rich.cells import cell_len
from rich.logging import RichHandler
from rich.text import Text

from .. import __version__
from ..context import AppContext
from ..persistence.logging import get_logger
from ..platforms import get_platform
from ..tools import all_tools
from .surface import TuiUi
from .braillechart import activity_peak, activity_sparkline
from .menus import SEP_COMPACT, SEP_ROOMY, command_icon, command_label, section_heading
from .theme import make_console
from .widgets import battery_cell
from .tui import (
    CANCEL,
    Choice,
    ReconnectDialog,
    ScrollScreen,
    SelectScreen,
    Separator,
    TuiSession,
)
from .tui.emoji_width import calibrate as calibrate_emoji_width
from .tui.spinner import spinner_interval


#: Grace period (seconds) allowed for the whole exit sequence — the full-screen unwind,
#: the device disconnect, and the interpreter's atexit thread joins — once the user has
#: committed to quitting. Comfortably longer than a healthy teardown (~1 s), so it only
#: ever fires when exit has genuinely wedged.
_EXIT_WATCHDOG_S = 5.0

#: How often the liveness watcher checks that the connected companion's serial port is still
#: present while the menu sits idle (seconds). The ``meshcore`` client serves cached data and
#: never raises on an unplug, so this OS-level port poll — not a failed command — is what
#: actually notices a pulled cable (see :func:`~meshterm.core.connection.serial_port_present`).
_LIVENESS_POLL_S = 2.0

#: Debounce: after the port first appears to be gone, wait this long and re-check before
#: declaring the link lost, so a momentary enumeration gap (driver churn during a replug)
#: can't fire a false "disconnected" prompt (seconds).
_LIVENESS_CONFIRM_S = 0.4


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
    :func:`configure_logging`) corrupts the frame — most visibly when the monitor
    connects the device at startup and emits INFO records. File logging is
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


#: The fewest sparkline cells the header aims to keep. When the fixed segments with
#: roomy "  ·  " separators would leave less than this, the header re-lays itself with
#: the compact " · " so a 72-column terminal still shows a readable stretch of pulse.
_SPARK_MIN_CELLS = 24

#: Floor for the header pulse's scaling ceiling (see :func:`~meshterm.ui.braillechart.
#: activity_peak`): a lone packet in a long-silent window draws against at least this
#: many packets-per-minute, so a single stray reads as a small nub, not a full column.
_HEADER_ACTIVITY_FLOOR = 3.0

def _header(ctx: AppContext, cache: dict, width: int) -> Text:
    """Build the persistent one-line header: who's connected, unread mail, mesh pulse.

    Left to right: the app mark, the connected node's own name with where it's reached
    (``(COM5)`` / ``(BLE)``), unread-message and Watchtower badges (shown only when
    something is waiting), and the braille activity pulse counting every packet the hub
    hears at one minute per dot column — newest at the right edge, like every MeshTerm
    timeline. The pulse fills the middle of the row; a battery gauge (only when the
    companion reports a pack) is pinned to the right edge and the pulse cedes exactly the
    cells it needs, so a wider terminal simply shows deeper history. Separators are roomy
    by default and drop to a compact ``·`` when that would squeeze the pulse below
    :data:`_SPARK_MIN_CELLS`.

    Args:
        ctx: The shared application context, read live on every repaint.
        cache: Scratch owned by the session (see :func:`_device_label`) so the
            device-name lookup doesn't re-read the registry file per repaint.
        width: The terminal width in columns; the sparkline soaks up whatever the
            fixed segments leave of it.

    Returns:
        A Rich :class:`Text` shown at the top of every screen. The frame crops it to a
        single line (see ``frame.compose_base``), so a too-narrow terminal chops the
        tail rather than wrapping.
    """
    # Which items compose at all is platform data (see Platform.header_atoms): the
    # PicoCalc's 53 columns drop the version mark and the pulse, keeping device, badges
    # and battery. Read live — set_platform runs before the session, but tests swap.
    atoms = get_platform().header_atoms
    # The battery gauge is pinned to the row's right edge, so reserve its width (plus a
    # leading separator) before the pulse claims the rest; a wider separator estimate for
    # the fit decision is harmless slack.
    battery = _battery_segment(ctx) if "battery" in atoms else Text()
    # The separator choice changes only the *joins*, never the segments themselves, so the
    # segments are built once and each candidate width is arithmetic: one separator per
    # segment (each is preceded by one, and a trailing one leads into the pulse). Building
    # them twice to measure the second option was pure waste on exactly the narrow terminals
    # that take the compact branch every repaint — the PicoCalc's 53 columns among them.
    pieces = _header_segments(ctx, cache)
    content = sum(piece.cell_len for piece in pieces)

    def _measure(sep: str) -> tuple[int, int]:
        """The joined header width and the right-edge reserve, for one separator."""
        joined = content + len(pieces) * cell_len(sep)
        return joined, (cell_len(sep) + battery.cell_len) if battery.cell_len else 0

    sep = SEP_ROOMY
    header_w, reserve = _measure(sep)
    if width - header_w - reserve < _SPARK_MIN_CELLS:
        sep = SEP_COMPACT
        header_w, reserve = _measure(sep)
    header = Text()
    for piece in pieces:
        header.append_text(piece)
        header.append(sep)
    # Two dot columns per cell: every cell left of the reserved tail shows two minutes.
    room = width - header_w - reserve
    if "pulse" not in atoms:
        room = 0
    if room > 0:
        # Buckets seeded from a previous session's stored history draw grey; only
        # traffic this session actually heard pulses green.
        styles = [
            "ok" if live else "muted" for live in ctx.monitor.activity_session_flags()
        ]
        # Scale to a steady ceiling over the monitor's *full* six-hour history — deeper
        # than the row draws — not the drawn window's bare maximum: an outlier-robust,
        # floored peak (see activity_peak), so the pulse doesn't lurch as a busy minute
        # scrolls off the edge and a lone packet in a lull stays a nub.
        histogram = ctx.monitor.activity_histogram()
        header.append_text(
            activity_sparkline(
                histogram,
                room * 2,
                peak=activity_peak(histogram, floor=_HEADER_ACTIVITY_FLOOR),
                column_styles=styles,
            )
        )
    if battery.cell_len:
        # Pin the gauge flush right: once the pulse has filled `room`, the separator and
        # gauge land against the edge. With no room for a pulse (a very narrow terminal),
        # pad instead so the gauge still sits in the corner rather than trailing the text.
        if room <= 0:
            header.append(" " * max(0, width - header_w - reserve))
        header.append(sep)
        header.append_text(battery)
    return header


#: Seconds per animation step of the header battery gauge (the charging sweep and the
#: low-battery blink). Matched to the session's ~1 Hz idle repaint (see the app's
#: ``refresh_interval``) so each repaint advances the animation by one clean step rather
#: than aliasing across skipped frames.
_BATTERY_ANIM_S = 1.0


def _battery_segment(ctx: AppContext) -> Text:
    """The header's right-anchored battery gauge, or an empty Text when there is no pack.

    Reads the poller's cached snapshot (:meth:`~meshterm.services.battery_service.
    BatteryService.reading`) — never the radio — so it is safe on the render path. The
    animation frame is derived from the wall clock, so the charging sweep and low-battery
    blink advance on their own without the header threading a counter through.

    Args:
        ctx: The shared application context, read for the latest battery snapshot.

    Returns:
        The gauge as a Rich :class:`Text` (glyph + ``%``), or an empty one when the
        companion reports no battery.
    """
    reading = ctx.battery.reading()
    if reading is None:
        return Text()
    # A platform without effects draws the gauge's resting state instead: the true fill,
    # unblinking, with charging said by a static mark rather than the sweep. The blink and
    # the sweep both exist to catch the eye, and neither is worth a forced repaint (nor, on
    # a 16-slot console, a colour swap) on hardware where the cells are dear — but charging
    # is a *fact about the pack*, not decoration, so it still has to show at rest.
    effects = get_platform().effects
    frame = int(time.monotonic() / _BATTERY_ANIM_S) if effects else 0
    return battery_cell(
        reading.percent, charging=reading.charging, frame=frame, animate=effects
    )


def _header_segments(ctx: AppContext, cache: dict) -> list[Text]:
    """The header's fixed segments — everything left of the pulse — unjoined.

    Separator-free by design: which separator fits depends on how wide these come out, and
    the choice changes nothing about the segments themselves, so :func:`_header` measures
    both options arithmetically off this one build and joins once.

    Args:
        ctx: The shared application context.
        cache: The device-label cache (see :func:`_device_label`).

    Returns:
        The segments in display order — the app mark, the device, then whichever badges
        have something to report. :func:`_header` writes one separator after each.
    """
    atoms = get_platform().header_atoms
    segments: list[Text] = []
    # Each segment starts as an unstyled Text and takes its styles per append. A base style
    # passed to the constructor would instead blanket everything appended after it, layering
    # the mark's brand under the version's muted.
    if "version" in atoms:
        mark = Text()
        mark.append("MeshTerm", style="brand")
        mark.append(f" v{__version__}", style="muted")
        segments.append(mark)

    if "device" in atoms:
        device = Text()
        if ctx.mock:
            device.append("simulator", style="warn")
        else:
            name, where = _device_label(ctx, cache)
            device.append(name or "no device", style=None if name else "muted")
            if where:
                device.append(f" ({where})", style="muted")
        segments.append(device)

    if "badges" in atoms:
        unread = ctx.chat.unread_total()
        if unread:
            badge = Text()
            badge.append("●", style="err")
            badge.append(f" {unread}", style="warn")
            segments.append(badge)
        alerts = ctx.watchtower.unacked_count()
        if alerts:
            # The Watchtower's badge: a triangle so it never reads as unread mail.
            badge = Text()
            badge.append("▲", style="err")
            badge.append(f" {alerts}", style="warn")
            segments.append(badge)
    return segments


def _device_label(ctx: AppContext, cache: dict) -> tuple[str, str]:
    """The header's device segment: ``(node name, where)``, cached per connection.

    The name prefers the device's own mesh node name (remembered at connect time in the
    device registry), then the profile alias, then the discovered hardware name — the
    same ladder the startup picker's NAME column walks. ``where`` is the serial port, or
    ``"BLE"`` for a Bluetooth companion. The registry lives in a file, so the lookup is
    cached under the connection's identity and re-read only when that changes.

    Args:
        ctx: The shared application context.
        cache: A dict owned by the caller; holds one ``(key, value)`` pair.

    Returns:
        The ``(name, where)`` pair; either may be empty when genuinely unknown.
    """
    sel = ctx.selected_device
    key = (
        sel.stable_id if sel is not None else None,
        ctx.profile_name,
        ctx.active_transport,
        ctx.active_port,
        ctx.active_address,
        ctx.active_endpoint,
    )
    if cache.get("key") == key:
        return cache["value"]

    from .device_picker import _hardware_name

    record = (
        ctx.device_store.load_all().get(sel.stable_id) if sel is not None
        else ctx.device_store.load()  # remembered reconnect: nothing discovered this session
    )
    name = (record.node_name if record is not None else "") or ctx.profile_name or ""
    if not name and sel is not None:
        name = _hardware_name(sel)
    if not name and record is not None:
        name = record.label
    if ctx.active_transport == "ble":
        where = "BLE"
    elif ctx.active_transport == "tcp":
        where = ctx.active_endpoint or "TCP"
    else:
        where = ctx.active_port or (sel.port if sel is not None else "") or ""
    cache["key"], cache["value"] = key, (name, where)
    return name, where


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
    # every chat bubble — aligned regardless of the terminal's emoji widths. Skipped
    # outright on a platform that never draws emoji (PicoCalc): there is nothing to
    # calibrate, and the probe writes escape sequences the console font can't shape.
    if get_platform().emoji:
        calibrate_emoji_width()

    header_cache: dict = {}
    session = TuiSession(header=lambda cols: _header(ctx, header_cache, cols))
    ctx.ui = TuiUi(session)

    async def main() -> None:
        try:
            if await _startup(ctx):
                await _session_loop(ctx, session)
        finally:
            # Stop history + chat recording (closing their run records), the background
            # advert scheduler, and the always-on event hub, even on an unexpected exit.
            await ctx.adverts.aclose()
            await ctx.monitor.aclose()
            await ctx.chat.aclose()
            await ctx.events.aclose()
            # If the user chose "Unpair & quit", drop the OS bond now — after the services are
            # down and the link is torn down, since a bond can't be cleanly removed while it is
            # in use. Best-effort: a failure must not block the exit.
            if ctx.unpair_on_exit:
                await _unpair_on_exit(ctx)
            # Everything the app owns is released; the remaining exit steps (prompt_toolkit's
            # full-screen unwind, the device disconnect in the CLI driver, the interpreter's
            # atexit thread joins) must never be able to hang the process. See
            # _arm_exit_watchdog.
            _arm_exit_watchdog()

    with _silence_console_logging():
        await session.run(main())
    ctx.console.print("[muted]bye 73![/muted]")


async def _can_unpair(ctx: AppContext) -> bool:
    """Whether the quit dialog should offer to drop the current device's OS pairing.

    True only when this session is on a Bluetooth link whose peripheral Windows actually holds
    a bond for — so the affordance appears for a PIN-paired companion but never for a serial
    port, an open (PIN-less) BLE companion, or a platform where we can't unpair. The bond query
    reflects the OS pairing (independent of MeshTerm's remembered record), so it correctly lights
    up even for a device bonded in an earlier session and reconnected here without a PIN.

    Args:
        ctx: The shared application context.

    Returns:
        ``True`` if an unpair-and-quit button is warranted.
    """
    if ctx.active_transport != "ble":
        return False
    address = ctx.active_address
    if not address:
        return False
    from ..core.connection import MeshCoreDevice

    return await MeshCoreDevice.is_ble_paired(address)


async def _unpair_on_exit(ctx: AppContext) -> None:
    """Tear down the live connection and drop the device's OS bond, on the way out.

    Ordered deliberately: the companion link is disconnected first (a bond can't be cleanly
    removed while an open connection is using it), then the Windows pairing is forgotten so the
    next launch re-runs the PIN ceremony. MeshTerm's remembered-device record is left untouched —
    unpairing forgets the *credential*, not the device's identity. Best-effort throughout: any
    failure is swallowed so it can never wedge the exit.

    Args:
        ctx: The shared application context (its device is disconnected as a side effect).
    """
    address = ctx.active_address
    if ctx._device is not None:
        try:
            await ctx._device.disconnect()
        except Exception:  # noqa: BLE001 - a dead/lost link must not block unpair or exit
            pass
        ctx._device = None
    if address:
        from ..core.connection import MeshCoreDevice

        await MeshCoreDevice.unpair_ble(address)


async def _menu_loop(ctx: AppContext, session: TuiSession) -> None:
    """Show the tool menu and run selections until the user quits.

    Args:
        ctx: The shared application context.
        session: The running TUI session.
    """
    last_selection: str | None = None
    loop = asyncio.get_running_loop()
    while True:
        # Two aligned columns — tool name, then its muted description — with no header
        # line: these are commands, not tabular data, so the alignment alone carries it.
        tools = [tool for tool in all_tools() if tool.menu_visible]

        def _label(tool) -> str:  # noqa: ANN001 - registry Tool; typed at the source
            title = tool.title or tool.name
            icon = command_icon(tool.icon)
            return f"{icon} {title}" if icon else title

        name_w = max((cell_len(_label(tool)) for tool in tools), default=0)
        items: list = []
        current_category: str | None = None
        for tool in tools:
            if tool.category != current_category:
                current_category = tool.category
                items.append(section_heading(current_category))
            label = _label(tool)
            row = Text(label)
            row.append(" " * (name_w - cell_len(label) + 2))
            row.append(tool.help, style="muted")
            # The tool's name is the row's identity and always fits; only the description
            # runs long, so ←→ slide it alone under a pinned name (Choice.hscroll_from).
            items.append(Choice(title=row, value=tool.name, hscroll_from=name_w + 2))
        items.append(Separator(" "))
        items.append(Choice(title=command_label("🚪 Quit"), value="__quit__"))

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
        ran_over_menu = False
        try:
            selection = await menu.future
            if selection is CANCEL:  # Esc at the top level
                selection = None
            # Both picking "quit" and pressing Esc ask to leave; confirm on a dialog floating
            # over the (still-pushed) menu so a stray key doesn't drop the user out. Cancel
            # (Esc) sits left of Quit (Enter); Quit starts highlighted so Enter commits it.
            if selection in (None, "__quit__"):
                # Offer to drop the OS pairing on the way out, but only when there is a live
                # Bluetooth bond to drop — never on serial, an open (PIN-less) companion, or a
                # platform we can't unpair. The extra button sits between Cancel and Quit and is
                # never the default, so it takes a deliberate choice, not a stray Enter.
                unpairable = await _can_unpair(ctx)
                buttons = [("Cancel", "cancel")]
                if unpairable:
                    buttons.append(("Unpair & quit", "unpair"))
                buttons.append(("Quit", "quit"))
                choice = await session.button_dialog(
                    "Are you sure you want to quit?",
                    buttons,
                    title="Quit MeshTerm",
                    default=len(buttons) - 1,  # highlight Quit
                    footer_hint="Enter quit · Esc cancel",
                    prompt_style="warn",
                    button_style="selected",
                    button_idle_style="muted",
                    border_style="warn",
                )
                if choice == "unpair":
                    # Forget the OS bond as we leave; the disconnect must happen first, so the
                    # actual unpair is deferred to teardown (see run_menu). The remembered-device
                    # record is intentionally kept — the device just asks for its PIN again.
                    ctx.unpair_on_exit = True
                    return
                if choice == "quit":
                    return
                # Cancel (button or Esc → None). Keep the cursor on "quit" when that is what
                # they chose, so a follow-up attempt lands where they expect.
                if selection == "__quit__":
                    last_selection = "__quit__"
                continue
            last_selection = selection
            # A popup tool runs while the menu is still pushed, so its prompts and
            # result float over it as modal dialogs instead of replacing the screen.
            tool = next((t for t in tools if t.name == selection), None)
            if tool is not None and tool.popup:
                ran_over_menu = True
                await _run_selection(ctx, selection)
        finally:
            session.pop(menu)
        if not ran_over_menu:
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
        from ..core.connection import probe_device
        from ..core.discovery import DiscoveredDevice, discover_all
        from .device_picker import prompt_device
        from .logo import load_logo

        baudrate = ctx.profile.baudrate if ctx.profile else 115200
        # Enumerate serial ports (instant) and scan for BLE companions (a few seconds), behind
        # the splash spinner so the wait reads as intentional rather than a hang.
        from .. import copyright_notice

        devices = await ctx.ui.busy_startup(
            "Scanning for companion devices…",
            discover_all(ble=True),
            title="Select a companion device",
            banner=load_logo(),
            footnote=copyright_notice(),
        )
        # The smoke test opens the radio; on success we keep that live connection and reuse
        # it for the session rather than reopening (boards often reset on each open).
        probed: dict = {}

        async def verify(device: DiscoveredDevice, pin: Optional[str] = None):
            # ``pin`` is what the picker's PIN dialog collected on a retry; fall back to any
            # ``--ble-pin`` supplied on the CLI for the first attempt.
            result = await probe_device(
                device, baudrate=baudrate, pin=pin if pin is not None else ctx.ble_pin
            )
            if result is None:
                return None
            connection, info = result
            probed["device_id"] = device.stable_id
            probed["device"] = connection
            return info

        chosen = await prompt_device(
            ctx.ui, devices, ctx.device_store, verify, ctx.settings.profiles
        )
        if chosen is None:
            return False  # the user quit at the splash — exit without opening the menu
        ctx.selected_device = chosen
        if chosen.is_tcp:
            ctx.tcp_override = chosen.target
            ctx.ble_override = None
            ctx.port_override = None
        elif chosen.is_ble:
            ctx.ble_override = chosen.address
            ctx.tcp_override = None
            ctx.port_override = None
        else:
            ctx.port_override = chosen.port
            ctx.ble_override = None
            ctx.tcp_override = None
        if probed.get("device_id") == chosen.stable_id:
            ctx.adopt_device(probed["device"])
    # Between the device splash and the first menu paint, resuming background listening opens
    # the radio — a slow, silent step that would otherwise leave the screen blank for a beat.
    # Float the skeleton card across that gap on any real link (see _busy_over_link).
    async with _busy_over_link(ctx, title="Starting up"):
        await _resume_monitor(ctx)
    # A forgetful device (a firmware-less radio bridge) may have lost settings you saved through
    # MeshTerm; offer to reconcile them on the splash before the menu paints. Quiet unless a
    # connected device actually drifts from what's remembered (see settings_offer).
    from .settings_offer import offer_remembered_settings

    await offer_remembered_settings(ctx)
    return True


async def _resume_monitor(ctx: AppContext) -> None:
    """Start always-on background listening and history recording.

    Recording has no switch: the monitor registers its hub subscription up front — a
    device-free, in-process step that cannot fail — so every overheard packet is logged
    from the moment the radio opens. The hub itself (a MeshCore client must always be
    listening) is started eagerly here unless the ``connect_on_start`` setting defers
    it, in which case it opens lazily when a tool first needs the radio and recording
    picks up then. A failure to start the hub (typically no companion device selected)
    is non-fatal; the header reflects the resulting state, so nothing needs to be
    printed here.

    Args:
        ctx: The shared application context.
    """
    await ctx.monitor.start()
    # The advert scheduler is safe to run from launch regardless of the connect policy:
    # each pass checks for a connected device and skips quietly without one, so starting
    # it here never opens the radio (and it simply waits out a deferred connect).
    await ctx.adverts.start()
    # The Watchtower only listens (rules over hub events), so it too is safe from launch.
    await ctx.watchtower.start()
    # The courier drains the outbox on its own paced schedule; each pass checks for a
    # connected device and skips quietly without one, exactly like the advert scheduler.
    await ctx.courier.start()
    # The battery poller reads the pack for the header's fuel gauge; it only ever reads,
    # and skips quietly without a device, so it too is safe to run from launch.
    await ctx.battery.start()
    if not ctx.settings.connect_on_start:
        return
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
    # Warm the map tile source's metadata behind the menu too: the first ``.max_zoom`` touch
    # resolves the TileJSON over the network, and the Map / Node-detail location preview would
    # otherwise pay that round-trip on the navigation path (a stall on opening a located node's
    # page). The tile source is independent of the radio link, so this runs regardless of
    # connect state — offline it just fails quietly and the map falls back to a default zoom.
    _warm_basemap(ctx)
    # Warm the slow session caches (contacts, the channel-slot probe) behind the menu now that
    # the link is up, so the first Chat/Trace/Dashboard open is served from cache instead of
    # paying those round-trips in the navigation path — one quiet wait after login rather than a
    # stall on the first open (see DeviceState.prewarm). Only with a live link: a deferred
    # connect (connect_on_start off) returns above and warms lazily on first use instead.
    if ctx.is_connected:
        ctx.devstate.prewarm()


def _warm_basemap(ctx: AppContext) -> None:
    """Resolve the shared tile source's TileJSON in the background (best-effort, once).

    Fire-and-forget: the resolve is a blocking network call, so it runs in a worker thread
    off the event loop, and any failure (offline, a slow source) is swallowed — the map
    simply falls back to its default max zoom. Warming it here means the first Map or
    Node-detail location preview opens from the already-resolved source instead of stalling
    on the round-trip.
    """
    async def _warm() -> None:
        try:
            await asyncio.to_thread(lambda: ctx.basemap_source.max_zoom)
        except Exception:  # noqa: BLE001 - the map works offline at a default zoom
            pass

    asyncio.ensure_future(_warm())


@asynccontextmanager
async def _busy_over_link(
    ctx: AppContext, *, title: str = ""
) -> AsyncIterator[None]:
    """Float the skeleton-card overlay for the wrapped block, on any real device link.

    Bluetooth is the worst offender, but serial navigation has a perceptible lag too, so the
    overlay is installed for either transport. Only the mock simulator (which has no link and
    answers instantly, so ``active_transport`` is ``None``) opts out. The overlay's own reveal
    delay still suppresses a flash on genuinely quick operations, so this never flickers.

    Args:
        ctx: The shared application context (read for the active transport and UI surface).
        title: An optional heading for the card, naming the screen being fetched.
    """
    if ctx.active_transport is not None:
        async with ctx.ui.busy_overlay(_reading_caption(ctx), title=title):
            yield
    else:
        yield


def _reading_caption(ctx: AppContext) -> str:
    """The skeleton card's caption: "reading from <companion>…", or a bare "reading…".

    Names the chosen companion when one is known this session, so the wait reads as a
    concrete fetch from a specific device rather than an abstract stall.
    """
    device = ctx.selected_device
    if device is not None and device.label:
        return f"reading from {device.label}…"
    return "reading…"


async def _run_selection(ctx: AppContext, name: str) -> None:
    """Gather parameters for, execute, and present a single tool from the menu.

    A dropped device link is not handled here: the session-wide watcher (see
    :func:`_session_loop`) polls independently and raises the reconnect dialog wherever the
    session is, so on a connection-lost error we simply drop the tool's half-built output and
    return — the watcher takes it from there within a poll interval.

    Args:
        ctx: The shared application context.
        name: The selected tool's name.
    """
    from ..core.connection import is_connection_lost
    from ..tools import get_tool

    tool = get_tool(name)
    if tool is None:  # pragma: no cover - registry and menu are always in sync
        ctx.ui.note(f"[err]Unknown tool: {name}[/err]")
        await ctx.ui.present(title=name)
        return

    title = tool.title or tool.name
    try:
        # Opening a tool can sit on a blank frame while the companion answers (the menu has
        # been popped, its first prompt not yet pushed) — very noticeable over Bluetooth, but
        # perceptible on serial too. Float the skeleton card through that gap so the wait reads
        # as work, not a hang. The overlay only paints between screens, so prompts show through.
        async with _busy_over_link(ctx, title=title):
            params = await tool.prompt_params(ctx)
            if params is None:  # user cancelled a prompt
                ctx.ui.discard()
                return
            result = await tool.execute(ctx, params)
    except Exception as exc:  # noqa: BLE001 - surface errors without crashing the menu
        if is_connection_lost(exc):
            ctx.ui.discard()  # drop the half-built output; the watcher will prompt to reconnect
            return
        ctx.ui.note(f"[err]✗ {title} failed:[/err] {exc}")
        await ctx.ui.present(title=title)
        return

    if result.message:
        ctx.ui.note(result.message)
    for artifact in result.artifacts:
        ctx.ui.note(f"[ok]●[/ok] wrote [accent]{artifact}[/accent]")
    await ctx.ui.present(title=title)


async def _session_loop(ctx: AppContext, session: TuiSession) -> None:
    """Run the menu with a single always-on watcher that handles a disconnect anywhere.

    The menu loop runs as a cancellable worker alongside one session-wide liveness watcher.
    Whichever finishes first wins: if the user quits, the menu loop returns and the watcher is
    stopped; if the device is unplugged — no matter which screen is up (a tool prompt, the
    chat view, the map, or the idle menu) — the watcher fires, the in-flight work is cancelled,
    the screen stack is unwound, and the reconnect dialog is shown. On a successful reconnect
    the loop starts a fresh menu; on quit it returns.

    Args:
        ctx: The shared application context.
        session: The running TUI session.
    """
    while True:
        worker = asyncio.ensure_future(_menu_loop(ctx, session))
        watcher = asyncio.ensure_future(_wait_for_disconnect(ctx))
        done, _ = await asyncio.wait(
            {worker, watcher}, return_when=asyncio.FIRST_COMPLETED
        )
        if worker in done:
            await _cancel_and_wait(watcher)
            worker.result()  # user quit (or re-raise a menu-loop error)
            return
        # The device link dropped. Abandon whatever the menu was doing, clear any screens it
        # left, and prompt to reconnect or quit over a clean frame.
        await _cancel_and_wait(worker)
        session.reset()
        if await _handle_disconnect(ctx, session):
            return  # user chose to quit
        # Reconnected — loop and start a fresh menu (with a fresh watcher).


async def _cancel_and_wait(task: "asyncio.Future") -> None:
    """Cancel ``task`` and await its unwind, swallowing the cancellation and any error.

    Used to stop the sibling menu-worker or watcher: awaiting the cancelled task lets its
    ``finally`` blocks run (popping any screens it pushed) before we continue, and its now-moot
    error — the link is gone — is intentionally discarded. Awaiting also consumes the result
    of an already-finished task, so no stray "exception never retrieved" warning is logged.
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - a cancelled worker's error is moot; move on
        pass


async def _wait_for_disconnect(ctx: AppContext) -> None:
    """Resolve once the connected companion's transport link drops.

    Polls a cheap, non-invasive liveness check (:meth:`AppContext.link_alive`) rather than
    watching for a failed command: the ``meshcore`` client keeps serving cached data after a
    serial unplug and never raises (confirmed on hardware), so an *active* liveness check is
    the only thing that reliably notices a pulled cable. The same check covers Bluetooth,
    where it reads the BLE client's connection flag (which flips the moment the peripheral
    drops or goes out of range). Never resolves for the simulator (it can't be unplugged) or
    before a real device has actually been opened.

    Args:
        ctx: The shared application context (read for the connection state and liveness).
    """
    if ctx.mock:
        await asyncio.Event().wait()  # the simulator is never "unplugged"; wait forever
        return
    while True:
        await asyncio.sleep(_LIVENESS_POLL_S)
        if not ctx.is_connected:
            continue  # nothing connected to watch yet (deferred connect / no device)
        if await ctx.link_alive():
            continue
        await asyncio.sleep(_LIVENESS_CONFIRM_S)  # debounce a transient enumeration/link gap
        if not await ctx.link_alive():
            return


async def _handle_disconnect(ctx: AppContext, session: TuiSession) -> bool:
    """Show a reconnect popup and watch for the device to return, auto-resuming on replug.

    Presents a centered, warn-styled dialog (:class:`~meshterm.ui.tui.prompt.ReconnectDialog`)
    with an animated spinner and a single Quit button. Two background tasks run behind it: one
    animates the spinner, the other polls for the device's port to re-enumerate and, once it
    does, keeps attempting :meth:`~meshterm.context.AppContext.reconnect` until one succeeds —
    a board can re-enumerate a moment before it will answer, so a failed attempt just retries.
    Whichever resolves first wins: a successful reconnect dismisses the dialog and resumes the
    session; the user pressing Quit (or Enter) leaves. The spinner stays up the whole time.

    Args:
        ctx: The shared application context.
        session: The running TUI session.

    Returns:
        ``True`` if the user chose to quit; ``False`` once the device reconnected.
    """
    # A drop the config editor announced (it just sent a reboot command) is expected, so
    # the dialog says what is actually happening instead of implying an unplug. The flag
    # is consumed here so a later, genuine disconnect goes back to the generic wording.
    if ctx.reboot_in_progress:
        ctx.reboot_in_progress = False
        dialog = ReconnectDialog(
            "Rebooting — waiting for the device to come back…",
            title="Device rebooting",
        )
    else:
        dialog = ReconnectDialog("Waiting for your device — reconnect it to resume.")
    dialog.future = asyncio.get_running_loop().create_future()
    # The session stack was cleared before we were called (see _session_loop), so push a
    # clean, empty base frame for the popup to float over. A lone floating screen with nothing
    # beneath it is drawn as the *base* (framed chrome, no centered panel) rather than as a
    # window — the base gives it something to center over, both horizontally and vertically.
    base = ScrollScreen("", floating=False, footer_hint="")
    session.push(base)
    session.push(dialog)
    animator = asyncio.ensure_future(_animate_dialog(session, dialog))
    reconnector = asyncio.ensure_future(_auto_reconnect(ctx, dialog))
    try:
        result = await dialog.future
    finally:
        await _cancel_and_wait(reconnector)
        await _cancel_and_wait(animator)
        session.pop(dialog)
        session.pop(base)
    return result == "quit"


async def _animate_dialog(session: TuiSession, dialog: ReconnectDialog) -> None:
    """Advance the reconnect dialog's spinner and repaint on a steady cadence, until cancelled."""
    while True:
        await asyncio.sleep(spinner_interval())
        dialog.tick()
        session.invalidate()


async def _auto_reconnect(ctx: AppContext, dialog: ReconnectDialog) -> None:
    """Poll for the device to return, reconnect when it does, then dismiss ``dialog``.

    For serial, waits for the OS to re-enumerate the port the connection was opened on before
    each attempt, so a reconnect is only tried once there's a device to reach. For Bluetooth
    there is no cheap "is it back yet" probe short of a full scan, so it simply attempts a
    reconnect each interval — ``create_ble`` connects directly by address and fails fast when
    the peripheral isn't in range. Either way a failed attempt (the endpoint is back but the
    board isn't ready yet, or it dropped again) just loops and retries. On the first success
    the dialog's future is resolved with ``"reconnected"``, dismissing the popup. Runs until
    it succeeds or the task is cancelled (the user quit).

    Args:
        ctx: The shared application context.
        dialog: The reconnect dialog to dismiss once the link is back.
    """
    from ..core.connection import serial_port_present

    while True:
        # Serial: wait for the port to re-appear before touching the radio. BLE/TCP (and an
        # unknown/deferred serial port): skip the wait and just retry the reconnect itself —
        # ``create_ble``/``create_tcp`` connect by address/endpoint and fail fast when absent.
        if ctx.active_transport == "serial":
            port = ctx.active_port
            if port is not None and not serial_port_present(port):
                await asyncio.sleep(_LIVENESS_POLL_S)
                continue
        try:
            await ctx.reconnect()
        except Exception:  # noqa: BLE001 - not reachable yet; keep the popup up and retry
            await asyncio.sleep(_LIVENESS_POLL_S)
            continue
        dialog.resolve("reconnected")
        return


# Exposed for tools that need a standalone console outside a context (rare).
default_console = make_console
