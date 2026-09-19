# SPDX-License-Identifier: Apache-2.0
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

from .relaunch import REOPENED_ENV, child_environment, own_command, wanted_size

__all__ = [
    "REOPENED_ENV",
    "available",
    "child_environment",
    "own_command",
    "reopen",
]


def available() -> str | None:
    """The path to ``wt.exe``, or ``None`` when Windows Terminal is not installed.

    Found on ``PATH``, where Windows puts an execution alias for it under
    ``%LOCALAPPDATA%/Microsoft/WindowsApps``.
    """
    if sys.platform != "win32" or os.environ.get(REOPENED_ENV):
        return None
    return shutil.which("wt.exe")


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
            [
                terminal,
                "-w",
                "new",
                "--size",
                _wanted_size(),
                # Otherwise the tab is titled with the executable's full path, which on a
                # downloaded build is a line of Downloads folder. The icon is not ours to
                # set: Windows Terminal takes that from a profile, and a bare command line
                # is not one, so it gets the generic console glyph.
                "--title",
                "MeshTerm",
                "-d",
                os.getcwd(),
                "--",
                *own_command(),
            ],
            env=child_environment(),
            close_fds=True,
        )
        return True
    except OSError:
        return False


def _wanted_size() -> str:
    """The window to ask Windows Terminal for, as ``cols,rows``.

    Asked for, because without ``-w new`` the session lands as a *tab* in whatever window
    happens to be open and inherits its size, and without ``--size`` a new window takes the
    reader's global launch size, which is a setting about their shell and not about this
    app. The dimensions themselves are
    :func:`~meshterm.core.relaunch.wanted_size`'s, shared with every other terminal
    MeshTerm reopens itself in; only the spelling is Windows Terminal's.
    """
    cols, rows = wanted_size()
    return f"{cols},{rows}"
