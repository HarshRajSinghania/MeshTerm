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

1. An explicit override via ``MESHTERM_EMOJI_VS16=1|2`` — always wins.
2. A fingerprint of renderers known to paint VS16 emoji narrow while their PTY
   counts them wide (VS Code, Windows Terminal) — where a probe would mislead.
3. Otherwise, a live cursor probe (``GetConsoleScreenBufferInfo`` on Windows, a
   ``ESC[6n`` Device Status Report on POSIX), valid on terminals that track and
   paint in the same layer.

If the chosen width is 1, we patch Rich's string-measuring function so every panel,
table, and text widget — menus and chat bubbles alike — lines up. On non-interactive
runs (tests, piped output, scripted CLI) we do nothing and keep Rich's defaults.

The same "width 1" verdict also narrows a small, curated set of *lone* emoji — single
codepoints like ``👋`` that carry no variation selector yet are still drawn in one cell
here. Unlike a VS16 sequence there is no structural tell (a trailing ``U+FE0F``) and no
rule separating them from the lone emoji this terminal draws two wide (``📡``, ``💬``):
the split is the font's, not the codepoint's, and a cursor probe can't see it. So the set
is an explicit allowlist (:data:`_DEFAULT_NARROW_LONE`, extended without a code change via
``MESHTERM_NARROW_EMOJI``), and — because it is the panel border *prompt_toolkit* places,
not Rich — narrowing a lone emoji patches *both* width authorities, where the VS16 fix only
needed Rich (prompt_toolkit never promoted a VS16 sequence in the first place).
"""

from __future__ import annotations

import os
import sys
from typing import Callable

#: Codepoints that never occupy a cell on their own: zero-width joiner and the
#: emoji-presentation variation selector. Skipping them means a base glyph is
#: measured at its own East-Asian width, with no VS16 "promote to 2" step.
_ZERO_WIDTH = ("‍", "️")

#: The probe: a sun. Its base (U+2600) is East-Asian *narrow* (1), and Rich
#: promotes the U+2600+U+FE0F pair to 2. So a terminal that reports 1 for this is
#: exactly the terminal whose borders Rich currently misaligns.
_PROBE = "☀️"

#: Lone-codepoint emoji this terminal draws in a single cell even though wcwidth and Rich
#: both call them two — the residual of the VS16 story for glyphs that carry no variation
#: selector (``👋`` is the one confirmed here). There is no rule separating these from the
#: emoji the same terminal draws two wide (``📡``, ``💬``, the menu icons): the split is the
#: font's glyph, not the codepoint, and it cannot be probed (a cursor probe reads the PTY,
#: not the renderer — see the module docstring). So this is a curated allowlist, seeded with
#: the confirmed glyph and extended — no code change — through ``MESHTERM_NARROW_EMOJI``.
_DEFAULT_NARROW_LONE = "👋"

#: Set once :func:`calibrate` has run so repeated calls are cheap no-ops.
_CALIBRATED = False


def _narrow_lone_set() -> frozenset[str]:
    """The lone-codepoint emoji to measure as one cell (see :data:`_DEFAULT_NARROW_LONE`).

    ``MESHTERM_NARROW_EMOJI`` overrides the default outright: set it to the full list of
    glyphs you have confirmed this terminal draws narrow (e.g. ``MESHTERM_NARROW_EMOJI=👋🤙``),
    or to the empty string to turn lone-emoji narrowing off entirely. Only consulted once the
    renderer is judged to draw emoji narrow in the first place (see :func:`calibrate`), so on a
    terminal that draws emoji two cells wide the whole allowlist is inert and nothing regresses.
    """
    return frozenset(os.environ.get("MESHTERM_NARROW_EMOJI", _DEFAULT_NARROW_LONE))


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
    # width 1 -> the renderer draws emoji narrow: stop Rich promoting VS16 sequences and
    # narrow the curated lone-codepoint emoji this terminal also draws in one cell.
    # width 2 (or unknown/None) -> Rich's default already matches; leave everything alone
    # (narrowing here would instead pull a correctly-wide emoji's border a column short).
    if width == 1:
        _install_terminal_widths(_narrow_lone_set())


def _decide_vs16_width() -> int | None:
    """Decide how wide the renderer paints :data:`_PROBE`, in order of trust.

    Returns:
        ``1`` or ``2`` when we have a confident answer, else ``None`` (leave Rich
        untouched). See the module docstring for why a probe alone is not enough.
    """
    override = os.environ.get("MESHTERM_EMOJI_VS16")
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


def _make_cell_len(narrow: frozenset[str]) -> Callable[[str, str], int]:
    """Build Rich's ``_cell_len`` replacement: the terminal's true width for every string.

    Two corrections fold into the one measuring loop the whole render pipeline reads:

    * **VS16 promotion** — a variation selector (:data:`_ZERO_WIDTH`) is skipped, so a narrow
      base (``☀`` = 1) is measured on its own instead of promoted to two by a trailing
      ``U+FE0F``. This is the general emoji-presentation fix (see the module docstring).
    * **Curated lone emoji** — a single codepoint in ``narrow`` (``👋`` and friends) that Rich
      would call two but this terminal draws in one is counted as one, so a chat row carrying
      it pads to a flush right border instead of a column-short notch.

    Everything else keeps its ``get_character_cell_size`` value, so a lone emoji the terminal
    *does* draw two wide (a menu icon like ``📡``, never placed in ``narrow``) is left alone.
    """
    import rich.cells as cells

    get_size = cells.get_character_cell_size

    def _cell_len(text: str, unicode_version: str = "auto") -> int:
        total = 0
        for char in text:
            if char in _ZERO_WIDTH:
                continue
            total += 1 if char in narrow else get_size(char, unicode_version)
        return total

    return _cell_len


def _make_pt_cache(narrow: frozenset[str]) -> "object":
    """Build a prompt_toolkit char-width cache that measures every ``narrow`` glyph as one.

    prompt_toolkit is the authority that *places the panel's right border*: it lays the
    composed frame into its screen buffer stepping a cursor by ``get_cwidth`` (backed by
    ``_CHAR_SIZES_CACHE``), so unless it too measures ``👋`` as one, it positions the border a
    column past where the terminal draws the narrow glyph — the notch survives even with Rich
    corrected. The subclass returns one for a listed lone glyph and defers everything else to
    the stock ``wcwidth`` logic; because the base ``__missing__`` sums per character through
    the cache, a whole chat line ``"Bob 👋 hi"`` inherits the one-cell ``👋`` for free.
    """
    import prompt_toolkit.utils as ptu

    base_cache_cls = type(ptu._CHAR_SIZES_CACHE)

    class _NarrowLoneCache(base_cache_cls):  # type: ignore[valid-type, misc]
        def __missing__(self, string: str) -> int:
            if string in narrow:
                self[string] = 1
                return 1
            return super().__missing__(string)

    return _NarrowLoneCache()


def _install_terminal_widths(narrow: frozenset[str]) -> None:
    """Redirect both width authorities through the terminal-aligned measurement, in one shot.

    Rich's ``cell_len`` delegates to the module-level ``_cell_len``; replacing that (and
    clearing the memoised wrapper) steers every downstream caller — panels, tables, text —
    through :func:`_make_cell_len`. prompt_toolkit is patched *only* when there are lone emoji
    to narrow: a VS16-only correction needs nothing from it (it never promoted the sequence),
    but a lone-emoji correction does, because pt is what places the border (see
    :func:`_make_pt_cache`). Both run at :func:`calibrate` time, before a frame is drawn, so no
    stale two-cell measurement or ``Char`` is ever painted.
    """
    import rich.cells as cells

    cells.cached_cell_len.cache_clear()
    cells._cell_len = _make_cell_len(narrow)

    if narrow:
        import prompt_toolkit.utils as ptu

        ptu._CHAR_SIZES_CACHE = _make_pt_cache(narrow)


__all__ = ["calibrate"]
