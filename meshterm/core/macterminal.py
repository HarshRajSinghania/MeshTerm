# SPDX-License-Identifier: Apache-2.0
"""Reopening the session in a Terminal window that looks like MeshTerm.

macOS Terminal is not the classic Windows console's problem over again. It falls back
generously — ask it for a glyph its font lacks and it finds one somewhere — so nothing here
is ever a box. The trouble is *what* it finds: no Mac monospace face carries the Braille
Patterns block at all (SF Mono, Menlo, Monaco and Courier all measure zero), so the block
the charts and the map's terrain are drawn from is borrowed from a **proportional** face
and arrives at the wrong width. The screen is not empty, it is crooked, which is worse —
nothing announces itself as broken and the reader has nothing to compare it against.

The fix is a font, and a font means a Terminal profile that names it. Which raises the
question this module exists to answer carefully: **whose terminal is it?**

MeshTerm's palette is the 16-colour VT set (:data:`~meshterm.ui.theme._VT_SLOTS`) — a
deliberately crude, deliberately CGA-ish thing that the app wears on purpose. Nobody should
have to live in it to read their mail. So this module never touches the reader's default
profile and never edits their preferences: it *adds* a profile named MeshTerm, and opens
MeshTerm's own window in it. Their Terminal keeps whatever they chose, down to its
translucency; ours is opaque and crude in a window of its own, and quitting leaves nothing
behind.

That also sidesteps a trap worth recording, because it costs an evening to find. Terminal
rewrites ``~/Library/Preferences/com.apple.Terminal.plist`` **from memory when it quits**,
so anything written into that file while Terminal is running is silently discarded later —
and MeshTerm is always running inside it. Nothing here writes that file. A ``.terminal``
file is handed to Terminal and *Terminal* does the importing, which is the one path that
works from inside a live session.

The profile is generated rather than shipped, from the same theme constant the app draws
itself with, so the window can never disagree with the screen it is holding.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .relaunch import REOPENED_ENV, own_command, wanted_size

#: The name the profile is installed under, and the name the reader sees in Terminal's
#: settings list. Stable: renaming it would strand the copy already on their machine.
PROFILE_NAME = "MeshTerm"

#: The bundled face, by its PostScript name — which is what an ``NSFont`` archive holds,
#: and not the family name Font Book shows. Installed by :func:`install_font` from the copy
#: in the package; see :mod:`meshterm.core.consolefont` for the Windows half of the same
#: idea.
FONT_FACE = "CascadiaMonoPL-Regular"

#: Points. Terminal's own default is 11; 13 is a size the braille charts read cleanly at on
#: a Retina panel without the map losing rows.
FONT_SIZE = 13.0

#: Where a user-installed font goes on macOS. No administrator rights, no installer, and
#: the reader can undo it by dragging the file out of Font Book.
USER_FONT_DIR = Path("~/Library/Fonts").expanduser()

#: Terminal's key for each palette slot, in :data:`~meshterm.ui.theme._VT_SLOTS` order —
#: the standard ANSI 16, dim bank then bright bank.
_ANSI_KEYS: tuple[str, ...] = (
    "ANSIBlackColor",
    "ANSIRedColor",
    "ANSIGreenColor",
    "ANSIYellowColor",
    "ANSIBlueColor",
    "ANSIMagentaColor",
    "ANSICyanColor",
    "ANSIWhiteColor",
    "ANSIBrightBlackColor",
    "ANSIBrightRedColor",
    "ANSIBrightGreenColor",
    "ANSIBrightYellowColor",
    "ANSIBrightBlueColor",
    "ANSIBrightMagentaColor",
    "ANSIBrightCyanColor",
    "ANSIBrightWhiteColor",
)


def _archive(objects: list[object]) -> bytes:
    """One ``NSKeyedArchiver`` document, as Terminal stores every colour and font.

    Terminal keeps these as nested binary plists inside the profile — an archived
    ``NSColor`` or ``NSFont`` rather than a number and a string, because the profile is
    written by AppKit rather than for us. The shape is small and fixed, so it is built
    here rather than reached for through a dependency.

    Args:
        objects: The ``$objects`` array, with ``$null`` already at index 0.

    Returns:
        The encoded archive. Binary, because :class:`plistlib.UID` has no XML spelling.
    """
    return plistlib.dumps(
        {
            "$archiver": "NSKeyedArchiver",
            "$objects": objects,
            "$top": {"root": plistlib.UID(1)},
            "$version": 100000,
        },
        fmt=plistlib.FMT_BINARY,
    )


def ns_color(rgb: str) -> bytes:
    """An archived ``NSColor`` for a ``#rrggbb`` string.

    Args:
        rgb: The colour, as the theme spells it.

    Returns:
        The archive Terminal expects in a colour key.
    """
    channels = (int(rgb[index : index + 2], 16) / 255 for index in (1, 3, 5))
    # Calibrated RGB, space-separated to six places and NUL-terminated: AppKit's own
    # spelling, matched exactly because Terminal parses it rather than tolerating it.
    packed = " ".join(f"{value:.6f}" for value in channels).encode("ascii") + b"\x00"
    return _archive(
        [
            "$null",
            {"$class": plistlib.UID(2), "NSColorSpace": 1, "NSRGB": packed},
            {"$classes": ["NSColor", "NSObject"], "$classname": "NSColor"},
        ]
    )


def ns_font(face: str = FONT_FACE, size: float = FONT_SIZE) -> bytes:
    """An archived ``NSFont``.

    Args:
        face: The PostScript name of the face.
        size: The size in points.

    Returns:
        The archive Terminal expects in the ``Font`` key.
    """
    return _archive(
        [
            "$null",
            {
                "$class": plistlib.UID(3),
                "NSName": plistlib.UID(2),
                "NSSize": float(size),
                "NSfFlags": 16,
            },
            face,
            {"$classes": ["NSFont", "NSObject"], "$classname": "NSFont"},
        ]
    )


def launch_command() -> str:
    """The shell line that starts this session again, for the profile to run.

    Terminal starts the command from a **fresh login shell**, which is why this is a
    string rather than an argv and why it carries what it needs explicitly. Nothing of the
    current process's environment survives the hop — good news for the ``_PYI*`` variables
    a frozen build must not pass on (see
    :func:`~meshterm.core.relaunch.child_environment`, which exists for the Windows side
    where they *do* survive), and bad news for a ``MESHTERM_HOME`` the reader set for this
    invocation alone, which the README itself suggests doing. So every ``MESHTERM_*``
    variable currently set is carried across, and the reopened-session marker with them.

    The variables ride on ``/usr/bin/env`` rather than on a ``VAR=value`` prefix, and that
    is not a stylistic choice. Terminal's own handling of the string is not something to
    lean on: measured on macOS 26, the same command line runs under ``RunCommandAsShell``
    *false* and fails under *true*, where Terminal takes the whole thing as the name of a
    program and puts this on the screen::

        Command not found: MESHTERM_REOPENED=1
        Could not create a new process and open a pseudo-tty

    ``env`` is a real executable, so it is a valid argv *and* a valid shell line, and the
    window cannot land on that message however Terminal decides to read it. ``env`` execs
    the program in its own place, so nothing lingers behind it either — the window holds
    MeshTerm and nothing else, and closing MeshTerm does not drop to a prompt.

    Returns:
        A command line Terminal can run with or without a shell.
    """
    carried = {name: value for name, value in os.environ.items() if name.startswith("MESHTERM_")}
    carried[REOPENED_ENV] = "1"
    assignments = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in sorted(carried.items())
    )
    program = " ".join(shlex.quote(part) for part in own_command())
    return f"/usr/bin/env {assignments} {program}"


def build_profile(command: str | None = None) -> dict[str, object]:
    """The MeshTerm profile, as Terminal's preferences hold it.

    Every colour comes from :data:`~meshterm.ui.theme._VT_SLOTS`, so the window and the
    app it is holding cannot disagree — change the theme and this follows on the next
    write. The window size is
    :func:`~meshterm.core.relaunch.wanted_size`'s, the same one Windows Terminal is asked
    for.

    Args:
        command: A shell line for the window to run on open, or ``None`` for a profile
            that just sits in the list waiting to be chosen.

    Returns:
        The profile dictionary, ready to write as a ``.terminal`` file.
    """
    from ..ui.theme import _VT_SLOTS

    cols, rows = wanted_size()
    profile: dict[str, object] = {
        "name": PROFILE_NAME,
        "type": "Window Settings",
        "ProfileCurrentVersion": 2.07,
        "Font": ns_font(),
        "FontAntialias": True,
        "FontWidthSpacing": 1.0,
        # Opaque on purpose. A braille raster over a translucent ground is unreadable, and
        # a translucent profile is a perfectly reasonable thing for the reader to have --
        # which is the whole argument for MeshTerm having a window of its own.
        "BackgroundColor": ns_color("#000000"),
        "TextColor": ns_color("#aaaaaa"),
        "TextBoldColor": ns_color("#ffffff"),
        "SelectionColor": ns_color("#555555"),
        "CursorColor": ns_color("#55ffff"),
        "CursorType": 0,
        "BlinkText": False,
        # Bold is bold, not a jump to the bright bank. The theme states brightness as a
        # colour (see the wordmark's own note); letting Terminal promote it as well would
        # land a bank too high.
        "UseBrightBold": False,
        "columnCount": cols,
        "rowCount": rows,
        "ScrollbackLines": 10000,
        "ShouldLimitScrollback": 0,
        "ShowActiveProcessInTitle": True,
        "ShowDimensionsInTitle": False,
        "ShowWindowSettingsNameInTitle": False,
        # Close the window when MeshTerm exits cleanly, and keep it when it does not --
        # a crash the reader never sees is a bug report nobody can write.
        "shellExitAction": 1,
    }
    for key, (_slot, _label, rgb) in zip(_ANSI_KEYS, _VT_SLOTS, strict=True):
        profile[key] = ns_color(rgb)
    if command is not None:
        profile["CommandString"] = command
        # False, measured. Under *true* Terminal takes the whole string as a program name
        # and the window says "Command not found: ..."; under false it runs. The opposite
        # of what the key's name suggests, which is why it is written down.
        profile["RunCommandAsShell"] = False
    return profile


def launcher_path() -> Path:
    """The little script the profile runs, beside the profile itself."""
    from .config import default_config_dir

    return default_config_dir() / "launch.sh"


def write_launcher() -> Path:
    """Write the launcher, and return where it landed.

    This exists for one reason, and it is the finding that shaped this module. Terminal
    names an imported settings set **after the file** — ``MeshTerm.terminal`` becomes the
    profile "MeshTerm" — and re-importing the *same bytes* reuses that profile, while
    importing **different** bytes under the same filename creates "MeshTerm 1", then
    "MeshTerm 2". Measured, by opening one file four times: three identical imports left
    one profile, and the fourth, one word different, left two.

    So the command cannot live in the profile. It carries ``sys.argv`` and the reader's
    ``MESHTERM_*`` variables, which differ between ``meshterm`` and ``meshterm --mock``,
    and a profile per command line would fill the reader's settings list with junk that
    only they can remove.

    Moving the variable part behind a fixed path fixes it exactly: the profile says
    ``~/.meshterm/launch.sh`` and never changes, and this file underneath it says whatever
    this launch happens to need.

    Returns:
        The path written, executable.
    """
    path = launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        "# Written by MeshTerm each time it reopens itself. Safe to delete.\n"
        f"exec {launch_command()}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def profile_path() -> Path:
    """Where the generated ``.terminal`` file is kept.

    In MeshTerm's own directory rather than the Desktop: it is ours, it is the one place
    that already survives an upgrade, and it moves with ``MESHTERM_HOME``.
    """
    from .config import default_config_dir

    return default_config_dir() / f"{PROFILE_NAME}.terminal"


def write_profile(*, command: str | None = None) -> Path:
    """Write the profile out, and return where it landed.

    Args:
        command: A shell line for the window to run, or ``None`` for a bare profile.

    Returns:
        The path written.
    """
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(build_profile(command), fmt=plistlib.FMT_XML))
    return path


def font_installed() -> bool:
    """Whether the bundled face is already in the reader's font directory."""
    from .consolefont import BUNDLED_FONT

    return (USER_FONT_DIR / BUNDLED_FONT.name).is_file()


