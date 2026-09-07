"""Typer entry point.

Global options are parsed once in the callback, which builds the shared
:class:`~meshterm.context.AppContext`. Running with no subcommand launches the
interactive menu; otherwise the selected tool's subcommand runs. Both paths funnel
through :func:`run_tool_command` / :func:`run_menu`, so behavior stays identical.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer
from rich.console import Console

from .context import AppContext
from .core import win32dll
from .core.admin_store import AdminStore
from .core.config import Settings
from .core.connection import DeviceCommandError, is_connection_lost
from .core.device_config import DeviceConfigError
from .core.device_store import DeviceStore
from .core.instancelock import InstanceBusy, hold_instance_lock
from .core.preferences import PreferenceError, Preferences
from .core.selection import DeviceSelectionError
from .persistence.logging import configure_logging, get_logger, level_from_name, log_path
from .persistence.repository import Repository
from .platforms import Resolution, resolve, set_platform, without_emoji
from .tools import all_tools
from .tools.base import Tool, ToolResult
from .ui.termfont import emoji_support
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
    # The one description, same as the package summary and the PyPI page. See
    # `meshterm.__doc__` — if this drifts from that, one of them is lying.
    help=(
        "MeshTerm is a full-featured TUI-based MeshCore client for your terminal. "
        "Supports USB, Bluetooth and TCP companion connection. "
        "For Windows, macOS and Linux."
    ),
)

# The context built by the callback and consumed by subcommands within one process.
_state: AppContext | None = None

# The platform resolution the callback made, for the ``platform`` diagnostic subcommand
# to report back (see :func:`platform_command`) without re-deriving it independently.
_platform_resolution: Resolution | None = None


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    profile: str | None = typer.Option(None, "--profile", "-p", help="Device profile"),
    port: str | None = typer.Option(None, "--port", help="Serial port override"),
    ble: str | None = typer.Option(
        None, "--ble", help="Bluetooth address of a companion device (selects the BLE transport)"
    ),
    ble_pin: str | None = typer.Option(
        None, "--ble-pin", help="BLE pairing PIN, if the Bluetooth companion requires one"
    ),
    tcp: str | None = typer.Option(
        None,
        "--tcp",
        help="Network address host[:port] of a TCP companion (selects the TCP transport)",
    ),
    mock: bool = typer.Option(False, "--mock", help="Use the built-in simulator"),
    db_path: Path | None = typer.Option(None, "--db", help="SQLite database path"),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress console logging"),
    platform: str | None = typer.Option(
        None,
        "--platform",
        help="Force the UI platform (regular|picocalc) instead of auto-detecting it",
    ),
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
        platform: Explicit ``--platform`` override; see :func:`meshterm.platforms.resolve`.
    """
    global _state, _platform_resolution

    # Resolved and installed first, before anything below it (make_console, the theme,
    # the eventual TuiSession) reads the active platform.
    try:
        _platform_resolution = resolve(platform)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--platform") from exc
    # A terminal that cannot draw emoji is asked about here rather than in resolve(),
    # which answers "which flavour" from the flag, the env and the device tree and should
    # stay that pure — this is the *terminal's* limit, and it applies whichever flavour
    # those three chose. See meshterm.ui.termfont.emoji_support.
    active = _platform_resolution.platform
    if not emoji_support().supported:
        active = without_emoji(active)
    set_platform(active)

    settings = Settings.load()
    if db_path is not None:
        settings.db_path = db_path

    console = make_console()
    # Loaded before logging is configured, because how much goes in the file is one of
    # them — and handed to the context afterwards so the file is not read twice.
    prefs = Preferences.load(settings.config_dir / "preferences.yaml")
    configure_logging(
        console,
        settings.config_dir,
        level=logging.INFO,
        file_level=level_from_name(prefs.log_level),
        quiet=quiet or json_output,
    )

    app_ctx = AppContext(
        preferences=prefs,
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

        # Before prompt_toolkit takes the screen, because this changes the console's own
        # font and the full-screen frame should be drawn once, in whatever it ends up.
        _offer_a_font_this_console_can_draw(console, prefs)

        # The interactive session is the one that holds the stores in memory and rewrites
        # them whole, so it is the one that claims the directory. One-shot subcommands are
        # brief and mostly read; blocking `meshterm contacts` because a menu is open in
        # another window would be an obstacle rather than a guard.
        try:
            with hold_instance_lock(settings.config_dir):
                asyncio.run(_drive(run_menu(app_ctx), app_ctx))
        except InstanceBusy as exc:
            console.print(f"[err]✗[/err] {exc}")
            raise typer.Exit(code=1) from None
        except Exception as exc:
            if type(exc).__name__ == "NoConsoleScreenBufferError":
                # prompt_toolkit needs a real Win32 console and reports its absence in the
                # language of its own internals. A traceback reads as "this app is broken"
                # when the answer is "open a different terminal", so answer that instead.
                get_logger().warning("no Windows console available: %s", exc)
                _report_no_windows_console(console)
                raise typer.Exit(code=1) from None
            # Logged before it is re-raised, and this is the case the log matters most for:
            # a session launched by double-clicking the executable owns its console window,
            # so when it dies the window closes with it and takes the traceback along. The
            # file is the only thing left to read afterwards.
            get_logger().exception("the interactive session raised an unhandled error")
            _report_log_location(console, settings.config_dir)
            raise


def _offer_a_font_this_console_can_draw(console: Console, prefs: Preferences) -> None:
    """On the classic Windows console, offer a font that can actually draw MeshTerm.

    That console does no font fallback: whatever its font lacks is a box, and its default
    — Consolas, or Lucida Console under Windows PowerShell — lacks most of what MeshTerm
    is drawn with, including every one of the 44 braille cells the timelines are made of.
    Nothing at the app's end fixes that, so this is the one thing left to offer.

    Two shapes, depending on what is already there: a machine with Windows Terminal (and
    every Windows 11 machine) already has Cascadia, so the offer is only to *use* it; a
    bare Windows 10 gets the offer to install the copy MeshTerm ships.

    Declining is answered with a plain description of what declining looks like, and then
    asked once more — a reader who has never seen the app has no way to know what "some
    glyphs may not render" is going to mean. A second decline is remembered, in the
    ``console_font`` preference, so the question is asked twice in total and never again.

    Args:
        console: The console to ask on.
        prefs: The preference set, read for a previous refusal and written on a new one.
    """
    from .core import consolefont
    from .ui.termfont import classic_console, face_draws_charts, installed_chart_font

    if not classic_console() or prefs.get("console_font") == "off":
        return
    if face_draws_charts(consolefont.current_face()):
        return

    installed = installed_chart_font()
    for asking_again in (False, True):
        if asking_again:
            console.print()
            console.print("[warn]⚠[/warn]  Then MeshTerm is going to look wrong here.")
            console.print()
            console.print("   Every chart — the signal timelines, the activity graphs — is")
            console.print("   drawn from braille characters this console's font does not")
            console.print("   have, so they will come out as rows of empty boxes. So will")
            console.print("   the ✓ and ✗ marks, and the ★ on the map.")
            console.print()
            console.print("   Nothing is broken and nothing is lost — the app works, and")
            console.print("   this is only about what the font can draw. It is your")
            console.print("   console, so it is your call.")
            console.print()
        elif installed:
            console.print()
            console.print("[accent]•[/accent]  This console's font can't draw MeshTerm's charts.")
            console.print(f"   You already have [accent]{installed}[/accent], which can.")
            console.print()
        else:
            console.print()
            console.print("[accent]•[/accent]  This console's font can't draw MeshTerm's charts.")
            console.print("   MeshTerm ships [accent]Cascadia Mono PL[/accent] — Microsoft's")
            console.print("   console font — and can install it just for you. No")
            console.print("   administrator rights, nothing downloaded, 723 KB.")
            console.print()

        verb = "Use it" if installed else "Install and use it"
        if _asks_yes(console, f"   {verb}? [y/N] "):
            _use_a_better_console_font(console, installed)
            return

    prefs.set("console_font", "off")
    try:
        prefs.save()
    except (OSError, RuntimeError) as exc:  # a read-only config dir is not fatal here
        get_logger().warning("could not remember the console font choice: %s", exc)
    console.print()
    console.print("[muted]   Leaving it as it is. Change your mind on the Preferences[/muted]")
    console.print("[muted]   page, under Display → Console font.[/muted]")
    console.print()


def _use_a_better_console_font(console: Console, installed: str | None) -> None:
    """Install (when needed) and select the font, and say plainly what happened."""
    from .core import consolefont

    face = installed
    if face is None:
        if not consolefont.install_bundled_font():
            console.print("[err]✗[/err]  The font could not be installed. Leaving the")
            console.print("   console as it is.")
            get_logger().warning("bundled console font could not be installed")
            return
        face = consolefont.BUNDLED_FACE

    if consolefont.use(face):
        console.print(f"[ok]✓[/ok]  Now drawing with [accent]{face}[/accent].")
        get_logger().info("console font set to %s", face)
    else:
        # Installed but not selected: the font is there for next time and for the
        # console's own properties dialog, which is worth saying rather than swallowing.
        console.print(f"[warn]⚠[/warn]  {face} is installed, but this console kept its own")
        console.print("   font. It will be available the next time you open one.")
        get_logger().warning("console refused the font %s", face)


def _asks_yes(console: Console, prompt: str) -> bool:
    """Ask a yes/no question on the console, defaulting to no.

    Not a TUI dialog: this runs before prompt_toolkit owns the screen, alongside the other
    plain-console reports here. An unanswerable prompt — no stdin, a closed pipe — reads as
    "no", which is the answer that changes nothing.

    Args:
        console: The console to ask on.
        prompt: The question, ending in its own spacing.

    Returns:
        Whether the reader said yes.
    """
    console.print(prompt, end="")
    try:
        return input().strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt, OSError):
        console.print()
        return False


