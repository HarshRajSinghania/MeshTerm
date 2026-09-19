# SPDX-License-Identifier: Apache-2.0
"""The macOS Terminal profile: what it says, and what it refuses to touch.

The archive shapes here were not invented. They were read back off a profile installed on
a real Mac and confirmed rendering — so the encoder is pinned to bytes that are known to
work, rather than to a reading of Apple's format.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from meshterm.core import macterminal
from meshterm.core.relaunch import REOPENED_ENV
from meshterm.ui.theme import _VT_SLOTS


def test_color_archive_is_the_shape_appkit_writes() -> None:
    """A colour decodes to a calibrated-RGB ``NSColor``, six places and NUL-terminated."""
    decoded = plistlib.loads(macterminal.ns_color("#aa0000"))

    assert decoded["$archiver"] == "NSKeyedArchiver"
    assert decoded["$version"] == 100000
    colour = decoded["$objects"][1]
    assert colour["NSColorSpace"] == 1
    assert colour["NSRGB"] == b"0.666667 0.000000 0.000000\x00"
    assert decoded["$objects"][2]["$classname"] == "NSColor"


def test_font_archive_names_the_bundled_face() -> None:
    """The font archive carries the PostScript name, not the family name."""
    decoded = plistlib.loads(macterminal.ns_font())

    assert macterminal.FONT_FACE in decoded["$objects"]
    assert decoded["$objects"][1]["NSSize"] == macterminal.FONT_SIZE
    assert decoded["$objects"][3]["$classname"] == "NSFont"


def test_every_palette_slot_comes_from_the_theme() -> None:
    """The window cannot disagree with the screen it is holding.

    Sixteen keys, in the dim-then-bright order Terminal expects, each one the theme's own
    hex. A slot changed in :data:`~meshterm.ui.theme._VT_SLOTS` lands here on the next
    write, which is the reason the profile is generated rather than shipped.
    """
    profile = macterminal.build_profile()

    assert len(macterminal._ANSI_KEYS) == len(_VT_SLOTS) == 16
    for key, (_slot, _label, rgb) in zip(macterminal._ANSI_KEYS, _VT_SLOTS, strict=True):
        assert profile[key] == macterminal.ns_color(rgb), key


def test_the_window_is_opaque_and_bold_is_not_brightness() -> None:
    """The two settings the profile exists to guarantee, beside the font."""
    profile = macterminal.build_profile()

    assert profile["BackgroundColor"] == macterminal.ns_color("#000000")
    assert "BackgroundBlur" not in profile
    assert profile["UseBrightBold"] is False
    # Tidy itself away on a clean exit, stay put on a crash the reader needs to read.
    assert profile["shellExitAction"] == 1


def test_the_profile_is_the_same_bytes_whatever_the_command_line() -> None:
    """The finding that shaped the module, pinned.

    Terminal names an imported settings set after the *file*, reuses it when the bytes
    match, and adds "MeshTerm 1" when they do not. So the profile must not carry anything
    that varies between launches — the launcher path is fixed, and the varying command
    lives in the file it points at.
    """
    import plistlib

    plain = plistlib.dumps(macterminal.build_profile("/Users/x/.meshterm/launch.sh"))
    again = plistlib.dumps(macterminal.build_profile("/Users/x/.meshterm/launch.sh"))

    assert plain == again
    # And what actually goes in is a path, never an argv or an environment.
    profile = macterminal.build_profile("/Users/x/.meshterm/launch.sh")
    assert profile["CommandString"] == "/Users/x/.meshterm/launch.sh"
    assert "--mock" not in str(profile["CommandString"])
    assert "MESHTERM_" not in str(profile["CommandString"])


def test_the_launcher_carries_the_command_and_is_executable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What varies lives here instead, regenerated on every launch."""
    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "argv", ["meshterm", "--mock"])

    path = macterminal.write_launcher()
    body = path.read_text(encoding="utf-8")

    assert path.name == "launch.sh"
    assert body.startswith("#!/bin/sh")
    assert "exec /usr/bin/env " in body
    assert body.rstrip().endswith("--mock")
    if sys.platform != "win32":  # NTFS has no execute bit for chmod to set
        assert path.stat().st_mode & 0o111


