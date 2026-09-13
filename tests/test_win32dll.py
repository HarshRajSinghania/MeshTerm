# SPDX-License-Identifier: Apache-2.0
"""Tests for the private Windows DLL handles.

These pin a crash that only ever appeared in a frozen build on Windows, which is the
worst place for a bug to live: no traceback survived, because the window it was printed
in was destroyed by the same exit that printed it.

``ctypes.windll.kernel32`` is a cache shared by the whole process. MeshTerm's emoji-width
probe declared ``GetConsoleScreenBufferInfo`` as taking a pointer to its own copy of the
screen-buffer struct; prompt_toolkit then called that same function object with a pointer
to *its* copy — same layout, different class — and ctypes refused the call while the app
was building its screen.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from meshterm.core import win32dll

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows libraries")


def test_each_handle_is_its_own() -> None:
    """Two callers get two objects, so neither can see the other's declarations."""
    assert win32dll.kernel32() is not win32dll.kernel32()
    assert win32dll.user32() is not win32dll.user32()


def test_the_shared_windll_is_the_thing_being_avoided() -> None:
    """The premise: ``ctypes.windll`` hands the same function object to every caller.

    If this ever stops being true the module is unnecessary — but until then it is the
    reason it exists, so it is worth asserting rather than assuming.
    """
    first = ctypes.windll.kernel32.GetConsoleScreenBufferInfo
    second = ctypes.windll.kernel32.GetConsoleScreenBufferInfo
    assert first is second


def test_declaring_a_signature_does_not_reach_the_shared_one() -> None:
    """The regression itself: our declarations must not be visible to anyone else.

    Before this module existed, the assignment below rewrote the signature that
    prompt_toolkit's console output later called through, and the application died on
    startup with ``expected LP__CSBI instance instead of pointer to
    CONSOLE_SCREEN_BUFFER_INFO``.
    """

    class _MineAlone(ctypes.Structure):
        _fields_ = [("whatever", ctypes.c_int)]

    shared = ctypes.windll.kernel32.GetConsoleScreenBufferInfo
    before = shared.argtypes

    private = win32dll.kernel32()
    private.GetConsoleScreenBufferInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_MineAlone),
    ]

    assert ctypes.windll.kernel32.GetConsoleScreenBufferInfo.argtypes == before
    assert private.GetConsoleScreenBufferInfo is not shared


def test_the_probe_leaves_the_shared_signature_alone() -> None:
    """End to end: running the emoji probe must not disturb the process-wide function.

    The narrow assertion above can pass while the real call site still reaches for
    ``ctypes.windll``, so this exercises the code that actually crashed.
    """
    from meshterm.ui.tui import emoji_width

    shared = ctypes.windll.kernel32.GetConsoleScreenBufferInfo
    before = shared.argtypes
    # No console under pytest, so this returns None rather than measuring. That is fine:
    # the declarations happen before the first call either way, which is what matters.
    emoji_width._win_probe_width("x")  # noqa: SLF001 - the point is this private path
    assert ctypes.windll.kernel32.GetConsoleScreenBufferInfo.argtypes == before