def _report_log_location(console: Console, config_dir: Path) -> None:
    """Point at the log after a fault, since it is the copy that outlives the terminal."""
    console.print()
    console.print(f"[muted]The details are in {log_path(config_dir)}[/muted]")
    console.print("[muted]Attach it to a bug report. For more detail next time, raise[/muted]")
    console.print("[muted]'Log detail' on the Preferences page.[/muted]")


def _report_no_windows_console(console: Console) -> None:
    """Explain a console MeshTerm cannot draw in, and how to get one it can.

    Reached when prompt_toolkit finds no Win32 console screen buffer: a Git Bash, MSYS or
    Cygwin shell — which announce themselves as ``xterm`` and are not Windows consoles at
    all — or a process whose output has been redirected away from one.
    """
    console.print("[err]✗[/err] MeshTerm needs a Windows console, and could not find one.")
    console.print()
    console.print("That usually means one of two things:")
    console.print()
    console.print("  • You are in Git Bash, MSYS or Cygwin. Those are not Windows consoles.")
    console.print("    Open [accent]Windows Terminal[/accent] or [accent]PowerShell[/accent]")
    console.print("    and run it from there.")
    console.print("  • The output is piped or redirected somewhere. The full-screen menu")
    console.print("    needs the terminal itself.")
    console.print()
    console.print("Every screen also has a subcommand, and those pipe fine —")
    console.print("try [accent]meshterm --help[/accent].")