def test_a_bare_profile_runs_nothing() -> None:
    """Without a command it is an entry in the list, not a launcher."""
    profile = macterminal.build_profile()

    assert "CommandString" not in profile
    assert "RunCommandAsShell" not in profile


def test_the_profile_round_trips_as_a_terminal_file() -> None:
    """What is written is what Terminal reads: a plist, with the nested archives intact."""
    written = plistlib.dumps(macterminal.build_profile("true"), fmt=plistlib.FMT_XML)
    reloaded = plistlib.loads(written)

    assert reloaded["name"] == macterminal.PROFILE_NAME
    assert reloaded["type"] == "Window Settings"
    assert reloaded["CommandString"] == "true"
    assert plistlib.loads(reloaded["ANSIRedColor"])["$objects"][1]["NSColorSpace"] == 1


def test_launch_command_carries_meshterm_variables_and_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal starts the command from a fresh shell, so what it needs travels with it."""
    monkeypatch.setenv("MESHTERM_HOME", "/Users/someone/meshterm test")
    monkeypatch.delenv(REOPENED_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["meshterm", "--mock"])

    command = macterminal.launch_command()

    assert f"{REOPENED_ENV}=1" in command
    # Quoted, because the reader's path may contain a space and the shell would split it.
    assert "MESHTERM_HOME='/Users/someone/meshterm test'" in command
    assert command.endswith("--mock")
    # A real executable, not a `VAR=value` shell prefix: measured on macOS 26, Terminal
    # reads the string as a bare program name under one setting and as a shell line under
    # the other, and a prefix puts "Command not found: MESHTERM_REOPENED=1" on the screen.
    assert command.startswith("/usr/bin/env ")


def test_an_unrelated_variable_is_not_carried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hop is not a chance to launder the whole environment into a new shell."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")

    assert "AWS_SECRET_ACCESS_KEY" not in macterminal.launch_command()


def test_a_reopened_session_never_reopens_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard that keeps a relaunch from chaining, since the profile's command sets it."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.setenv(REOPENED_ENV, "1")

    assert macterminal.available() is False
    assert macterminal.reopen() is False


@pytest.mark.parametrize("program", ["iTerm.app", "ghostty", "vscode", "WezTerm"])
def test_another_terminal_is_left_alone(program: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is offered to a terminal that already has truecolor and a chosen font."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv(REOPENED_ENV, raising=False)
    monkeypatch.setenv("TERM_PROGRAM", program)

    assert macterminal.in_apple_terminal() is False
    assert macterminal.available() is False


def test_nothing_happens_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every entry point is inert on the platforms this module is not about."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")

    assert macterminal.in_apple_terminal() is False
    assert macterminal.available() is False
    assert macterminal.reopen() is False
    assert macterminal.install_font() is False


def test_the_profile_is_written_under_meshterm_home(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It lives in MeshTerm's own directory, and follows ``MESHTERM_HOME`` when set."""
    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))

    path = macterminal.write_profile()

    assert path == tmp_path / "home" / "MeshTerm.terminal"
    assert plistlib.loads(path.read_bytes())["name"] == "MeshTerm"


def test_the_readers_preferences_are_never_named(tmp_path, monkeypatch) -> None:
    """Terminal discards external edits to its prefs on quit, so nothing here writes them.

    A guard against the obvious future shortcut rather than against today's code: the
    whole design rests on handing Terminal a file and letting it do the importing.
    """
    source = (macterminal.__file__).replace(".pyc", ".py")
    with open(source, encoding="utf-8") as handle:
        body = handle.read().split('"""', 2)[2]

    assert "com.apple.Terminal.plist" not in body
    assert "Library/Preferences" not in body
    assert "defaults" not in body.replace("default_config_dir", "")


def test_the_font_goes_to_the_users_own_library() -> None:
    """No administrator rights, and undoable from Font Book."""
    assert macterminal.USER_FONT_DIR == Path.home() / "Library" / "Fonts"
