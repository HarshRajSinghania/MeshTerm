"""The persistent full-screen session: the heart of the TUI library.

A :class:`TuiSession` owns one prompt_toolkit :class:`Application`, a stack of
:class:`~meshterm.ui.tui.screen.Screen` layers, and the persistent header/footer frame. It
translates prompt_toolkit key events into normalized *actions* dispatched to the top screen,
composes the current view (bounded to the terminal, reflowing on resize) via
:mod:`~meshterm.ui.tui.frame`, and exposes ``async`` helpers (``select``/``text``/
``confirm``/``autocomplete``/``scroll``/``progress``) that push a screen, await its result,
and pop it — the push/await/pop model behind every prompt.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional, Sequence

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.utils import get_cwidth
from rich.console import RenderableType
from rich.text import Text

from ...platforms import get_platform
from ...services import modifier_watch
from . import fastrender, fkeys, frame
from .overlay import BusyOverlay
from .progress import TuiProgress
from .prompt import (
    AutocompleteScreen,
    ButtonDialog,
    ConfirmScreen,
    PinDialog,
    TextScreen,
    TypedConfirmDialog,
    Validator,
)
from .screen import CANCEL, BusyScreen, Screen, ScrollScreen
from .select import Choice, ReorderScreen, SelectScreen, Separator

#: Every Ctrl-letter chord the app binds, keyed by the bare lowercase letter — the single
#: source of truth for both halves of a chord's life: the ``Keys.Control*`` bindings folded
#: into :data:`_KEY_ACTIONS` below, and the right-Ctrl rescue in :meth:`TuiSession._dispatch`
#: (see :func:`_right_ctrl_down`), which promotes the bare letter a layout-claimed right Ctrl
#: delivers as *text* back into the chord. Add a chord here and both Ctrl keys reach it — no
#: second edit, no chord that works on one side of the keyboard only. ``quit`` and
#: ``paste_clipboard`` are the session's own (answered in ``_dispatch``); the rest are actions
#: forwarded to the top screen. A bare letter only reaches text while the right Ctrl is held
#: when the layout has no third-level glyph for that key (i.e. it really is the chord) — a
#: genuine third-level character arrives as some *other* glyph and never matches this table.
#: ``m``/``i``/``h`` are not free: prompt_toolkit spells Enter, Tab and Backspace as
#: ``Keys.ControlM``/``ControlI``/``ControlH``, so claiming them here would rebind those keys.
#: A chord's letter is the mnemonic of the *action*, not of one screen's word for it —
#: ``locate`` is ^U for **you** (JP, 2026-08-09), the same key on the map and in the mesh
#: walk, because what it names on both is our own node.
_CTRL_LETTER_CHORDS: dict[str, str] = {
    "c": "quit",
    "p": "paths",
    "r": "retry",
    "u": "locate",
    "v": "paste_clipboard",
}

#: Maps prompt_toolkit keys to the normalized action names screens understand.
_KEY_ACTIONS: dict[Any, str] = {
    Keys.Up: "up",
    Keys.Down: "down",
    Keys.Left: "left",
    Keys.Right: "right",
    Keys.ShiftUp: "shift_up",
    Keys.ShiftDown: "shift_down",
    Keys.ShiftLeft: "shift_left",
    Keys.ShiftRight: "shift_right",
    Keys.PageUp: "pageup",
    Keys.PageDown: "pagedown",
    Keys.Home: "home",
    Keys.End: "end",
    Keys.ControlHome: "ctrl_home",
    Keys.ControlEnd: "ctrl_end",
    Keys.ControlPageUp: "ctrl_pageup",
    Keys.ControlPageDown: "ctrl_pagedown",
    Keys.ControlLeft: "ctrl_left",
    Keys.ControlRight: "ctrl_right",
    Keys.ControlUp: "ctrl_up",
    Keys.ControlDown: "ctrl_down",
    Keys.Enter: "enter",
    # Ctrl+Enter, as far as a terminal can spell it. There is no such keycode: Enter *is*
    # ^M, so a console that doesn't do modifyOtherKeys has nothing left to modify and
    # sends plain CR. What most do send is LF — prompt_toolkit's ``ControlJ`` — which is
    # otherwise unbound here (an unprintable char, dropped by ``_typed``), so claiming it
    # costs nothing and wins on the terminals that offer it. The right-Ctrl rescue below
    # covers the rest through _CTRL_CHORDS, and on a console that spells neither, the
    # F-lane chip is the affordance (which is the platform where that is already true).
    Keys.ControlJ: "ctrl_enter",
    Keys.Escape: "escape",
    Keys.Backspace: "backspace",
    Keys.Delete: "delete",
    Keys.Tab: "tab",
    Keys.BackTab: "shift_tab",
    # The function keys, for the PicoCalc's F-key lane (F6–F10 are its Shift bank —
    # the MCU translates Shift+F1..F5 into these plain keycodes). The session resolves
    # them against the top screen's lane in _dispatch; on platforms without the lane
    # they resolve to nothing and fall away. Alt+Fn is the kernel's VT switch on the
    # device and must never be bound.
    **{getattr(Keys, f"F{n}"): f"f{n}" for n in range(1, 11)},
    # The Ctrl-letter chords, generated from the one table above so a chord can never be
    # bound without its right-Ctrl rescue (or rescued into an action nothing binds).
    **{
        getattr(Keys, f"Control{letter.upper()}"): action
        for letter, action in _CTRL_LETTER_CHORDS.items()
    },
}

#: The plain actions that have a Ctrl-chord sibling, for the right-Ctrl rescue in
#: :meth:`TuiSession._dispatch` (see :func:`_right_ctrl_down`). Navigation keys, plus
#: ``enter`` — the one key a terminal cannot reliably spell chorded (see ``Keys.ControlJ``
#: above), so the rescue is not a fallback there but the surer of the two paths.
_CTRL_CHORDS: dict[str, str] = {
    "up": "ctrl_up",
    "down": "ctrl_down",
    "left": "ctrl_left",
    "right": "ctrl_right",
    "home": "ctrl_home",
    "end": "ctrl_end",
    "pageup": "ctrl_pageup",
    "pagedown": "ctrl_pagedown",
    "enter": "ctrl_enter",
}


def _right_ctrl_down() -> bool:
    """Whether the right Ctrl key is physically held right now (Windows; ``False`` elsewhere).

    The rescue behind right-Ctrl chords. Keyboard layouts that claim the right Ctrl key as a
    character-group modifier — the Canadian Multilingual Standard uses it to reach a third
    character level — can deliver a right-Ctrl'd arrow to the console as a *bare* arrow, no
    ctrl flag left for prompt_toolkit to map, so only the left Ctrl ever steered a sortable
    list. This probes the physical key state (``GetAsyncKeyState``) instead of trusting the
    stripped event modifiers: if an arrow reached our dispatch, our terminal had focus, so a
    right Ctrl held at that instant is the user chording it. Non-Windows platforms report
    ``False`` — a VT terminal encodes the ctrl modifier side-agnostically itself, and there
    is no per-side key state to consult anyway.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(0xA3) & 0x8000)  # VK_RCONTROL
    except Exception:  # noqa: BLE001 - a failed probe just leaves the plain action
        return False