@app.command(name="platform")
def platform_command() -> None:
    """Print the resolved platform and every input the resolution looked at.

    A diagnostic for the platform seam (see ``meshterm/platforms.py``): confirms which
    flavour a given invocation would run as, and why — the ``--platform``/
    ``MESHTERM_PLATFORM`` inputs, what ``/proc/device-tree/model`` reports (``None`` off
    the actual hardware), and which of those decided it.

    It also reports the terminal's own emoji verdict, which is a separate question from
    the flavour and the usual reason a Windows session looks plainer than the screenshots
    (see :func:`meshterm.ui.termfont.emoji_support`).
    """
    assert _platform_resolution is not None  # set by the callback that always runs first
    r = _platform_resolution
    console = make_console()
    console.print(f"platform: [accent]{r.platform.name}[/accent]  (source: {r.source})")
    console.print(f"  --platform:              {r.flag or '(not passed)'}")
    console.print(f"  MESHTERM_PLATFORM:       {r.env or '(not set)'}")
    console.print(f"  /proc/device-tree/model: {r.detected_model or '(unavailable)'}")

    emoji = emoji_support()
    verdict = "yes" if emoji.supported else "no — icons use the compact glyphs"
    console.print(f"icons:    [accent]{verdict}[/accent]  (source: {emoji.source})")
    if not emoji.supported and emoji.source == "console":
        console.print(
            "  this is the classic Windows console, which draws no emoji whatever font\n"
            "  it is set to. Windows Terminal and VS Code's terminal both do — running\n"
            "  MeshTerm in one gets the icons back. MESHTERM_EMOJI=1 overrides this."
        )


