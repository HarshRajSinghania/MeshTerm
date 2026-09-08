"""Tests for reopening the session in Windows Terminal.

Nothing here starts a terminal. What is pinned is the command that *would* be run — which
is the part that can be silently wrong, because a mistake in it produces a window that
opens, fails, and closes faster than anyone can read it.
"""

from __future__ import annotations

import sys

import pytest

from meshterm.core import winterminal


def test_it_is_only_ever_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other platform's terminal already draws the whole app; there is nothing to fix."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert winterminal.available() is None
    assert winterminal.reopen() is False


def test_a_reopened_session_never_offers_to_reopen_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop guard. Windows Terminal would not qualify anyway, and this is the backstop.

    A relaunch that could chain is not a cosmetic bug: each one spawns a window and exits,
    so the reader would watch windows open forever.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv(winterminal.REOPENED_ENV, "1")
    assert winterminal.available() is None
    assert winterminal.reopen() is False


def test_a_frozen_build_relaunches_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer's own executable takes the arguments exactly as they were given."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Downloads\meshterm.exe")
    monkeypatch.setattr(sys, "argv", ["meshterm", "--mock", "--profile", "home"])
    assert winterminal.own_command() == [
        r"C:\Downloads\meshterm.exe",
        "--mock",
        "--profile",
        "home",
    ]


def test_a_source_install_relaunches_through_its_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-m meshterm``, not the console script.

    The script's path is a wrapper whose location varies by install method (venv, pipx,
    user site), while the interpreter running us is a fact we already hold. Using the
    interpreter also guarantees the new window gets the same environment as this one.
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\venv\Scripts\python.exe")
    monkeypatch.setattr(sys, "argv", ["meshterm", "--mock"])
    assert winterminal.own_command() == [r"C:\venv\Scripts\python.exe", "-m", "meshterm", "--mock"]


def test_the_program_name_is_never_passed_on_as_an_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``argv[0]`` is how this process was named, not something to hand to the next one.

    Passing it through would reach MeshTerm as a stray positional argument and be rejected
    by Typer — in a brand new window that then closes.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\meshterm.exe")
    monkeypatch.setattr(sys, "argv", [r"C:\some\other\name.exe"])
    assert winterminal.own_command() == [r"C:\meshterm.exe"]


def test_the_bundles_own_bookkeeping_never_reaches_the_new_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug the frozen build found, and the only one the source build could not.

    A PyInstaller one-file executable unpacks itself and re-runs itself, the two halves
    talking through ``_PYI*`` variables. Passing those on made the reopened MeshTerm think
    it was the second half of a launch it never made; it checked its parent, found Windows
    Terminal, and killed itself with a bootloader error in a window that had just opened.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", r"C:\Downloads\meshterm.exe")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "0")
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI123")
    monkeypatch.setenv("MESHTERM_HOME", r"C:\mine")

    environment = winterminal.child_environment()

    assert not [key for key in environment if key.startswith("_PYI")]
    assert "_MEIPASS2" not in environment
    assert environment["MESHTERM_HOME"] == r"C:\mine"  # the reader's own settings survive
    assert environment[winterminal.REOPENED_ENV] == "1"


def test_a_source_run_keeps_the_environment_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is stripped when there is no bundle, so a dev run is passed through whole.

    ``LD_LIBRARY_PATH`` is the one to watch: unfrozen it is the reader's, and dropping it
    would change how the new process loads its libraries for no reason at all.
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/mine/lib")
    assert winterminal.child_environment()["LD_LIBRARY_PATH"] == "/opt/mine/lib"


def test_the_loader_path_the_bootloader_replaced_is_put_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bootloader points the loader at its own unpacked copy and parks the original.

    The new process unpacks a bundle of its own, so it wants the value the reader started
    with — and where there was none, none.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI999")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/mine/lib")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/tmp/_MEI999")
    monkeypatch.delenv("DYLD_LIBRARY_PATH_ORIG", raising=False)

    environment = winterminal.child_environment()

    assert environment["LD_LIBRARY_PATH"] == "/opt/mine/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in environment
    assert "DYLD_LIBRARY_PATH" not in environment


def test_the_window_asked_for_fits_the_startup_wordmark() -> None:
    """The size request exists because a too-small window fails quietly, not loudly.

    The startup wordmark is 71 columns; below that the splash silently swaps to the narrow
    one drawn for the PicoCalc, which is how this was found — a desktop session showing the
    handheld's mark. The request has to clear that, not merely the platform's minimum.
    """
    from meshterm.platforms import REGULAR, set_platform
    from meshterm.ui.logo import load_logo, logo_width

    set_platform(REGULAR)
    cols, rows = (int(part) for part in winterminal._wanted_size().split(","))

    assert logo_width(load_logo(cols)) == 71, "the window asked for would get the narrow mark"
    assert cols >= REGULAR.readable_cols
    assert rows >= REGULAR.readable_rows


def test_the_session_gets_its_own_window_at_that_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both halves matter, and each was a separate way to arrive somewhere too small.

    Without ``-w new`` the session lands as a tab in whatever window is already open and
    inherits its size — measured at 64 columns on a real machine, which is why the wrong
    mark appeared. Without ``--size`` a new window takes the reader's global launch size,
    a setting about their shell rather than about this app.
    """
    started: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv(winterminal.REOPENED_ENV, raising=False)
    monkeypatch.setattr(winterminal.shutil, "which", lambda _: r"C:\wt.exe")
    monkeypatch.setattr(
        winterminal.subprocess, "Popen", lambda argv, **_: started.append(argv) or None
    )

    assert winterminal.reopen() is True
    argv = started[0]
    assert argv[:3] == [r"C:\wt.exe", "-w", "new"]
    assert "--size" in argv
    assert argv[argv.index("--size") + 1] == winterminal._wanted_size()
    # `--` must still separate wt's options from MeshTerm's, or `--mock` becomes wt's.
    assert "--" in argv and argv.index("--") > argv.index("--size")
