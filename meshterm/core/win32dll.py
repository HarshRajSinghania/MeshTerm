"""Private handles on the Windows DLLs, because ``ctypes.windll`` is shared with everyone.

``ctypes.windll.kernel32`` is a cache. Every caller in the process gets the *same*
``WinDLL`` object, and each function on it is a cached attribute — so
``ctypes.windll.kernel32.GetConsoleScreenBufferInfo`` is one object, shared by this
application and every library inside it. Setting ``.argtypes`` on it is not configuration,
it is a process-wide mutation, and the last writer wins.

That is not theoretical. It crashed MeshTerm on Windows:

* ``ui/tui/emoji_width.py`` measured an emoji by asking the console for its cursor
  position, and declared ``GetConsoleScreenBufferInfo`` as taking a pointer to *its* copy
  of the screen-buffer struct.
* prompt_toolkit later called the very same function object with a pointer to *its* copy
  of the same struct — identical layout, different class.
* ctypes compared the two classes, found them unequal, and refused:
  ``expected LP__CSBI instance instead of pointer to CONSOLE_SCREEN_BUFFER_INFO``.

The application died while building its screen, which on a double-clicked build meant the
traceback and the window it was drawn in disappeared together.

Constructing ``WinDLL`` directly builds a *new* object with its own function cache, so
declarations made on it are visible only to the caller that made them. Each call here
returns a fresh one: two parts of MeshTerm declaring the same function differently is the
same bug in miniature, and the cost is one small object at a call site that runs rarely.

The rule this exists to enforce: **nothing in MeshTerm touches ``ctypes.windll``.**
"""

from __future__ import annotations

import ctypes
import sys

__all__ = ["kernel32", "user32"]


def _library(name: str) -> ctypes.WinDLL:
    """Load ``name`` into a WinDLL of our own.

    Args:
        name: The DLL to load, without its extension.

    Returns:
        A private handle whose function signatures nobody else can see or change.

    Raises:
        RuntimeError: If called anywhere but Windows, where these do not exist.
    """
    if sys.platform != "win32":  # pragma: no cover - the callers all guard on platform
        raise RuntimeError(f"{name} is a Windows library")
    # `use_last_error` keeps GetLastError intact for the caller, which is the only way to
    # tell "the call failed" from "the call succeeded and returned zero".
    return ctypes.WinDLL(name, use_last_error=True)


def kernel32() -> ctypes.WinDLL:
    """A private handle on ``kernel32`` — console, handles, memory."""
    return _library("kernel32")


def user32() -> ctypes.WinDLL:
    """A private handle on ``user32`` — clipboard, keyboard state, windows."""
    return _library("user32")
