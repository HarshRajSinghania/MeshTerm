# SPDX-License-Identifier: Apache-2.0
"""What MeshTerm is running on, read once so a bug report does not have to be interviewed.

Every value here answers a question that otherwise costs a round trip with whoever
reported the problem: which build of MeshTerm, on which OS, in which terminal, how wide,
with which colour depth. None of it is about the mesh and none of it is private — this
module deliberately knows nothing about contacts, keys, positions or messages, so the
block it feeds can be pasted in public without anyone having to read it first.

Two probes are **ladders over environment variables** rather than protocol queries, which
is a concession worth naming: a terminal has no portable way to be asked who it is. The
one sequence that comes close (``CSI > q``) needs a live tty, a reply that may never
arrive, and a timeout budget on a path that must never hang. So each terminal is
identified by the variable it sets about *itself* — which is the same ladder
:func:`~meshterm.ui.termfont.detect_terminal_font` already climbs for the font — and
anything unrecognised falls back to ``TERM`` rather than to a guess.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: The first Windows 11 build. Windows 11 still reports its release as ``"10"`` through
#: every documented API, so the build number is the only thing that separates the two.
_WINDOWS_11_BUILD = 22000

#: Terminals that announce themselves, by the variable each one sets. Ordered most to
#: least specific: an emulator hosting another (VS Code's terminal, a multiplexer) sets
#: its own marker *and* inherits the outer one, so the inner identity has to win.
#:
#: ``TERM_PROGRAM`` is checked separately, between these and the ``TERM`` fallback,
#: because its *value* is the name — it identifies terminals this table never has to list.
_TERMINAL_VARS: tuple[tuple[str, str], ...] = (
    ("WT_SESSION", "Windows Terminal"),
    ("ConEmuPID", "ConEmu"),
    ("TERMINAL_EMULATOR", ""),  # JetBrains IDEs put the name in the value
    ("KITTY_WINDOW_ID", "kitty"),
    ("ALACRITTY_WINDOW_ID", "Alacritty"),
    ("KONSOLE_VERSION", "Konsole"),
    ("GNOME_TERMINAL_SCREEN", "GNOME Terminal"),
    ("VTE_VERSION", "VTE-based"),
)

#: What ``TERM_PROGRAM`` calls terminals whose own name is not what a person calls them.
#: Only the ones that genuinely differ; every other value passes through as it is, which
#: is what keeps this from growing into a catalogue of every terminal there is.
_TERM_PROGRAM_NAMES: dict[str, str] = {
    "Apple_Terminal": "Terminal.app",
    "vscode": "VS Code",
}


@dataclass(frozen=True, slots=True)
class Host:
    """The machine and the Python running MeshTerm.

    Attributes:
        install: How this copy was installed — ``"frozen"`` (a one-file build, which has
            its own bootloader, its own relaunch rules and no source tree to read),
            ``"source"`` (a checkout, so a commit can be asked for), or ``"package"``
            (a wheel from an index). The three fail in different ways and the reporter
            usually does not know which one they have.
        os: The flavour and its version as a person writes it — ``Windows 11``,
            ``macOS 15.6``, ``Debian GNU/Linux 12 (bookworm)``.
        os_build: The exact build under that name: a Windows build number, or the kernel
            release everywhere else. What separates two hosts that call themselves the
            same thing.
        arch: The processor architecture, which is what tells an arm64 build from an
            x86_64 one when a binary misbehaves.
        python: The interpreter version, three parts.
    """

    install: str
    os: str
    os_build: str
    arch: str
    python: str


@dataclass(frozen=True, slots=True)
class Terminal:
    """The terminal MeshTerm is drawing into, and what it can draw.

    Attributes:
        program: The emulator's name, or ``TERM``'s value where nothing identified
            itself, or ``None`` when even that is unset.
        term: The ``TERM`` value, verbatim. Kept beside ``program`` rather than folded
            into it: a terminal lying about ``TERM`` is itself a common cause of a
            rendering report.
        colorterm: The ``COLORTERM`` value, which is how a terminal claims truecolor and
            the first thing to check when a screen arrives in the wrong colours.
        cols: The terminal's width in cells.
        rows: The terminal's height in cells.
        over_ssh: Whether this is an ssh session. It changes what is knowable — the font
            lives on the far client — and it is why several other verdicts here read
            ``unknown`` rather than wrong.
    """

    program: str | None
    term: str | None
    colorterm: str | None
    cols: int
    rows: int
    over_ssh: bool


def install_kind() -> str:
    """How this copy of MeshTerm got here: ``frozen``, ``source`` or ``package``.

    A frozen build is unambiguous — the bootloader sets the flag. Past that, the question
    is whether there is a repository above the package: a checkout can be asked for its
    commit and can carry uncommitted work, and a wheel can do neither.

    Returns:
        One of ``"frozen"``, ``"source"`` or ``"package"``.
    """
    if getattr(sys, "frozen", False):
        return "frozen"
    # meshterm/core/hostinfo.py -> meshterm/core -> meshterm -> the tree that holds it
    if (Path(__file__).resolve().parents[2] / ".git").exists():
        return "source"
    return "package"


def host() -> Host:
    """Read the machine's own facts.

    Returns:
        The :class:`Host` block.
    """
    name, build = _os_name_and_build()
    return Host(
        install=install_kind(),
        os=name,
        os_build=build,
        arch=platform.machine() or "unknown",
        python=platform.python_version(),
    )


def terminal(environ: Mapping[str, str] | None = None) -> Terminal:
    """Read the terminal's own facts.

    Args:
        environ: The environment to inspect (defaults to ``os.environ``), injectable so
            the ladder can be tested without a terminal of the right kind to hand.

    Returns:
        The :class:`Terminal` block.
    """
    env = os.environ if environ is None else environ
    size = shutil.get_terminal_size()
    return Terminal(
        program=terminal_program(env),
        term=env.get("TERM") or None,
        colorterm=env.get("COLORTERM") or None,
        cols=size.columns,
        rows=size.lines,
        over_ssh=bool(env.get("SSH_TTY") or env.get("SSH_CONNECTION")),
    )


def terminal_program(environ: Mapping[str, str] | None = None) -> str | None:
    """Name the terminal emulator from what it says about itself.

    The ladder: the terminals in :data:`_TERMINAL_VARS` that set a marker variable, then
    ``TERM_PROGRAM`` (whose *value* is the name, so it covers terminals this module has
    never heard of), then ``TERM`` as the last honest thing there is to say.

    Args:
        environ: The environment to inspect (defaults to ``os.environ``).

    Returns:
        The terminal's name, or ``None`` when nothing in the environment names one.
    """
    env = os.environ if environ is None else environ
    for var, name in _TERMINAL_VARS:
        value = env.get(var)
        if value:
            return name or value
    program = env.get("TERM_PROGRAM")
    if program:
        return _TERM_PROGRAM_NAMES.get(program, program)
    return env.get("TERM") or None


def _os_name_and_build() -> tuple[str, str]:
    """The OS as a person writes it, and the exact build under that name."""
    system = platform.system()
    if system == "Windows":
        return _windows()
    if system == "Darwin":
        version = platform.mac_ver()[0]
        return (f"macOS {version}" if version else "macOS"), platform.release()
    if system == "Linux":
        return (_pretty_name() or "Linux"), platform.release()
    return (system or "unknown"), platform.release()


def _windows() -> tuple[str, str]:
    """Windows by the name it is sold under, and its build number.

    ``platform.win32_ver`` reports release ``"10"`` on Windows 11 as well — the rename
    never reached the version APIs — so the build number is what decides which one this
    is. The build is also the useful half of the pair: it is what a fix is shipped
    against.
    """
    release, version, _service_pack, _kind = platform.win32_ver()
    try:
        build = int(version.rsplit(".", 1)[-1])
    except ValueError:
        build = 0
    name = "Windows 11" if build >= _WINDOWS_11_BUILD else f"Windows {release or '?'}"
    return name, version or platform.version()


def _pretty_name() -> str | None:
    """The distribution's own name for itself, from ``/etc/os-release``.

    The one place a Linux flavour is stated by the system rather than inferred, and the
    reason a report can say *Calculinux* or *Raspberry Pi OS* instead of ``Linux``.
    A host without the file (or without permission to read it) simply has no answer.
    """
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "PRETTY_NAME":
            return value.strip().strip('"') or None
    return None