def _read_clipboard() -> str:
    """Best-effort read of the OS clipboard's Unicode text (Windows; ``""`` elsewhere).

    The fallback behind Ctrl-V on a terminal that delivers the key literally rather than as a
    bracketed paste (see :meth:`TuiSession._dispatch`): it pulls the clipboard's text
    straight from the Win32 API. Every failure — a non-Windows platform, an empty or
    non-text clipboard, a clipboard busy elsewhere we couldn't open — collapses to ``""``, so
    the paste simply does nothing rather than raising into the key handler. Pointer-returning
    calls declare a ``c_void_p`` result so a 64-bit handle isn't truncated to an int.
    """
    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - a clipboard hiccup must never break a keypress
        return ""


def _reclaim_last_column() -> bool:
    """Whether to reclaim the terminal's final column.

    Some terminals (and prompt_toolkit's size probe on them) report the window one column
    narrower than it really is, so the frame is drawn to ``columns - 1`` and the true last
    column sits unused — visibly selectable to the right of the border. When this is on,
    :class:`_WidthExtendedOutput` tells both the renderer and the frame compositor the
    window is one column wider, and that final column gets drawn.

    This is a gate, not a certainty: it is *correct* only when the probe under-reports. On a
    terminal whose width probe is already right, the extra column falls off the real screen and
    the frame would wrap and tear — which is exactly PicoCalc's exact-width console, so
    :data:`~meshterm.platforms.PICOCALC` defaults this off. ``MESHTERM_FULL_WIDTH=0``/``=1``
    remains an explicit override on top of the platform default for any terminal that needs
    to disagree with its platform's usual verdict (TODO: thread through ``settings`` and the
    config editor once confirmed further).

    Read fresh on every call (never cached at import time) so it reflects whichever platform
    :func:`~meshterm.platforms.set_platform` installed for this process — see that module's
    docstring for why a cached/imported copy of the platform would go stale.
    """
    override = os.environ.get("MESHTERM_FULL_WIDTH")
    if override is not None:
        return override != "0"
    return get_platform().width_reclaim

#: How many stacked dialog layers the layout can float over the background at once. A fixed
#: pool of centered-box floats (see :meth:`TuiSession._build_app`), sized well past the deepest
#: real nesting — a tool's list, an item's detail popup, and a confirm over that is only three.
_MAX_DIALOG_LAYERS = 8

def _changed_rows(before: str, after: str) -> Optional[list[int]]:
    """Which lines of a full-screen frame differ, or ``None`` when they can't be compared.

    Both frames are composed one line per terminal row (see
    :func:`~meshterm.ui.tui.frame.compose_base`), so a line index *is* a row index — the
    mapping :meth:`TuiSession._scrub_rows` needs. A differing line count means the frame's
    height moved (a resize, a splash giving way to the framed layout) and no such mapping
    holds; the caller falls back to repainting everything.
    """
    old, new = before.split("\n"), after.split("\n")
    if len(old) != len(new):
        return None
    return [i for i, (a, b) in enumerate(zip(old, new)) if a != b]


def _has_wide_glyph(text: str) -> bool:
    """Whether ``text`` holds a glyph prompt_toolkit reserves two cells for.

    A width-2 glyph is where the terminal and the renderer can disagree: an emoji (``👋``)
    that the terminal draws in a *single* cell is the common case here — pt reserves two,
    the terminal advances one, and from that point the row's cursor model is off. Node-type
    marks (``▲●■``) and chart braille are width-1 everywhere, so they never trip this. The
    ``>= 0x1100`` guard skips the ASCII/Latin bulk of a frame before the width lookup, which
    matters because this runs over the whole composed frame on every repaint.

    On a platform that draws no emoji the answer is ``False`` by construction — its console
    font is a fixed 512-glyph set with no wide glyph in it, so nothing a frame can contain
    would return ``True``. Answering from the platform instead of the text skips a
    per-character scan of the entire frame on every repaint, and with it the scrub machinery
    that a ``True`` would arm, leaving the cheap differential paint permanently in play.
    """
    if not get_platform().emoji:
        return False
    return any(ord(ch) >= 0x1100 and get_cwidth(ch) == 2 for ch in text)


class _WidthExtendedOutput:
    """A prompt_toolkit ``Output`` proxy that reports one extra terminal column.

    Wraps the real output and forwards everything untouched *except* :meth:`get_size`, which
    adds a column. Because the whole render pipeline — prompt_toolkit's differential renderer
    and MeshTerm's own frame compositor (via :meth:`TuiSession._size`) — keys off
    ``output.get_size()``, this single override makes both use the reclaimed column in
    lock-step. See :func:`_reclaim_last_column` for when this is right (and when it isn't).
    """

    def __init__(self, inner: Any) -> None:
        """Wrap ``inner`` (the concrete prompt_toolkit output for the real terminal)."""
        self._inner = inner

    def get_size(self) -> Size:
        """The wrapped size with one column added, so the last column is claimed."""
        size = self._inner.get_size()
        return Size(rows=size.rows, columns=size.columns + 1)

    def __getattr__(self, name: str) -> Any:
        """Forward every other attribute/method straight to the wrapped output."""
        return getattr(self._inner, name)


def _message_border(message: "Text | str") -> str:
    """Pick a message dialog's border style from the strongest tone in the text.

    Outcome notes carry their severity as theme spans (``[err]``, ``[warn]``, ``[ok]``),
    so the dialog frame can echo it: an error message gets the ``err`` border, a warning
    (e.g. "device rebooting") the cautionary one, and anything else — successes included —
    the standard accent. A plain string carries no spans and always reads as neutral.

    Args:
        message: The dialog's message, styled or plain.

    Returns:
        The Rich style name for the dialog border.
    """
    if isinstance(message, Text):
        styles = {str(span.style) for span in message.spans}
        if "err" in styles:
            return "err"
        if "warn" in styles:
            return "warn"
    return "accent"


