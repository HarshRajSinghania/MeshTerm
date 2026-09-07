"""Reopening the session in Windows Terminal, which is where MeshTerm looks like itself.

MeshTerm is drawn with emoji icons, braille charts and powerline path chips. Whether any
of that reaches the screen is decided by the *terminal*, not by MeshTerm and not really by
the font either: a modern terminal, asked for a character its font lacks, quietly borrows
the glyph from another font on the machine. That is why the app looks right in Windows
Terminal, in VS Code's terminal, and on macOS and Linux — and why it looks right there
even though the font the reader chose usually holds almost none of it. Measured on a
development machine, the everyday choices — Hack Nerd Font, JetBrains Mono, Fira Code,
Source Code Pro — carry no braille at all, and no monospace font anywhere carries emoji.

The classic Windows console does not do that. It draws what its one font holds and boxes
for the rest, so on that host the emoji cannot be shown *at all* — the only two fonts on a
Windows machine with emoji glyphs, Segoe UI Emoji and Segoe UI Symbol, are proportional,
and a console will not take a proportional font. No font choice fixes it, which leaves
exactly one thing that does: a different terminal.

So when MeshTerm finds itself in that console and Windows Terminal is installed — it is on
every Windows 11 machine, and a free install on 10 — it offers to reopen itself there.
One keypress, and the app looks the way it was drawn instead of the way conhost can
manage. Declining falls back to :mod:`meshterm.core.consolefont`, which makes the best of
the console the reader chose to stay in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

#: Set in the reopened session, so a relaunch can never chain into another one. Windows
#: Terminal is not a classic console and so would not qualify anyway — this is the belt to
#: that braces, and it costs one environment variable.
REOPENED_ENV = "MESHTERM_REOPENED"


def available() -> str | None:
    """The path to ``wt.exe``, or ``None`` when Windows Terminal is not installed.

    Found on ``PATH``, where Windows puts an execution alias for it under
    ``%LOCALAPPDATA%/Microsoft/WindowsApps``.
    """
    if sys.platform != "win32" or os.environ.get(REOPENED_ENV):
        return None
    return shutil.which("wt.exe")


def own_command() -> list[str]:
    """The command that would start this same session again.

    A frozen build is its own executable and takes the arguments as they were given. A
    development or ``pip`` install is reached through its interpreter, because the console
    script's own path is not something ``wt`` can be relied on to resolve.

    Returns:
        The program and its arguments, ready to hand to Windows Terminal.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, "-m", "meshterm", *sys.argv[1:]]


def child_environment() -> dict[str, str]:
    """The environment for a fresh launch of MeshTerm, minus this bundle's bookkeeping.

    A PyInstaller one-file build runs in two stages: the executable unpacks itself into a
    temporary directory and then runs itself again as a child, the two halves coordinating
    through ``_PYI*`` environment variables. Those must not reach a *new* launch of the
    executable. The bootloader would find them, conclude it is the second stage of a launch
    it never made, check that its parent is the same program — find Windows Terminal
    instead — and abort::

        [PYI-33368:ERROR] Security validation failure: parent process has different
        executable!

    Which is the whole session gone, in a brand new window holding a message nobody can act
    on, while the window that offered the move has already closed. Removing them makes the
    new process a first stage, which is what it actually is.

    Returns:
        The environment to start the new process with.
    """
    environment = dict(os.environ)
    environment[REOPENED_ENV] = "1"
    if not getattr(sys, "frozen", False):
        return environment

    for key in [key for key in environment if key.startswith("_PYI")]:
        del environment[key]
    environment.pop("_MEIPASS2", None)  # the name older bootloaders used
    # The bootloader points the loader at the unpacked bundle and parks the caller's own
    # value beside it. The new process unpacks its own copy, so it wants the original.
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        original = environment.pop(f"{name}_ORIG", None)
        if original is not None:
            environment[name] = original
        else:
            environment.pop(name, None)
    return environment


def reopen() -> bool:
    """Start this session again in Windows Terminal, in the same directory.

    The new window is not a child in any meaningful sense — the caller exits immediately
    afterwards and the two never talk — so nothing is waited on and no pipes are held.

    Returns:
        Whether Windows Terminal was started. ``False`` leaves the caller exactly where it
        was, which is why the offer is only ever a detour and never a dead end.
    """
    terminal = available()
    if terminal is None:
        return False
    try:
        # `--` ends wt's own option parsing, so MeshTerm's flags reach MeshTerm rather
        # than being read as Windows Terminal's (verified: `--mock` and `--profile x`
        # arrive intact). `-d` keeps the working directory, which relative paths given
        # on the command line depend on.
        subprocess.Popen(  # noqa: S603 - the argv is ours, not the reader's
            [terminal, "-d", os.getcwd(), "--", *own_command()],
            env=child_environment(),
            close_fds=True,
        )
        return True
    except OSError:
        return False
