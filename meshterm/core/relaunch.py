# SPDX-License-Identifier: Apache-2.0
"""What every relaunch of MeshTerm needs, whichever terminal it is relaunching into.

MeshTerm reopens itself when the terminal it was started from cannot draw it, and there
are two such terminals for two unrelated reasons. The classic Windows console has no font
fallback at all, so an icon it lacks is a box and no font choice fixes it
(:mod:`meshterm.core.winterminal`). macOS Terminal falls back generously — too generously:
no Mac monospace face carries the Braille Patterns block, so the charts are borrowed from a
*proportional* face and land at the wrong width (:mod:`meshterm.core.macterminal`). One
host shows nothing, the other shows something crooked.

The destinations could hardly be less alike. The act is the same one: work out the command
that would start this session again, hand it an environment a fresh launch can survive, and
let the caller exit. So it is written once, here, and each platform module supplies only
the part that is genuinely its own — which terminal, and how to ask it.
"""

from __future__ import annotations

import os
import sys

#: Set in the reopened session, so a relaunch can never chain into another one. Each
#: platform's own gate would usually refuse a second pass anyway — Windows Terminal is not
#: a classic console; a window opened from MeshTerm's profile is already in it — but this
#: is the belt to those braces, and it costs one environment variable.
REOPENED_ENV = "MESHTERM_REOPENED"


def own_command() -> list[str]:
    """The command that would start this same session again.

    A frozen build is its own executable and takes the arguments as they were given. A
    development or ``pip`` install is reached through its interpreter, because the console
    script's own path is not something the receiving terminal can be relied on to resolve.

    Returns:
        The program and its arguments, ready to hand to a terminal.
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
    it never made, check that its parent is the same program — find the terminal instead —
    and abort::

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


def wanted_size() -> tuple[int, int]:
    """The window to ask for, as ``(cols, rows)``.

    Asked for, because a window MeshTerm opens should be one MeshTerm fits in. A session
    that lands in whatever size the reader's shell happens to launch at can arrive too
    small for the app, and the failure is quiet and cosmetic: the startup wordmark is 71
    columns, so at 70 it silently swaps to the narrow one drawn for the PicoCalc, and a
    short window truncates every description onto the pager.

    The numbers come from the platform's own stated minimum plus a margin, so they follow
    it rather than repeating it. A terminal is free to clamp to what the display can show,
    so this is a request and not a demand — on a small screen the app degrades exactly as
    it would have anyway.

    Returns:
        The columns and rows to ask the terminal for.
    """
    from ..platforms import get_platform

    platform = get_platform()
    return platform.readable_cols + 8, platform.readable_rows + 6