class TuiSession:
    """A running full-screen TUI: screen stack, frame, input loop, and async prompts."""

    def __init__(
        self,
        header: Optional[Callable[[int], RenderableType]] = None,
        *,
        input: Any = None,  # noqa: A002 - matches prompt_toolkit's Application(input=) name
        output: Any = None,
    ) -> None:
        """Create a session.

        Args:
            header: Callable taking the terminal width in columns and returning the
                persistent header renderable (banner + live status), re-invoked on
                every repaint — the width lets it stretch trailing content (the
                activity sparkline) to fill the row exactly. Defaults to a plain title.
            input: Optional prompt_toolkit input to drive the app from (tests use a pipe);
                defaults to the real terminal.
            output: Optional prompt_toolkit output to render to (tests use a dummy);
                defaults to the real terminal.
        """
        self._stack: list[Screen] = []
        self._header = header or (lambda cols: Text("MeshTerm", style="brand"))
        self._app: Optional[Application] = None
        self._input = input
        self._output = output
        # The top-most floating "working" overlay (a skeleton card), or None when idle. It is
        # deliberately *not* on the screen stack: it hovers above every layer and is shown/
        # hidden by busy_overlay, independent of whatever screens are pushed.
        self._overlay: Optional[BusyOverlay] = None
        # What each drawn layer last composed — ``layer -> (text, carries a wide glyph)`` — so
        # :meth:`_emit` can tell a frame that actually changed from one the 1 Hz refresh just
        # re-rendered identically, and can ask whether anything currently on screen needs the
        # full-repaint treatment. Reconciled against the layers actually drawn on every paint
        # (see :meth:`_reconcile_layers`).
        self._layers: dict[str, tuple[str, bool]] = {}

    # --- stack ---------------------------------------------------------------

    @property
    def top(self) -> Optional[Screen]:
        """The active (top-most) screen, or ``None`` when the stack is empty."""
        return self._stack[-1] if self._stack else None

    def push(self, screen: Screen) -> None:
        """Push a screen onto the stack and repaint."""
        self._stack.append(screen)
        self.invalidate()

    def pop(self, screen: Optional[Screen] = None) -> None:
        """Pop ``screen`` (or the top) off the stack and repaint."""
        if not self._stack:
            return
        popped: Optional[Screen] = None
        if screen is None or self._stack[-1] is screen:
            popped = self._stack.pop()
        elif screen in self._stack:
            self._stack.remove(screen)
            popped = screen
        self._expose_overlay()
        # A floating dialog is drawn as a content-sized box over the screen beneath. If any
        # of its cells held a glyph the terminal painted wider than prompt_toolkit tracks
        # (an emoji or box-draw fallback the differential renderer can't see), that overhang
        # would otherwise survive as stray characters in the dialog's wake. Force the frame
        # beneath to rewrite every cell as the dialog is torn down so nothing of it lingers.
        if popped is not None and getattr(popped, "floating", False):
            self._invalidate_last_frame()
        self.invalidate()

    def reset(self) -> None:
        """Clear the whole screen stack and repaint.

        Used to unwind to a blank frame after a cancelled activity — e.g. when a mid-session
        device disconnect abandons whatever screens the interrupted work had pushed, before
        the reconnect dialog is shown over a clean slate. Screens hold no external resources
        (their callers pop them in ``finally``), so dropping any stragglers here is safe.
        """
        self._stack.clear()
        self._expose_overlay()
        self.invalidate()

    def _expose_overlay(self) -> None:
        """Restart the busy overlay's fade whenever a stack change re-exposes it.

        A prompt pushed over an active overlay hides the card; popping back to an empty stack
        re-exposes it. Restart the intro then so the black hold and fade-in replay fresh each
        time the card is shown, rather than snapping back at full brightness (see
        :meth:`~meshterm.ui.tui.overlay.BusyOverlay.restart`).
        """
        if self._overlay is not None and not self._stack:
            self._overlay.restart()

    def invalidate(self) -> None:
        """Request a repaint if the application is running."""
        if self._app is not None:
            self._app.invalidate()

    def request_full_repaint(self) -> None:
        """Force the next paint to rewrite every cell, then schedule it.

        prompt_toolkit repaints differentially: cells equal to the previous frame are left
        untouched. That is normally what we want, but it also means terminal-side corruption
        (a double-width fallback glyph the diff can't see) survives until those exact cells
        change. Callers use this when leaving a screen that could have smeared the terminal —
        the map — so the screen drawn underneath starts from a clean slate.
        """
        self._invalidate_last_frame()
        self.invalidate()

    def _invalidate_last_frame(self) -> None:
        """Drop prompt_toolkit's cached last frame so the next paint rewrites every cell.

        Touches a prompt_toolkit internal, so it fails soft if the attribute ever moves.
        """
        renderer = getattr(self._app, "renderer", None)
        if renderer is not None and hasattr(renderer, "_last_screen"):
            renderer._last_screen = None

    def _scrub_columns(self, start: int, stop: int) -> None:
        """Force prompt_toolkit to repaint terminal columns ``[start, stop)`` on the next diff.

        Overwrites those cells in pt's remembered last frame with a sentinel that can't equal
        any real content, so the differential renderer treats them as changed and redraws
        them — scrubbing a double-width fallback glyph that smeared over a *static* edge (the
        map's panel border, a floating dialog's), without the whole-frame flicker of dropping
        the entire cached frame. Touches a pt internal, so it fails soft if the structure ever
        moves.
        """
        renderer = getattr(self._app, "renderer", None)
        last = getattr(renderer, "_last_screen", None)
        if last is None:
            return
        try:
            from prompt_toolkit.layout.screen import Char

            buffer = last.data_buffer
            sentinel = Char("￿")  # a non-character; never equals real cell content
            for x in range(max(0, start), stop):
                for row in list(buffer.keys()):
                    buffer[row][x] = sentinel
        except Exception:  # noqa: BLE001 - a cosmetic scrub must never break rendering
            pass

    def _scrub_right_columns(self, count: int) -> None:
        """Scrub the terminal's rightmost ``count`` columns — a full-frame panel's edge."""
        cols, _ = self._size()
        self._scrub_columns(cols - count, cols)

    def _scrub_rows(self, rows: Sequence[int]) -> bool:
        """Force prompt_toolkit to rewrite these whole terminal rows on the next diff.

        The row-wise twin of :meth:`_scrub_columns`, and the cheap form of the wide-glyph
        repaint (:meth:`_emit`): every column of each listed row is sentinelled, so pt finds
        the entire row changed and writes it from column 0 in one contiguous run — which is
        the whole requirement for a row whose glyph the terminal draws narrower than pt
        reserved. Rows that did not change are not touched at all, and nothing is erased.

        Row-scoped is sound because pt's cursor is *relative*: stepping down a row emits
        ``\\r\\n``, which returns the terminal to a true column 0 whatever the drift on the row
        above, so a mis-measured row can never throw off the rows below it. The drift only
        matters *within* a row, and a row rewritten whole never jumps inside itself.

        Args:
            rows: The terminal row indices to mark changed.

        Returns:
            Whether the scrub was applied — ``False`` when pt has no remembered frame to
            scrub (it is already going to repaint everything) or the internals moved.
        """
        renderer = getattr(self._app, "renderer", None)
        last = getattr(renderer, "_last_screen", None)
        if last is None:
            return False
        cols, _ = self._size()
        try:
            from prompt_toolkit.layout.screen import Char

            buffer = last.data_buffer
            sentinel = Char("￿")  # a non-character; never equals real cell content
            for y in rows:
                row = buffer[y]
                for x in range(cols):
                    row[x] = sentinel
        except Exception:  # noqa: BLE001 - a cosmetic scrub must never break rendering
            return False
        return True

    # --- async prompt helpers ------------------------------------------------

    async def run_screen(self, screen: Screen) -> Any:
        """Push a screen, await its result, then pop it.

        Args:
            screen: The screen to run.

        Returns:
            The screen's resolved value, or :data:`~meshterm.ui.tui.screen.CANCEL`.
        """
        loop = asyncio.get_running_loop()
        screen.future = loop.create_future()
        self.push(screen)
        try:
            return await screen.future
        finally:
            self.pop(screen)

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        wrap: bool = True,
        filterable: bool = True,
        footer_hint: Optional[str] = None,
        delete_hint: str = "",
    ) -> Any:
        """Show a select screen; return the chosen value or ``None`` if cancelled.

        ``prompt`` draws an instruction inside the box above the list; ``filterable`` and
        ``footer_hint`` are forwarded for short, fixed lists (a yes-or-no style choice) that
        want no type-to-filter and a tailored hint. ``delete_hint`` (with rows marked
        :attr:`~meshterm.ui.tui.select.Choice.deletable`) surfaces the Delete key's atom
        while the highlight sits on such a row; Delete then resolves a
        :class:`~meshterm.ui.tui.select.DeleteRequest` the caller unwraps.
        """
        kwargs: dict[str, Any] = dict(
            prompt=prompt, default=default, wrap=wrap, filterable=filterable,
            delete_hint=delete_hint,
        )
        if footer_hint is not None:
            kwargs["footer_hint"] = footer_hint
        result = await self.run_screen(SelectScreen(title, items, **kwargs))
        return None if result is CANCEL else result

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Optional[Any] = None,
        footnote: Optional[str] = None,
        footer_hint: str = "↑↓ move · Enter select · Esc quit",
    ) -> Any:
        """Show a chromeless select splash (banner above a content-sized box).

        Like :meth:`select`, but drawn without the header/footer status bars and centered
        under ``banner`` — the startup device picker's presentation. Type-to-filter is off:
        the device list is short and fixed, so stray keys never narrow it. An optional
        ``footnote`` (e.g. a copyright notice) sits muted below the box. When any row opts
        into removal (a :attr:`~meshterm.ui.tui.select.Choice.deletable` row), a "Del remove"
        atom joins the footer — but only while the highlight is actually on such a row, so the
        removal key advertises itself exactly where it acts (see
        :attr:`~meshterm.ui.tui.select.SelectScreen.footer_hint`).
        """
        delete_hint = (
            "Del remove"
            if any(isinstance(it, Choice) and it.deletable for it in items)
            else ""
        )
        screen = SelectScreen(
            title,
            items,
            default=default,
            footer_hint=footer_hint,
            delete_hint=delete_hint,
            filterable=False,
        )
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

    async def confirm_startup(
        self,
        prompt: "str | Text",
        *,
        title: str = "",
        confirm_label: str = "Remove",
        banner: Optional[Any] = None,
        footnote: Optional[str] = None,
        backdrop_items: Optional[list] = None,
        backdrop_default: Any = None,
        backdrop_title: str = "Select a companion device",
        footer_hint: str = "←→ choose · Enter select · Esc cancel",
    ) -> bool:
        """Confirm a destructive splash action with a Cancel/verb dialog (chromeless).

        The startup-splash sibling of :meth:`button_dialog`: the same platform-dialog layout
        — the safe *Cancel* on the left and the committing verb on the right and default, so
        Enter commits and Esc backs out. Themed as data loss (the reserved red prompt and
        border), since it only ever gates forgetting a remembered device — the splash sibling
        of a ``destructive`` :meth:`button_dialog`.

        When ``backdrop_items`` is given, the device list they describe is redrawn as the
        chromeless base and the red confirm *floats over it* as a centred box (a modal popup
        over the pushed backdrop, the way every other dialog behaves) — so removing a device
        reads as a popup on top of the picker rather than a splash that replaces it. The
        ``backdrop_default`` row is pre-highlighted so the confirm reads as being about it.
        Without ``backdrop_items`` the confirm draws as its own chromeless splash under
        ``banner`` (the fallback for a caller with no list to float over).

        Args:
            prompt: The question shown above the buttons.
            title: Short heading shown in the dialog's border.
            confirm_label: Label for the committing button (e.g. ``"Remove"``).
            banner: Wordmark rows drawn above the box (as on the other startup splashes).
            footnote: Muted line drawn below the box.
            backdrop_items: The picker's rows to redraw behind the confirm; ``None`` falls
                back to a standalone chromeless confirm splash.
            backdrop_default: The row value to pre-highlight in the backdrop list.
            backdrop_title: Heading for the backdrop list (the picker's own title).
            footer_hint: Footer key hint.

        Returns:
            ``True`` only when the user chose the committing button; ``False`` on Cancel/Esc.
        """
        dialog = ButtonDialog(
            prompt,
            [("Cancel", False), (confirm_label, True)],
            title=title,
            default=1,
            footer_hint=footer_hint,
            prompt_style="err",
            border_style="err",
        )
        if backdrop_items is None:
            # No list to float over: draw the confirm as its own chromeless splash.
            dialog.chrome = False
            dialog.banner = banner
            dialog.footnote = footnote
            return await self.run_screen(dialog) is True
        # Keep the picker on screen as the chromeless base and float the red confirm over it,
        # so the removal confirm sits *on top of* the device list it acts on. The backdrop is
        # a static redraw of the same rows (it never takes a key — the dialog above owns input).
        backdrop = SelectScreen(
            backdrop_title,
            backdrop_items,
            default=backdrop_default,
            footer_hint="↑↓ move · Enter select · Esc quit",
            delete_hint="Del remove",
            filterable=False,
        )
        backdrop.chrome = False
        backdrop.banner = banner
        backdrop.footnote = footnote
        self.push(backdrop)
        try:
            return await self.run_screen(dialog) is True
        finally:
            self.pop(backdrop)

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Optional[Any] = None,
        footnote: Optional[str] = None,
        footer_hint: str = "Enter continue",
    ) -> None:
        """Show a chromeless message splash (banner above a boxed renderable) until dismissed."""
        screen = ScrollScreen(renderable, title=title, footer_hint=footer_hint)
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        await self.run_screen(screen)

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Optional[Any] = None,
        footnote: Optional[str] = None,
        interval: float = 0.12,
    ) -> Any:
        """Await ``coro`` while showing an animated spinner on the chromeless splash.

        Keeps the startup splash on screen (same wordmark and box) and swaps its contents for
        an ASCII spinner beside ``message`` while the awaited task runs, then returns the
        task's result. A background timer advances the spinner and repaints every
        ``interval`` seconds; it is always cancelled and the splash popped before returning.

        Args:
            message: The line shown beside the spinner (e.g. "Talking to Wio on COM5…").
            coro: The awaitable to run (e.g. a device smoke test).
            title: Optional panel title for the splash box.
            banner: Wordmark rows drawn above the box (as on the other startup splashes).
            footnote: Muted line drawn below the box.
            interval: Seconds between spinner frames.

        Returns:
            Whatever ``coro`` resolves to.
        """
        screen = BusyScreen(message, title=title)
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        self.push(screen)

        async def animate() -> None:
            while True:
                await asyncio.sleep(interval)
                screen.tick()
                self.invalidate()

        ticker = asyncio.ensure_future(animate())
        try:
            return await coro
        finally:
            ticker.cancel()
            # Await the cancelled ticker so it is never garbage-collected while still
            # pending: that surfaces as a screen-corrupting "Task was destroyed but it is
            # pending!" loop error. The spinner is cosmetic, so any glitch is swallowed.
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break startup
                pass
            self.pop(screen)

    async def prompt_pin_startup(
        self,
        device_name: str,
        *,
        error: str = "",
        help_text: str = "",
        banner: Optional[Any] = None,
        footnote: Optional[str] = None,
    ) -> Optional[str]:
        """Ask for a companion's Bluetooth PIN on the chromeless startup splash.

        Drawn like :meth:`notify_startup` / :meth:`select_startup` — a bordered box centered
        under ``banner`` with no status bars — so the PIN request is visually part of the same
        device-selection flow. ``error`` is shown in the box on a re-ask after a rejected code.

        Args:
            device_name: The companion's display name, shown in the prompt.
            error: A rejected-PIN message to display (empty on the first ask).
            help_text: A muted hint under the field.
            banner: Wordmark rows drawn above the box (as on the other startup splashes).
            footnote: Muted line drawn below the box.

        Returns:
            The entered PIN, or ``None`` if the user pressed Esc to cancel.
        """
        screen = PinDialog(device_name, error=error, help_text=help_text)
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

    async def prompt_text_startup(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Optional[Validator] = None,
        help_text: str = "",
        banner: Optional[Any] = None,
        footnote: Optional[str] = None,
    ) -> Optional[str]:
        """Ask for a line of text on the chromeless startup splash (e.g. a TCP host:port).

        Drawn like :meth:`prompt_pin_startup` — a bordered :class:`TextScreen` centered under
        ``banner`` with no status bars — so entering a network address reads as part of the
        same device-selection flow. ``validate`` blocks submission on a bad value the same way
        the in-menu text prompt does.

        Args:
            title: Short heading shown in the dialog's border.
            prompt: The instruction shown inside the box, above the field.
            default: Prefilled text.
            validate: Optional validator run on Enter; a returned string blocks submission.
            help_text: A muted hint under the field.
            banner: Wordmark rows drawn above the box (as on the other startup splashes).
            footnote: Muted line drawn below the box.

        Returns:
            The entered text, or ``None`` if the user pressed Esc to cancel.
        """
        screen = TextScreen(
            title, prompt=prompt, default=default, validate=validate, help_text=help_text
        )
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Show a drag-with-arrows reorder screen; return the final order of row indices.

        The Apply action row below the list commits the rearrangement; Back (or Esc)
        cancels, which comes back as the original (identity) order so the caller
        treats it as "no change".
        """
        result = await self.run_screen(ReorderScreen(title, labels))
        return list(range(len(labels))) if result is CANCEL else result

    async def text(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Optional[Validator] = None,
        help_text: str = "",
        password: bool = False,
        byte_limit: Optional[int] = None,
        floating: bool = False,
    ) -> Optional[str]:
        """Show a text prompt; return the string or ``None`` if cancelled.

        ``floating`` guarantees the prompt draws as a centered popup even on an empty stack
        — a modal step mid-flow (a remote-admin password) rather than a tool's full-frame
        entry screen. It then floats over a blank base the way :meth:`button_dialog` and
        :meth:`typed_confirm` do (see :meth:`_run_dialog_screen`); with a screen already
        beneath it there is no difference, so leave it ``False`` for a prompt that is itself
        a tool's primary screen (the Trace target's typed fallback).

        ``byte_limit`` puts the shared UTF-8 byte gauge on the field and blocks submission
        past it — for a field that feeds a size-capped packet (see :class:`TextScreen`).
        """
        screen = TextScreen(
            title,
            prompt=prompt,
            default=default,
            validate=validate,
            help_text=help_text,
            password=password,
            byte_limit=byte_limit,
        )
        runner = self._run_dialog_screen if floating else self.run_screen
        result = await runner(screen)
        return None if result is CANCEL else result

    async def confirm(self, title: str, *, default: bool = True) -> Optional[bool]:
        """Show a yes/no prompt; return the bool or ``None`` if cancelled."""
        result = await self.run_screen(ConfirmScreen(title, default=default))
        return None if result is CANCEL else result

    async def button_dialog(
        self,
        prompt: "str | Text",
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: Optional[dict[str, Any]] = None,
        footer_hint: str = "←→ choose · Enter select · Esc cancel",
        prompt_style: str = "",
        button_style: str = "selected",
        button_idle_style: str = "muted",
        border_style: str = "accent",
    ) -> Any:
        """Show a centered button dialog; return the chosen value or ``None`` if cancelled.

        A reusable prompt-above-buttons dialog (see :class:`~meshterm.ui.tui.prompt.
        ButtonDialog`): the colours, prompt, buttons, and single-key shortcuts are all
        parametrised, so a caller can theme it (e.g. a destructive action in red) or wire
        instant y/n keys. ``keys`` maps a shortcut character to the value it commits.
        The prompt may be a pre-styled :class:`Text` (see the ButtonDialog docs).
        """
        screen = ButtonDialog(
            prompt,
            buttons,
            title=title,
            default=default,
            keys=keys,
            footer_hint=footer_hint,
            prompt_style=prompt_style,
            button_style=button_style,
            button_idle_style=button_idle_style,
            border_style=border_style,
        )
        result = await self._run_dialog_screen(screen)
        return None if result is CANCEL else result

    async def _run_dialog_screen(self, screen: Screen) -> Any:
        """Run a floating dialog, guaranteeing it draws as a centered box, not full-frame.

        On an empty stack a lone floating screen is drawn *as* the background — framed
        chrome filling the terminal, no popup — so a blank base is pushed beneath it first
        (the trick the reconnect dialog and the message popup already use) and popped once
        it closes. With a background already present the dialog simply floats over it.
        """
        backdrop: Optional[ScrollScreen] = None
        if not self._stack:
            backdrop = ScrollScreen("", floating=False, footer_hint="")
            self.push(backdrop)
        try:
            return await self.run_screen(screen)
        finally:
            if backdrop is not None:
                self.pop(backdrop)

    async def typed_confirm(
        self, warning: str, word: str, *, title: str = "Are you sure?"
    ) -> bool:
        """Gate a destructive action behind typing ``word``; return whether it was typed.

        Shows the error-themed :class:`~meshterm.ui.tui.prompt.TypedConfirmDialog` and
        collapses its result to a plain bool: ``True`` only when the user typed the word,
        ``False`` when they backed out with Esc.
        """
        result = await self._run_dialog_screen(
            TypedConfirmDialog(warning, word, title=title)
        )
        return result is True

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Optional[Validator] = None,
    ) -> Optional[str]:
        """Show a free-text prompt with suggestions; return text or ``None`` if cancelled."""
        result = await self._run_dialog_screen(
            AutocompleteScreen(
                title, choices, prompt=prompt, default=default, validate=validate
            )
        )
        return None if result is CANCEL else result

    async def message_dialog(self, message: "Text | str", *, title: str = "") -> None:
        """Show a short outcome in a centered popup with a single OK button.

        The lightweight acknowledgement counterpart of :meth:`scroll`: a one-line result
        ("✓ flood advertisement sent") doesn't warrant a full result window, so it floats
        as a small dialog over whatever screen is beneath — Enter (OK) or Esc dismisses
        it. The border takes the message's strongest tone (see :func:`_message_border`),
        so an error pops red while a success stays in the standard accent. It delegates to
        :meth:`button_dialog`, which floats it over a blank base when the stack is empty (a
        tool run straight from the menu, which is popped while the tool executes).

        Args:
            message: The outcome to show — a pre-styled :class:`Text` (note markup
                survives into the dialog) or a plain string.
            title: Optional dialog heading (typically the tool or action name).
        """
        await self.button_dialog(
            message,
            [("OK", "ok")],
            title=title,
            footer_hint="Enter OK",
            border_style=_message_border(message),
        )

    async def scroll(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Show a dismissable, scrollable view of a renderable (a result window)."""
        screen = ScrollScreen(
            renderable,
            title=title,
            footer_hint=footer_hint or "↑↓ PgUp/PgDn scroll · Esc close",
        )
        await self.run_screen(screen)

    def progress(self, title: str = "Working") -> TuiProgress:
        """Return a progress context manager backed by a pushed :class:`ProgressScreen`."""
        return TuiProgress(self, title)

    @asynccontextmanager
    async def busy_overlay(
        self,
        message: str = "",
        *,
        title: str = "",
        interval: float = 0.06,
    ) -> AsyncIterator[BusyOverlay]:
        """Float a skeleton card on top of everything for the duration of a block.

        Wrap a slow, screen-affecting operation — most usefully a device menu navigation,
        which can otherwise sit on a blank frame while the companion answers — in::

            async with session.busy_overlay("reading…", title="Nodes"):
                await slow_work()

        The card stands in for the screen being fetched: a title, the one-cell working chip
        beside the caption, and a Knight-Rider scanning-light bar. A background timer advances the chip,
        fades the card in, and repaints while the block runs; it is always cleared and the
        timer cancelled on exit, even on error. The card fades in from black rather than
        popping in (see :attr:`BusyOverlay.brightness`), so a quick operation only paints a
        near-black card and it never appears suddenly. It is drawn only in the gaps between
        screens (see :meth:`_overlay_visible`), so it announces the wait without covering a
        prompt the user is interacting with.

        Args:
            message: An optional caption drawn beside the working chip.
            title: An optional heading naming the screen being fetched.
            interval: Seconds between animation frames (also the fade's repaint cadence).

        Yields:
            The live :class:`BusyOverlay`, in case the caller wants to update its caption.
        """
        # A nested busy_overlay keeps the outer one (the outermost wait owns the screen); its
        # own body still runs, it just doesn't install a second card.
        if self._overlay is not None:
            yield self._overlay
            return
        overlay = BusyOverlay(message, title=title)
        self._overlay = overlay
        if self._overlay_visible():
            self.invalidate()  # start the fade-in promptly, before the first tick

        async def animate() -> None:
            while True:
                await asyncio.sleep(interval)
                overlay.tick()
                # Only repaint when the card is actually on screen, so an overlay waiting
                # behind a live prompt doesn't churn that prompt's repaints for nothing.
                if self._overlay_visible():
                    self.invalidate()

        ticker = asyncio.ensure_future(animate())
        try:
            yield overlay
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a cosmetic overlay must never break a flow
                pass
            self._overlay = None
            self.invalidate()

    # --- application lifecycle ------------------------------------------------

    async def run(self, main: Any) -> None:
        """Run ``main`` (the menu loop) inside the full-screen application.

        Starts the prompt_toolkit event loop, drives ``main`` as a background task on that
        same loop, and exits the application when ``main`` returns. Any exception from
        ``main`` is re-raised after the loop unwinds.

        Args:
            main: The coroutine driving the session (typically the menu loop).
        """
        self._app = self._build_app()
        if get_platform().modifier_watch:
            # The Shift watcher flips the F-key lane's labels live. It reports from its
            # own thread; hop onto the app loop for the repaint. Failure to engage (no
            # device, no permission) just leaves the lane static — see the module doc.
            loop = asyncio.get_running_loop()
            modifier_watch.start(lambda: loop.call_soon_threadsafe(self.invalidate))
        box: dict[str, BaseException] = {}

        async def driver() -> None:
            try:
                await main
            except BaseException as exc:  # noqa: BLE001 - re-raised after the app unwinds
                box["exc"] = exc
            finally:
                if self._app is not None and not self._app.is_done:
                    self._app.exit()

        def pre_run() -> None:
            asyncio.ensure_future(driver())

        # Don't let prompt_toolkit install its own loop exception handler: on any stray
        # background-task error it prints a traceback and a "Press ENTER to continue..."
        # prompt straight over the full-screen UI. With it disabled, asyncio's default
        # handler logs such errors to the ``asyncio`` logger instead, which is routed to the
        # file log (see :func:`meshterm.persistence.logging.configure_logging`) and never
        # touches the screen. Errors from ``main`` still propagate via ``driver``/``box``.
        await self._app.run_async(pre_run=pre_run, set_exception_handler=False)
        if "exc" in box:
            raise box["exc"]

    def _build_app(self) -> Application:
        """Construct the prompt_toolkit application, layout, and key bindings."""
        base_control = FormattedTextControl(self._render_base, focusable=True)
        base_window = Window(base_control, always_hide_cursor=True)
        # One centered-box float per stacked dialog layer, bottom-to-top: each renders the
        # k-th screen floating above the background (see :meth:`_float_layers`), so a dialog
        # opened over an existing popup draws *over* it — both at their own size — instead of
        # the lower one being stretched to fill the frame. A generous fixed pool covers any
        # realistic nesting; the ConditionalContainer hides the layers not in use this frame.
        dialog_floats = [
            Float(
                ConditionalContainer(
                    Window(
                        FormattedTextControl(
                            lambda i=i: self._render_float_layer(i)
                        ),
                        always_hide_cursor=True,
                    ),
                    filter=Condition(lambda i=i: len(self._float_layers()) > i),
                )
            )
            for i in range(_MAX_DIALOG_LAYERS)
        ]
        # The busy overlay is the last float, so it draws on top of every dialog float — the
        # top of the z-order. It is a content-sized window (dont_extend_*) with no anchors, so
        # the FloatContainer centres just its skeleton card over the screen rather than blanking it.
        overlay_window = Window(
            FormattedTextControl(self._render_overlay),
            always_hide_cursor=True,
            dont_extend_width=True,
            dont_extend_height=True,
        )
        root = FloatContainer(
            content=base_window,
            floats=[
                *dialog_floats,
                Float(
                    ConditionalContainer(
                        overlay_window, filter=Condition(self._overlay_visible)
                    )
                ),
            ],
        )
        app = Application(
            layout=Layout(root, focused_element=base_window),
            key_bindings=self._key_bindings(),
            full_screen=True,
            mouse_support=False,
            # Keeps the live monitor counter in the header ticking. Per-platform, so a host
            # where an idle repaint is expensive can breathe more slowly between frames.
            refresh_interval=get_platform().tick_s,
            # Repaint as soon as the loop is free, rather than spinning the event loop for
            # up to 10 ms first. prompt_toolkit's default postpone is there to protect a
            # UI whose *own* output floods it with invalidations (its motivating case was
            # a terminal multiplexer); here an invalidation is a keystroke or the 2 s
            # header tick, so the delay buys nothing and was measured as a flat 3 ms on
            # the PicoCalc and 10 ms on desktop, paid on every single key.
            max_render_postpone_time=None,
            input=self._input,
            output=self._resolve_output(),
        )
        if fastrender.enabled():
            # Swap in the row-diff renderer for plain full-screen frames. Built with the
            # same arguments Application gave the stock one, so everything except the
            # paint itself — CPR, alternate screen, mouse, cursor shape — is unchanged.
            app.renderer = fastrender.FastRenderer(
                app._merged_style,
                app.output,
                full_screen=True,
                mouse_support=False,
                cpr_not_supported_callback=app.cpr_not_supported_callback,
                frame_source=self._plain_frame,
            )
        return app

    def _resolve_output(self) -> Any:
        """The output the app renders to — optionally widened to reclaim the last column.

        Only the *real* terminal (``self._output is None``, so prompt_toolkit would build its
        own output) is wrapped, and only when :func:`_reclaim_last_column` says on: a test
        that supplies its own output keeps the exact size it set, so headless rendering stays
        deterministic. See :class:`_WidthExtendedOutput` for what the wrap does.
        """
        if self._output is not None or not _reclaim_last_column():
            return self._output
        from prompt_toolkit.output.defaults import create_output

        return _WidthExtendedOutput(create_output())

    # --- rendering -----------------------------------------------------------

    def _size(self) -> tuple[int, int]:
        """Return the current (cols, rows) of the terminal."""
        size = self._app.output.get_size()  # type: ignore[union-attr]
        return max(20, size.columns), max(6, size.rows)

    def base_body_size(self) -> tuple[int, int]:
        """Return the ``(width, height)`` in cells available to the base screen's body.

        Mirrors the layout math in :func:`~meshterm.ui.tui.frame.compose_base` so a
        full-screen screen (e.g. the map) can size its own content to fill the frame exactly,
        without waiting a repaint to learn its height. *Both* of that function's branches:
        a borderless platform swaps the panel's two border rows and four padding columns for
        a single title-bar row, so a screen sizing itself against the bordered math there
        would leave a row of the frame it was handed permanently blank.

        Returns:
            The inner content width and the body viewport height, both in character cells.
        """
        from .render import render_lines

        cols, rows = self._size()
        header_h = len(render_lines(self._header(cols), cols, no_wrap=True))
        if get_platform().frame_border:
            # minus footer(1) and panel border(2); the border eats 4 columns too
            return cols - 4, max(1, rows - header_h - 1 - 2)
        return cols, max(1, rows - header_h - 1 - 1)  # minus footer(1) and title bar(1)

    def _base_index(self) -> int:
        """Stack index of the full-frame background screen.

        The background is the top-most *non-floating* screen — a menu, map, or list that
        fills the frame — and every floating dialog above it is drawn as a centered box
        over it (see :meth:`_float_layers`). When the whole stack is floating (a tool
        whose own primary screen is a floating select, e.g. Channels), the bottom screen
        is the background: it is the one the dialogs above it should float over.
        """
        for i in range(len(self._stack) - 1, -1, -1):
            if not getattr(self._stack[i], "floating", False):
                return i
        return 0

    def _base_screen(self) -> Optional[Screen]:
        """The screen drawn as the full-frame background, or ``None`` when the stack is empty."""
        if not self._stack:
            return None
        return self._stack[self._base_index()]

    def _float_layers(self) -> list[Screen]:
        """The floating dialogs stacked above the background, bottom-to-top.

        Each is drawn as its own centered box over the ones beneath — so opening a dialog
        over an existing popup leaves that popup at its own size rather than stretching it
        to fill the frame (the single-float compositor's old failing).
        """
        if not self._stack:
            return []
        return self._stack[self._base_index() + 1:]

    def _has_float(self) -> bool:
        """Whether any dialog floats over the background this frame."""
        return bool(self._float_layers())

    def _emit(self, text: str, layer: str = "base") -> ANSI:
        """Wrap a composed frame as prompt_toolkit :class:`ANSI`, repainting whole *rows*
        when it holds a glyph the terminal may draw narrower than pt reserves for it.

        prompt_toolkit paints differentially: it rewrites only the cells that changed since
        the last frame, and it steps the cursor *relative* to its own width model. That is
        sound only while every glyph is one cell wide. A width-2 glyph the terminal draws in
        a single cell (an emoji in a chat line, a menu icon) leaves everything to its right on
        that row one column left of where pt thinks it is; a later paint that jumps into the
        row — skipping the unchanged emoji — writes at pt's column, one past the content it
        meant to overwrite, and the stale cell lingers.

        The requirement that fixes is narrow: a row carrying such a glyph must be rewritten
        *whole*, from column 0, so the terminal's own cursor advance re-lays it. It does not
        need the screen erased, and it does not need the rows around it touched — pt steps
        down a row with ``\\r\\n``, which returns the terminal to a true column 0 whatever the
        drift above it, so the misalignment can never spread past the row it is on.

        So the changed rows are rewritten and nothing else is (:meth:`_scrub_rows`). Three
        things narrow it to that:

        * **Nothing changed → nothing to do.** The app repaints on a 1 Hz timer to tick the
          header's pulse, and an idle screen composes identically each time. pt writes no
          cells, so its cursor cannot drift; the previous paint already left the terminal
          aligned. (This alone is the flicker: erasing an emoji-bearing screen and rewriting
          it identically, once a second.)
        * **No wide glyph drawn → nothing to do.** The plain differential paint is exact.
        * **Otherwise, only the rows that changed.** The background composes one line per
          terminal row, so its diff maps straight onto rows — a ticking header repaints the
          header, not the screen under it. A *float* is a centred box whose rows the layout
          places, not us, so a dialog changing (or closing — see :meth:`_reconcile_layers`)
          still falls back to dropping pt's cached frame, as does a frame whose height moved.
          Those are user-driven and occasional; the timer is neither.
        """
        entry = (text, _has_wide_glyph(text))
        previous = self._layers.get(layer)
        if previous == entry:
            return ANSI(text)
        self._layers[layer] = entry
        if any(wide for _text, wide in self._layers.values()):
            rows = _changed_rows(previous[0], text) if previous is not None else None
            # The background's line i *is* terminal row i; nothing else can claim that.
            if layer != "base" or rows is None or not self._scrub_rows(rows):
                self._invalidate_last_frame()
        return ANSI(text)

    def _repaint_if_wide(self) -> None:
        """Upgrade this paint to a full repaint if any drawn layer holds a wide glyph."""
        if any(wide for _text, wide in self._layers.values()):
            self._invalidate_last_frame()

    def _reconcile_layers(self) -> None:
        """Forget the layers this paint won't draw, and treat their leaving as a change.

        A float is hidden by dropping its window from the layout, so a closing dialog simply
        stops calling :meth:`_emit` — nothing would otherwise notice it had gone, and the
        cells it gives back to the base would be rewritten piecemeal over a row the terminal
        may be drawing shifted. Called at the top of the paint, from the base layer, since
        the background is composed before the floats above it.
        """
        live = {f"float{i}" for i in range(len(self._float_layers()))}
        if self._base_screen() is not None:
            live.add("base")
        if self._overlay_visible():
            live.add("overlay")
        gone = [layer for layer in self._layers if layer not in live]
        for layer in gone:
            self._layers.pop(layer)
        if gone:
            self._repaint_if_wide()

    def _render_base(self) -> ANSI:
        """Render the persistent frame around the background screen."""
        self._reconcile_layers()  # the paint starts here: the background composes first
        cols, rows = self._size()
        base = self._base_screen()
        if base is None:
            return ANSI("")
        scrub = base.consume_edge_scrub()
        if scrub:
            self._scrub_right_columns(scrub)
        # A chromeless base (the startup splash) forgoes the header/footer bars and is
        # centered under its banner instead of stretched across the terminal.
        if not base.chrome:
            return self._emit(frame.compose_startup(base, cols, rows))
        footer = self.top.footer_hint if self.top else base.footer_hint
        lane = self._fkey_lane(self.top or base)
        return self._emit(
            frame.compose_base(self._header(cols), base, footer, cols, rows, footer_lane=lane)
        )

    def _plain_frame(self) -> Optional[str]:
        """The whole frame as rows, when this paint is one the fast path may take.

        The background screen, with every floating dialog composited over it exactly where
        prompt_toolkit's ``FloatContainer`` would centre it (see
        :func:`~meshterm.ui.tui.frame.composite_float`) — so a confirm, a picker or the
        packet viewer stays on the row-diff path rather than paying prompt_toolkit's whole
        grid rebuild on every keystroke.

        Answers ``None`` — meaning "let prompt_toolkit lay this one out" — for the two
        frames we don't place ourselves: the busy overlay, which is a content-sized window
        the float container measures, and an empty stack, which has no background at all.

        Every layer still goes through its usual renderer (:meth:`_render_base`,
        :meth:`_render_float_layer`), so the layer bookkeeping those keep is identical
        whichever path the paint takes.
        """
        if self._overlay_visible() or not self._stack:
            return None
        rows_out = self._render_base().value.split("\n")
        layers = self._float_layers()
        if layers:
            cols, rows = self._size()
            for index in range(len(layers)):
                rows_out = frame.composite_float(
                    rows_out, self._render_float_layer(index).value, cols, rows
                )
        return "\n".join(rows_out)

    @staticmethod
    def _fkey_lane(active: Screen) -> Optional[Callable[[], Text]]:
        """A deferred F-key lane row for ``active``, or ``None`` on a platform without one.

        Deferred because the frame resolves it *after* the body renders: a lane dims the
        slots whose action would do nothing, and that reading comes from the scroll
        metrics this paint is about to record (see :func:`~meshterm.ui.tui.fkeys.default_lane`).
        """
        if not get_platform().footer_fkeys:
            return None
        return lambda: fkeys.lane_text(active.fkey_lane, shifted=modifier_watch.shift_down())

    def _render_float_layer(self, index: int) -> ANSI:
        """Render the ``index``-th floating dialog (bottom-to-top) as a centered box."""
        layers = self._float_layers()
        if index >= len(layers):
            return ANSI("")
        cols, rows = self._size()
        return self._emit(frame.compose_dialog(layers[index], cols, rows), f"float{index}")

    def _overlay_visible(self) -> bool:
        """Whether the busy overlay should be painted this frame.

        Gated on an empty screen stack so the card only appears in the "black screen" gaps a
        device operation opens between screens (a menu navigation before the tool's first
        prompt, say) and never buries a dialog the user is meant to be reading. It also stays
        unpainted through the overlay's initial hold (:attr:`BusyOverlay.brightness` is 0), so
        an operation that finishes within the hold shows nothing and never flashes.
        """
        return self._overlay is not None and not self._stack and self._overlay.brightness > 0

    def _render_overlay(self) -> ANSI:
        """Render the busy overlay's skeleton card (only when :meth:`_overlay_visible`)."""
        if self._overlay is None:
            return ANSI("")
        return self._emit(self._overlay.render(), "overlay")

    # --- input ---------------------------------------------------------------

    def _key_bindings(self) -> KeyBindings:
        """Build the global key bindings that dispatch normalized actions to the top.

        Every key — navigation, Ctrl chord, typed character, pasted run — funnels through
        :meth:`_dispatch`, which is what lets the right-Ctrl rescue there cover the whole app
        rather than the screen actions only.
        """
        kb = KeyBindings()

        def bind(key: Any, action: str) -> None:
            @kb.add(key)
            def _(event: Any, action: str = action) -> None:  # noqa: ANN401
                self._dispatch(action)

        for key, action in _KEY_ACTIONS.items():
            bind(key, action)

        @kb.add(Keys.Any)
        def _typed(event: Any) -> None:  # noqa: ANN401
            data = event.data
            if not data:
                return
            if len(data) == 1 and data.isprintable():
                self._dispatch("text", data)
            elif len(data) > 1:
                # A bracketed paste (Ctrl-V, right-click, Ctrl-Shift-V) arrives as one
                # multi-character run — hand the whole thing to the top screen as a paste it
                # can confirm and insert, rather than dropping it as the old len==1 guard did.
                self._dispatch("paste", data)

        return kb

    def _dispatch(self, action: str, data: str = "") -> None:
        """Forward an action to the top screen and repaint.

        Right Ctrl is read as Ctrl first, app-wide: a plain navigation key, or *any* bare
        letter arriving as text, is promoted to its Ctrl chord while the right Ctrl key is
        physically held (see :func:`_right_ctrl_down`, :data:`_CTRL_CHORDS`,
        :data:`_CTRL_LETTER_CHORDS`) — a no-op when the console already reported the chord, and
        the rescue when a layout-claimed right Ctrl stripped it to a bare character. Because
        every binding funnels through here, the rescue covers the session's own chords too, not
        just the screen actions: right Ctrl-V pastes into a compose line instead of typing a
        ``v``, right Ctrl-C quits.

        The two session-level actions are answered here rather than forwarded — no screen ever
        sees ``quit`` or ``paste_clipboard``.

        The repaint keeps prompt_toolkit's fast differential paint; a frame carrying a glyph
        the terminal may draw narrower than pt reserves for it (an emoji) upgrades itself to a
        full repaint at compose time — see :meth:`_emit`.
        """
        # An F-key resolves against the top screen's lane (see ui.tui.fkeys) into a
        # normal screen action; an unassigned slot, or a platform without the lane,
        # drops the press here.
        if len(action) in (2, 3) and action[0] == "f" and action[1:].isdigit():
            number = int(action[1:])
            if number > 5:
                # However this resolves, the code only exists because Shift was
                # physically down for the MCU to emit it — latch the lane's shifted
                # display against the release/re-assert flicker (see modifier_watch).
                modifier_watch.note_shift_bank_key()
            top = self.top or self._base_screen()
            resolved = fkeys.action_for(top.fkey_lane, number) if top else None
            if resolved is None:
                return
            action = resolved
        # The key-state probe comes last in each test, so it only runs for a key that could
        # be a chord at all — not on every keystroke.
        if action in _CTRL_CHORDS and _right_ctrl_down():
            action = _CTRL_CHORDS[action]
        elif action == "text" and data.lower() in _CTRL_LETTER_CHORDS and _right_ctrl_down():
            action, data = _CTRL_LETTER_CHORDS[data.lower()], ""
        if action == "quit":
            if self._app is not None:
                self._app.exit()
            return
        if action == "paste_clipboard":
            # Some terminals deliver Ctrl-V as the literal control key — no bracketed-paste
            # sequence, so no text on the event. Read the OS clipboard ourselves and hand the
            # run to the top screen as a paste. Terminals that instead translate Ctrl-V into a
            # bracketed paste never reach here — that lands in _typed as a multi-char run.
            text = _read_clipboard()
            if text:
                self._dispatch("paste", text)
            return
        top = self.top
        if top is not None:
            top.handle(action, data)
        self.invalidate()


__all__ = [
    "TuiSession",
    "Choice",
    "Separator",
    "SelectScreen",
    "ReorderScreen",
    "ScrollScreen",
]
