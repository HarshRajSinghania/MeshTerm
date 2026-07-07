"""Align Rich's emoji cell-width table with the terminal's *actual* rendering.

Emoji column width is not standardized. Whether a variation-selector-16 sequence
such as ``"☀️"`` (a sun, U+2600 U+FE0F) occupies one cell or two depends on
the terminal emulator and the font it uses. Rich follows Unicode TR51 and counts
such a sequence as **2** columns; many terminals — notably Windows ``conhost`` and
Windows Terminal with common fonts — draw it as **1**. When the two disagree, Rich
pads the line assuming the wrong width and the panel border that follows the text
lands a column off (the classic "the sun pushes the right border left" bug).

There is no static table that is correct on every terminal, and — importantly —
no fully reliable way to *measure* it either. A cursor-position probe only reveals
how the layer that **tracks the cursor** counts the emoji, which on modern split
terminals is a different component than the one that **paints** it:

* **VS Code integrated terminal** — ConPTY advances the cursor by 2 for ``☀️``,
  but the xterm.js renderer paints it in 1 cell. The border you see follows the
  renderer; a probe can only see ConPTY. They disagree, so the probe is wrong here.
* **Windows Terminal** has the same PTY/renderer split.
* A classic **conhost** window or a traditional **POSIX terminal** does its own
  cursor tracking and painting, so there a probe *is* the ground truth.

Because no escape sequence reports the renderer's width back to the program, we
decide the width for :data:`_PROBE` in layers, most trustworthy first:

1. An explicit override via ``MESHTOOLS_EMOJI_VS16=1|2`` — always wins.
2. A fingerprint of renderers known to paint VS16 emoji narrow while their PTY
   counts them wide (VS Code, Windows Terminal) — where a probe would mislead.
3. Otherwise, a live cursor probe (``GetConsoleScreenBufferInfo`` on Windows, a
   ``ESC[6n`` Device Status Report on POSIX), valid on terminals that track and
   paint in the same layer.

If the chosen width is 1, we patch Rich's string-measuring function so every panel,
table, and text widget — menus and chat bubbles alike — lines up. On non-interactive
runs (tests, piped output, scripted CLI) we do nothing and keep Rich's defaults.
"""

from __future__ import annotations

import os
import sys

#: Codepoints that never occupy a cell on their own: zero-width joiner and the
#: emoji-presentation variation selector. Skipping them means a base glyph is
#: measured at its own East-Asian width, with no VS16 "promote to 2" step.
_ZERO_WIDTH = ("‍", "️")

#: The probe: a sun. Its base (U+2600) is East-Asian *narrow* (1), and Rich
#: promotes the U+2600+U+FE0F pair to 2. So a terminal that reports 1 for this is
#: exactly the terminal whose borders Rich currently misaligns.
_PROBE = "☀️"

#: Set once :func:`calibrate` has run so repeated calls are cheap no-ops.
_CALIBRATED = False


def calibrate(*, force_width: int | None = None) -> None:
    """Measure the terminal's emoji width once and patch Rich to match.

    Safe to call more than once (only the first call does work) and safe to call
    when there is no real terminal (it becomes a no-op). Call this after the
    terminal is available but *before* a full-screen renderer takes it over.

    Args:
        force_width: Skip measuring and act as if the terminal rendered the probe
            at this width. For tests; production code omits it.
    """
    global _CALIBRATED
    if _CALIBRATED:
        return
    _CALIBRATED = True

    width = force_width if force_width is not None else _decide_vs16_width()
    # width 1 -> the renderer paints VS16 emoji narrow, so stop Rich promoting.
    # width 2 (or unknown/None) -> Rich's default already matches; leave it alone.
    if width == 1:
        _install_no_vs16_promotion()


def _decide_vs16_width() -> int | None:
    """Decide how wide the renderer paints :data:`_PROBE`, in order of trust.

    Returns:
        ``1`` or ``2`` when we have a confident answer, else ``None`` (leave Rich
        untouched). See the module docstring for why a probe alone is not enough.
    """
    override = os.environ.get("MESHTOOLS_EMOJI_VS16")
    if override in ("1", "2"):
        return int(override)

    # Renderers that paint VS16 emoji in one cell while their PTY counts two, so a
    # cursor probe reads the PTY's (wrong) answer. Fingerprint them directly.
    if os.environ.get("TERM_PROGRAM") == "vscode" or os.environ.get("WT_SESSION"):
        return 1

    # Everywhere else, the terminal tracks and paints in the same layer: measure it.
    return _measure_probe_width()