def install_font() -> bool:
    """Copy the bundled face into the reader's own font directory.

    The one thing here that writes outside MeshTerm's own folder, which is why it is the
    one thing worth asking about: a single file, in the reader's own Library, no
    administrator rights, no installer, removable by dragging it out of Font Book. It
    changes nothing about how their Terminal looks — it only makes the face available for
    a profile to name.

    Safe to repeat: an existing copy is left alone.

    Returns:
        Whether the font is installed and usable, including when it already was.
    """
    from .consolefont import BUNDLED_FONT

    if sys.platform != "darwin" or not BUNDLED_FONT.is_file():
        return False
    target = USER_FONT_DIR / BUNDLED_FONT.name
    if target.is_file():
        return True
    try:
        USER_FONT_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BUNDLED_FONT, target)
    except OSError:
        return False
    return True


def in_apple_terminal() -> bool:
    """Whether this session is drawing into macOS Terminal.

    iTerm2, Ghostty, kitty, WezTerm and VS Code all set ``TERM_PROGRAM`` to their own
    name, and several of them have both truecolor and a font the reader chose deliberately
    — none of them wants anything this module offers.
    """
    return sys.platform == "darwin" and os.environ.get("TERM_PROGRAM") == "Apple_Terminal"


def available() -> bool:
    """Whether reopening in MeshTerm's own window is a thing worth doing here.

    ``False`` in a session that was itself reopened, which is what keeps a relaunch from
    chaining into another one: the profile's own command carries the marker, because
    Terminal starts it from a fresh shell that inherits nothing from us.
    """
    return in_apple_terminal() and not os.environ.get(REOPENED_ENV)


def reopen() -> bool:
    """Open MeshTerm in a window using its own profile, and report whether it started.

    ``open`` hands the file to Terminal, and **Terminal** imports the profile and opens the
    window — the only route that works from inside a live session, since Terminal discards
    external edits to its preferences when it quits.

    The new window is not a child in any meaningful sense: the caller exits immediately
    afterwards and the two never talk.

    Two files, not one: the launcher carries what varies and the profile points at it, so
    the profile's bytes are the same on every launch and Terminal reuses the one settings
    set instead of adding a numbered copy. :func:`write_launcher` has the measurement.

    Returns:
        Whether ``open`` was launched. ``False`` leaves the caller exactly where it was,
        so a failure here is a detour and never a dead end.
    """
    if not available():
        return False
    try:
        launcher = write_launcher()
        path = write_profile(command=str(launcher))
    except OSError:
        return False
    try:
        subprocess.Popen(  # noqa: S603 - the argv is ours, not the reader's
            ["/usr/bin/open", str(path)],
            close_fds=True,
        )
    except OSError:
        return False
    return True
