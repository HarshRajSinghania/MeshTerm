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
from .core import exitcodes, win32dll
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
from .ui import script
from .ui.termfont import emoji_support
from .ui.theme import make_console

#: The exit-status table, printed under every ``--help``. A documented return value is
#: half of what makes the CLI scriptable (the other half is :mod:`meshterm.ui.script`),
#: and a caller should not have to find it in a README.
EXIT_STATUS_EPILOG = "Exit status: " + " · ".join(
    f"{code} {meaning}" for code, meaning in exitcodes.MEANINGS.items()
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    # Click's own plain help, not Typer's rich one: no boxed Options/Commands panels, no
    # colour, no markup to strip out of a piped `--help`. The CLI is read by scripts, and
    # its help is read the way every other utility's is (see meshterm.ui.script).
    rich_markup_mode=None,
    # The one description, same as the package summary and the PyPI page. See
    # `meshterm.__doc__` — if this drifts from that, one of them is lying.
    help=(
        "MeshTerm is a full-featured TUI-based MeshCore client for your terminal. "
        "Supports USB, Bluetooth and TCP companion connection. "
        "For Windows, macOS and Linux."
    ),
    epilog=EXIT_STATUS_EPILOG,
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

    # Two front ends, two consoles, and which one this process gets is settled here — a
    # named subcommand is a scripted run, so it prints on the plain, colourless,
    # never-wrapping console the CLI's output language is written for (see
    # meshterm.ui.script); no subcommand means the menu, which needs the themed one.
    scripted = ctx.invoked_subcommand is not None
    console = script.console() if scripted else make_console()
    # Loaded before logging is configured, because how much goes in the file is one of
    # them — and handed to the context afterwards so the file is not read twice.
    prefs = Preferences.load(settings.config_dir / "preferences.yaml")
    configure_logging(
        # A scripted run's stdout carries its answer and nothing else, so log records go
        # to stderr instead of interleaving with the data a caller is parsing.
        script.stderr_console() if scripted else console,
        settings.config_dir,
        # And it only hears about faults. Every *expected* failure is already reported on
        # stderr in one line and in the exit status (see `_reported`), so echoing the same
        # thing again as a WARNING record would say it twice; the file keeps both either
        # way. The menu stays at INFO, where the log pane is the only place these show.
        level=logging.ERROR if scripted else logging.INFO,
        file_level=level_from_name(prefs.log_level),
        quiet=quiet or json_output,
    )

    if profile is not None and settings.resolve_profile(profile) is None:
        # Falling through to ordinary discovery would pick whichever radio is attached, so
        # a typo in a scheduled `--profile yagi config advert` transmits from the wrong
        # node rather than failing. Naming a profile is a claim about *which* device, and
        # an unkeepable claim is a bad argument.
        known = ", ".join(sorted(settings.profiles)) or "none are defined"
        raise typer.BadParameter(f"no device profile named {profile!r} (known: {known})")

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

        # Before prompt_toolkit takes the screen: this can change the console's own font,
        # or hand the whole session to a better terminal, and the full-screen frame should
        # be drawn once, in whatever it ends up in.
        if _offer_a_console_that_can_draw_meshterm(console, prefs):
            return

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


def _offer_a_console_that_can_draw_meshterm(console: Console, prefs: Preferences) -> bool:
    """On the classic Windows console, get the reader somewhere better than it.

    Two remedies, and they are not asked the same way.

    **Windows Terminal** first, and simply done. It is the only thing that gets the whole
    app — emoji icons included, which no font can put on that console, the only Windows
    fonts carrying any being proportional and a console taking none of them. Moving there
    installs nothing and changes nothing, so there is one sensible answer to a question
    that would be put to someone who has not seen the app yet and cannot judge it.

    **A font** second, only where the move was not possible, and that one *is* asked —
    it writes a file onto someone's machine, which is theirs to agree to.

    Args:
        console: The console to report and ask on.
        prefs: The preference set, read for a previous refusal and written on a new one.

    Returns:
        ``True`` when the session is being reopened elsewhere and this one should stop.
    """
    from .ui.termfont import classic_console

    if not classic_console() or prefs.get("console_setup") == "off":
        return False
    if _move_to_windows_terminal(console):
        return True
    _offer_a_font_this_console_can_draw(console, prefs)
    return False


def _move_to_windows_terminal(console: Console) -> bool:
    """Reopen the session in Windows Terminal, where the app can draw all of itself.

    Done rather than offered. The classic console cannot show a single icon whatever it is
    given, so "reopen there?" is a question with one sensible answer, asked of someone who
    has not seen the app yet and so cannot judge it — and asked at the worst moment, before
    anything has been drawn. Moving costs nothing and changes nothing: no install, no
    setting, one window instead of another.

    It is announced rather than silent, and the announcement names the way to stop it,
    because a program that opens a window you did not ask for should at least say so.
    ``console_setup`` set to ``off`` keeps the session here for good.

    Args:
        console: The console to report on.

    Returns:
        Whether Windows Terminal took over and this session should stand down.
    """
    from .core import winterminal

    if winterminal.available() is None:
        return False

    console.print()
    console.print("[accent]•[/accent]  Opening in [accent]Windows Terminal[/accent] — this console")
    console.print("   can't draw MeshTerm's icons or charts.")
    console.print("[muted]   Preferences → Display → Console setup keeps it here instead.[/muted]")
    console.print()

    if winterminal.reopen():
        get_logger().info("reopened the session in Windows Terminal")
        return True
    console.print("[warn]⚠[/warn]  Windows Terminal would not start. Staying here.")
    get_logger().warning("could not start Windows Terminal")
    return False


def _offer_a_font_this_console_can_draw(console: Console, prefs: Preferences) -> None:
    """Offer a font that can at least draw the charts and marks, for a reader who stays.

    The console does no font fallback: whatever its font lacks is a box, and its default
    — Consolas, or Lucida Console under Windows PowerShell — lacks most of what MeshTerm
    is drawn with, including every one of the 44 braille cells the timelines are made of.

    Two shapes, depending on what is already there: a machine with Windows Terminal (and
    every Windows 11 machine) already has Cascadia, so the offer is only to *use* it; a
    bare Windows 10 gets the offer to install the copy MeshTerm ships.

    Declining is answered with a plain description of what declining looks like, and then
    asked once more — a reader who has never seen the app has no way to know what "some
    glyphs may not render" is going to mean. A second decline is remembered, in the
    ``console_setup`` preference, so the question is asked twice in total and never again.

    Args:
        console: The console to ask on.
        prefs: The preference set, read for a previous refusal and written on a new one.
    """
    from .core import consolefont
    from .ui.termfont import face_draws_charts, installed_chart_font

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

    prefs.set("console_setup", "off")
    try:
        prefs.save()
    except (OSError, RuntimeError) as exc:  # a read-only config dir is not fatal here
        get_logger().warning("could not remember the console setup choice: %s", exc)
    console.print()
    console.print("[muted]   Leaving it as it is. Change your mind on the Preferences[/muted]")
    console.print("[muted]   page, under Display → Console setup.[/muted]")
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


def _asks_yes(console: Console, prompt: str, *, default: bool = False) -> bool:
    """Ask a yes/no question on the console.

    Not a TUI dialog: this runs before prompt_toolkit owns the screen, alongside the other
    plain-console reports here. An unanswerable prompt — no stdin, a closed pipe — takes
    the default, and each caller's default is the answer that costs the reader least.

    Args:
        console: The console to ask on.
        prompt: The question, ending in its own spacing.
        default: The answer an empty line means, and the one an unreadable stdin takes.

    Returns:
        Whether the reader said yes.
    """
    console.print(prompt, end="")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        console.print()
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


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
    assert _state is not None  # ditto
    r = _platform_resolution
    emoji = emoji_support()
    _state.console.print(
        script.pairs(
            [
                ("platform", r.platform.name),
                ("platform_source", r.source),
                ("flag", r.flag or script.NONE),
                ("env", r.env or script.NONE),
                ("device_tree_model", r.detected_model or script.NONE),
                ("icons", "yes" if emoji.supported else "no"),
                ("icons_source", emoji.source),
            ]
        )
    )


@app.command(name="specimen")
def specimen_command() -> None:
    """Print the visual-language specimen for the active platform.

    Every mark, icon funnel, colour scale and fold on one card, drawn through the same
    theme and glyph machinery the TUI uses — so on the PicoCalc console it is the
    font/palette acceptance screen, and with ``--platform picocalc`` on a desktop it
    previews that flavour. See :mod:`meshterm.ui.specimen`.

    The one command that keeps its colour, and it builds its own themed console to do it
    (rather than the plain one every other subcommand prints on — see
    :mod:`meshterm.ui.script`). This is not scripted output that happens to be pretty: the
    colour *is* the output. A monochrome specimen would test nothing.
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
        result = asyncio.run(_drive(_execute_and_render(tool, params, _state), _state))
    except DeviceSelectionError as exc:
        # No companion could be picked at all: nothing was transmitted, and no retry is
        # going to help until the caller attaches a device or names one.
        raise _reported(tool, exc, exitcodes.NO_DEVICE) from exc
    except DeviceCommandError as exc:
        # A device was reached and the operation failed. Worth retrying.
        raise _reported(tool, exc, exitcodes.DEVICE) from exc
    except (DeviceConfigError, PreferenceError) as exc:
        # A value we were given is wrong — a bad setting key, an out-of-range preference.
        # Retrying changes nothing; the caller has to change what it asked for.
        raise _reported(tool, exc, exitcodes.FAILURE) from exc
    except (typer.BadParameter, typer.Abort, typer.Exit):
        # A tool that rejected one of its own arguments (an unresolvable `--to`, a
        # malformed path) raised the same thing the parser would have. It is a usage
        # error, Click already knows how to print it and what to exit with, and it is not
        # a fault — so it passes through without a traceback or a pointer to the log.
        raise
    except Exception as exc:
        # A dropped serial link (device unplugged/powered off mid-command) can't be
        # recovered from in a one-shot scripted run the way the interactive menu offers —
        # but it reads as a clean message, not a traceback, and as a device failure.
        if is_connection_lost(exc):
            raise _reported(
                tool,
                "the connection to the device was lost (it may have been unplugged or powered off)",
                exitcodes.DEVICE,
            ) from exc
        # Anything else is a real fault. It still reaches the terminal as a traceback,
        # because a person looking at one wants to see it — but it also lands in the log,
        # which is the copy that survives the terminal being closed and is the one thing
        # worth attaching to a bug report.
        get_logger().exception("%s raised an unhandled error", tool.name)
        _report_log_location(script.stderr_console(), _state.settings.config_dir)
        raise
    if result.exit_code != exitcodes.OK:
        raise typer.Exit(result.exit_code)


def _reported(tool: Tool, problem: object, code: int) -> typer.Exit:
    """Log a failure, say so on stderr, and build the exit it should end on.

    The message goes to stderr in the ``program: what went wrong`` shape every utility
    uses, so a caller redirecting stdout still sees it and a caller parsing stdout never
    has to filter it out. It carries no mark and no colour — the exit status is what says
    this failed, which is why it is classified (see :mod:`meshterm.core.exitcodes`).

    It is logged at warning rather than error because these are the *ordinary* failures —
    an absent device, a wrong password — and a log holding only crashes cannot answer
    "what happened just before it".

    Args:
        tool: The tool that failed, for the log line.
        problem: The exception or message to report.
        code: The status to exit with.

    Returns:
        The :class:`typer.Exit` for the caller to raise.
    """
    get_logger().warning("%s failed: %s", tool.name, problem)
    script.stderr_console().print(f"meshterm: {problem}", style="err", highlight=False)
    return typer.Exit(code)


async def _drive(coro, ctx: AppContext):  # noqa: ANN201 - passes the awaited value through
    """Await a coroutine then disconnect the device (but keep the repo open).

    Args:
        coro: The coroutine to run (a tool execution or the menu loop).
        ctx: The application context whose device should be closed afterward.

    Returns:
        Whatever ``coro`` returned — a :class:`~meshterm.tools.base.ToolResult` on the
        scripted path, whose ``exit_code`` the caller turns into the process status.
    """
    try:
        return await coro
    finally:
        if ctx._device is not None:
            try:
                await ctx._device.disconnect()
            except Exception:  # noqa: BLE001 - a dead/lost link must not crash teardown
                pass
            ctx._device = None


async def _execute_and_render(tool: Tool, params: dict, ctx: AppContext) -> ToolResult:
    """Run a tool and print any file it produced.

    A tool's ``message`` is not printed here. It is the menu's closing line — "✓ applied
    3 changes", "✓ traced Alice" — and on the command line it restates what the output
    above it already showed and what ``$?`` already says. Where a scripted run has a fact
    to report that the listing does not carry, the tool prints that fact itself.

    Args:
        tool: The tool to execute.
        params: Parameters for the tool.
        ctx: The application context.

    Returns:
        The tool's :class:`ToolResult`.
    """
    result = await tool.execute(ctx, params)
    # A produced file is a fact, and its path is the whole of it: bare, one per line, so
    # `meshterm config backup out.toml` can be read by the thing that runs it.
    for artifact in result.artifacts:
        ctx.console.print(artifact, highlight=False)
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