def _measure_probe_width() -> int | None:
    """Return the column width the live terminal gives :data:`_PROBE`, or ``None``.

    ``None`` means the width could not be measured (not a terminal, the console
    call failed, or the escape reply timed out) — callers treat that as "don't
    touch Rich".
    """
    if not (sys.stdout and sys.stdout.isatty()):
        return None
    try:
        if os.name == "nt":
            return _win_probe_width(_PROBE)
        return _posix_probe_width(_PROBE)
    except Exception:  # noqa: BLE001 - any probe failure must fall back cleanly
        return None


# --- Windows: read the cursor cell column from the console API ----------------


def _win_probe_width(probe: str) -> int | None:
    """Measure ``probe`` width via the Windows console cursor position."""
    import ctypes
    from ctypes import wintypes

    class _COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", wintypes.SHORT),
            ("Top", wintypes.SHORT),
            ("Right", wintypes.SHORT),
            ("Bottom", wintypes.SHORT),
        ]

    class _CSBI(ctypes.Structure):
        _fields_ = [
            ("dwSize", _COORD),
            ("dwCursorPosition", _COORD),
            ("wAttributes", wintypes.WORD),
            ("srWindow", _SMALL_RECT),
            ("dwMaximumWindowSize", _COORD),
        ]

    kernel32 = ctypes.windll.kernel32
    # Declare signatures explicitly. Without this ctypes assumes a 32-bit int
    # return, which truncates the 64-bit console HANDLE and makes every
    # subsequent call fail — silently reducing the probe to "no measurement".
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_CSBI),
    ]
    kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL

    STD_OUTPUT_HANDLE = wintypes.DWORD(-11).value
    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

    def cursor_x() -> int | None:
        info = _CSBI()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None
        return info.dwCursorPosition.X

    sys.stdout.write("\r")
    sys.stdout.flush()
    start = cursor_x()
    if start is None:
        return None
    sys.stdout.write(probe)
    sys.stdout.flush()
    end = cursor_x()
    # Erase the probe so it never flashes into the user's scrollback.
    sys.stdout.write("\r" + " " * (2 if end is None else max(0, end - start)) + "\r")
    sys.stdout.flush()
    if end is None:
        return None
    return end - start


# --- POSIX: ask the terminal with a Device Status Report ----------------------


def _posix_probe_width(probe: str, timeout: float = 0.3) -> int | None:
    """Measure ``probe`` width via a cursor-position report (``ESC[6n``)."""
    import re
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # Column 1, print the probe, then ask where the cursor landed.
        sys.stdout.write("\r" + probe + "\x1b[6n")
        sys.stdout.flush()
        reply = ""
        while "R" not in reply:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                break
            reply += os.read(fd, 32).decode(errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    # Erase the probe from the line.
    sys.stdout.write("\r\x1b[K")
    sys.stdout.flush()

    match = re.search(r"\x1b\[\d+;(\d+)R", reply)
    if not match:
        return None
    # We started at column 1, so the reported column is 1 + width.
    return int(match.group(1)) - 1


# --- the patch ----------------------------------------------------------------


def _install_no_vs16_promotion() -> None:
    """Make Rich measure strings without the VS16 "promote narrow base to 2" step.

    Rich's ``cell_len`` delegates to the module-level ``_cell_len``; replacing that
    (and clearing the memoised wrapper) redirects every downstream caller — panels,
    tables, text — through the terminal-aligned measurement in one shot.
    """
    import rich.cells as cells

    get_size = cells.get_character_cell_size

    def _cell_len_no_promotion(text: str, unicode_version: str = "auto") -> int:
        return sum(
            get_size(char, unicode_version)
            for char in text
            if char not in _ZERO_WIDTH
        )

    cells.cached_cell_len.cache_clear()
    cells._cell_len = _cell_len_no_promotion


__all__ = ["calibrate"]
