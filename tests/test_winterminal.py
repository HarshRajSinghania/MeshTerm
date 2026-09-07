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