@app.command(name="specimen")
def specimen_command() -> None:
    """Print the visual-language specimen for the active platform.

    Every mark, icon funnel, colour scale and fold on one card, drawn through the same
    theme and glyph machinery the TUI uses — so on the PicoCalc console it is the
    font/palette acceptance screen, and with ``--platform picocalc`` on a desktop it
    previews that flavour. See :mod:`meshterm.ui.specimen`.
    """
    from .ui.specimen import specimen_lines

    console = make_console()
    for line in specimen_lines():
        console.print(line)


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
    except (DeviceSelectionError, DeviceConfigError, DeviceCommandError, PreferenceError) as exc:
        # Expected user-facing error (ambiguous/absent device, a bad config or preference
        # value, or a transient command failure): show the message, not a traceback. Logged
        # at warning because it is a thing that went wrong, even though it is an ordinary
        # one — a log that only holds crashes cannot answer "what happened before it".
        get_logger().warning("%s failed: %s", tool.name, exc)
        _state.console.print(f"[err]✗[/err] {exc}")
        raise typer.Exit(1) from exc
    except Exception as exc:
        # A dropped serial link (device unplugged/powered off mid-command) can't be recovered
        # from in a one-shot scripted run the way the interactive menu offers — but it should
        # still read as a clean message, not a traceback.
        if is_connection_lost(exc):
            get_logger().warning("%s lost the device connection: %s", tool.name, exc)
            _state.console.print(
                "[err]✗[/err] the connection to your device was lost "
                "(it may have been unplugged or powered off)."
            )
            raise typer.Exit(1) from exc
        # Anything else is a real fault. It still reaches the terminal as a traceback,
        # because a person looking at one wants to see it — but it also lands in the log,
        # which is the copy that survives the terminal being closed and is the one thing
        # worth attaching to a bug report.
        get_logger().exception("%s raised an unhandled error", tool.name)
        _report_log_location(_state.console, _state.settings.config_dir)
        raise


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


def _owns_its_console() -> bool:
    """Whether this process is the only one on its console — i.e. it was double-clicked.

    Windows gives a console to a process launched from Explorer and destroys it the moment
    that process ends, so an error message is displayed and erased in the same instant.
    Launched from a shell instead, the console belongs to the shell and survives.

    ``GetConsoleProcessList`` tells the two apart: it reports how many processes are
    attached to this console. One is us, alone, holding a window that dies with us.

    Returns:
        ``True`` only on Windows, and only when nothing else shares the console.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        buffer = (ctypes.c_uint * 4)()
        count = win32dll.kernel32().GetConsoleProcessList(buffer, 4)
    except Exception:  # pragma: no cover - no console at all, or a stubbed kernel32
        return False
    return count == 1


def main() -> None:
    """Console-script entry point.

    Holds the window open after a failure when MeshTerm owns it, because otherwise the
    error is drawn and destroyed together and the person who needs to read it sees a
    flash. Only on that path: a successful run should not make anyone press a key, and a
    run from a shell leaves its output on the screen anyway.
    """
    try:
        app()
    except SystemExit as exit_request:
        if exit_request.code and _owns_its_console():
            _wait_before_the_window_closes()
        raise
    except BaseException:
        if _owns_its_console():
            _wait_before_the_window_closes()
        raise


def _wait_before_the_window_closes() -> None:
    """Ask for a keypress so a message stays readable, and never fail doing it."""
    try:
        print()
        print("-- Press Enter to close this window --")
        input()
    except Exception:  # pragma: no cover - stdin closed, redirected, or gone
        pass


if __name__ == "__main__":
    main()
