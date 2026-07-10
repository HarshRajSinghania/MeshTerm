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
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import RenderableType
from rich.text import Text

from . import frame
from .overlay import BusyOverlay
from .progress import TuiProgress
from .prompt import (
    AutocompleteScreen,
    ButtonDialog,
    ConfirmScreen,
    PinDialog,
    TextScreen,
    Validator,
)
from .screen import CANCEL, BusyScreen, Screen, ScrollScreen
from .select import Choice, ReorderScreen, SelectScreen, Separator

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
    Keys.Enter: "enter",
    Keys.Escape: "escape",
    Keys.Backspace: "backspace",
    Keys.Delete: "delete",
    Keys.Tab: "tab",
    Keys.ControlR: "retry",
}


class TuiSession:
    """A running full-screen TUI: screen stack, frame, input loop, and async prompts."""

    def __init__(
        self,
        header: Optional[Callable[[], RenderableType]] = None,
        *,
        input: Any = None,  # noqa: A002 - matches prompt_toolkit's Application(input=) name
        output: Any = None,
    ) -> None:
        """Create a session.

        Args:
            header: Callable returning the persistent header renderable (banner + live
                status), re-invoked on every repaint. Defaults to a plain title.
            input: Optional prompt_toolkit input to drive the app from (tests use a pipe);
                defaults to the real terminal.
            output: Optional prompt_toolkit output to render to (tests use a dummy);
                defaults to the real terminal.
        """
        self._stack: list[Screen] = []
        self._header = header or (lambda: Text("MeshTerm", style="brand"))
        self._app: Optional[Application] = None
        self._input = input
        self._output = output
        # The top-most floating "working" overlay (a ring spinner), or None when idle. It is
        # deliberately *not* on the screen stack: it hovers above every layer and is shown/
        # hidden by busy_overlay, independent of whatever screens are pushed.
        self._overlay: Optional[BusyOverlay] = None

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
        if screen is None or self._stack[-1] is screen:
            self._stack.pop()
        elif screen in self._stack:
            self._stack.remove(screen)
        self._expose_overlay()
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

        A prompt pushed over an active overlay hides the ring; popping back to an empty stack
        re-exposes it. Restart the intro then so the black hold and fade-in replay fresh each
        time the ring is shown, rather than snapping back at full brightness (see
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

    def _scrub_right_columns(self, count: int) -> None:
        """Force prompt_toolkit to repaint the rightmost ``count`` columns on the next diff.

        Overwrites those cells in pt's remembered last frame with a sentinel that can't equal
        any real content, so the differential renderer treats them as changed and redraws
        them — scrubbing a double-width fallback glyph that smeared over the panel's right
        edge, without the whole-frame flicker of dropping the entire cached frame. Touches a
        pt internal, so it fails soft if the structure ever moves.
        """
        renderer = getattr(self._app, "renderer", None)
        last = getattr(renderer, "_last_screen", None)
        if last is None:
            return
        try:
            from prompt_toolkit.layout.screen import Char

            buffer = last.data_buffer
            cols, _ = self._size()
            sentinel = Char("￿")  # a non-character; never equals real cell content
            for x in range(max(0, cols - count), cols):
                for row in list(buffer.keys()):
                    buffer[row][x] = sentinel
        except Exception:  # noqa: BLE001 - a cosmetic scrub must never break rendering
            pass

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
        default: Any = None,
        wrap: bool = True,
        filterable: bool = True,
        footer_hint: Optional[str] = None,
    ) -> Any:
        """Show a select screen; return the chosen value or ``None`` if cancelled.

        ``filterable`` and ``footer_hint`` are forwarded to the screen for short, fixed
        lists (a yes-or-no style choice) that want no type-to-filter and a tailored hint.
        """
        screen = (
            SelectScreen(title, items, default=default, wrap=wrap, filterable=filterable, footer_hint=footer_hint)
            if footer_hint is not None
            else SelectScreen(title, items, default=default, wrap=wrap, filterable=filterable)
        )
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Optional[Any] = None,
        footnote: Optional[str] = None,
        footer_hint: str = "↑↓ move · Enter select · Esc skip",
    ) -> Any:
        """Show a chromeless select splash (banner above a content-sized box).

        Like :meth:`select`, but drawn without the header/footer status bars and centered
        under ``banner`` — the startup device picker's presentation. Type-to-filter is off:
        the device list is short and fixed, so stray keys never narrow it. An optional
        ``footnote`` (e.g. a copyright notice) sits muted below the box.
        """
        screen = SelectScreen(
            title, items, default=default, footer_hint=footer_hint, filterable=False
        )
        screen.chrome = False
        screen.banner = banner
        screen.footnote = footnote
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

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

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Show a drag-with-arrows reorder screen; return the final order of row indices."""
        result = await self.run_screen(ReorderScreen(title, labels))
        return list(range(len(labels))) if result is CANCEL else result

    async def text(
        self,
        title: str,
        *,
        default: str = "",
        validate: Optional[Validator] = None,
        help_text: str = "",
        password: bool = False,
    ) -> Optional[str]:
        """Show a text prompt; return the string or ``None`` if cancelled."""
        result = await self.run_screen(
            TextScreen(
                title,
                default=default,
                validate=validate,
                help_text=help_text,
                password=password,
            )
        )
        return None if result is CANCEL else result

    async def confirm(self, title: str, *, default: bool = True) -> Optional[bool]:
        """Show a yes/no prompt; return the bool or ``None`` if cancelled."""
        result = await self.run_screen(ConfirmScreen(title, default=default))
        return None if result is CANCEL else result

    async def button_dialog(
        self,
        prompt: str,
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
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        default: str = "",
        validate: Optional[Validator] = None,
    ) -> Optional[str]:
        """Show a free-text prompt with suggestions; return text or ``None`` if cancelled."""
        result = await self.run_screen(
            AutocompleteScreen(title, choices, default=default, validate=validate)
        )
        return None if result is CANCEL else result

    async def scroll(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Show a dismissable, scrollable view of a renderable (a result window)."""
        screen = ScrollScreen(
            renderable,
            title=title,
            footer_hint=footer_hint or "↑↓ PgUp/PgDn scroll · Esc back",
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
        interval: float = 0.06,
    ) -> AsyncIterator[BusyOverlay]:
        """Float an animated ring spinner on top of everything for the duration of a block.

        Wrap a slow, screen-affecting operation — most usefully a device menu navigation,
        which can otherwise sit on a blank frame while the companion answers — in::

            async with session.busy_overlay("Talking to your companion…"):
                await slow_work()

        A background timer advances the ring, fades it in, and repaints while the block runs;
        the overlay is always cleared and the timer cancelled on exit, even on error. The ring
        fades in from black rather than popping in (see :attr:`BusyOverlay.brightness`), so a
        quick operation only paints a near-black ring and it never appears suddenly. It is
        drawn only in the gaps between screens (see :meth:`_overlay_visible`), so it announces
        the wait without covering a prompt the user is interacting with.

        Args:
            message: An optional caption drawn beneath the ring.
            interval: Seconds between animation frames (also the fade's repaint cadence).

        Yields:
            The live :class:`BusyOverlay`, in case the caller wants to update its caption.
        """
        # A nested busy_overlay keeps the outer one (the outermost wait owns the screen); its
        # own body still runs, it just doesn't install a second ring.
        if self._overlay is not None:
            yield self._overlay
            return
        overlay = BusyOverlay(message)
        self._overlay = overlay
        if self._overlay_visible():
            self.invalidate()  # start the fade-in promptly, before the first tick

        async def animate() -> None:
            while True:
                await asyncio.sleep(interval)
                overlay.tick()
                # Only repaint when the ring is actually on screen, so an overlay waiting
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
        float_window = Window(
            FormattedTextControl(self._render_float), always_hide_cursor=True
        )
        # The busy overlay is the last float, so it draws on top of the dialog float — the
        # top of the z-order. It is a content-sized window (dont_extend_*) with no anchors, so
        # the FloatContainer centres just its ring box over the screen rather than blanking it.
        overlay_window = Window(
            FormattedTextControl(self._render_overlay),
            always_hide_cursor=True,
            dont_extend_width=True,
            dont_extend_height=True,
        )
        root = FloatContainer(
            content=base_window,
            floats=[
                Float(
                    ConditionalContainer(
                        float_window, filter=Condition(self._has_float)
                    )
                ),
                Float(
                    ConditionalContainer(
                        overlay_window, filter=Condition(self._overlay_visible)
                    )
                ),
            ],
        )
        return Application(
            layout=Layout(root, focused_element=base_window),
            key_bindings=self._key_bindings(),
            full_screen=True,
            mouse_support=False,
            refresh_interval=1.0,  # keep the live monitor counter in the header ticking
            input=self._input,
            output=self._output,
        )

    # --- rendering -----------------------------------------------------------

    def _size(self) -> tuple[int, int]:
        """Return the current (cols, rows) of the terminal."""
        size = self._app.output.get_size()  # type: ignore[union-attr]
        return max(20, size.columns), max(6, size.rows)

    def base_body_size(self) -> tuple[int, int]:
        """Return the ``(width, height)`` in cells available to the base screen's body.

        Mirrors the layout math in :func:`~meshterm.ui.tui.frame.compose_base` so a
        full-screen screen (e.g. the map) can size its own content to fill the frame exactly,
        without waiting a repaint to learn its height.

        Returns:
            The inner content width and the body viewport height, both in character cells.
        """
        from .render import render_lines

        cols, rows = self._size()
        header_h = len(render_lines(self._header(), cols, no_wrap=True))
        viewport = max(1, rows - header_h - 1 - 2)  # minus footer(1) and panel border(2)
        return cols - 4, viewport

    def _base_screen(self) -> Optional[Screen]:
        """The screen drawn as the background (parent of a floating dialog, else the top)."""
        if not self._stack:
            return None
        if self._has_float() and len(self._stack) >= 2:
            return self._stack[-2]
        return self._stack[-1]

    def _has_float(self) -> bool:
        """Whether the top screen should be drawn as a centered dialog over the base."""
        return len(self._stack) >= 2 and bool(getattr(self.top, "floating", False))

    def _render_base(self) -> ANSI:
        """Render the persistent frame around the background screen."""
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
            return ANSI(frame.compose_startup(base, cols, rows))
        footer = self.top.footer_hint if self.top else base.footer_hint
        return ANSI(frame.compose_base(self._header(), base, footer, cols, rows))

    def _render_float(self) -> ANSI:
        """Render the top screen as a centered dialog (only when floating)."""
        if not self._has_float() or self.top is None:
            return ANSI("")
        cols, rows = self._size()
        return ANSI(frame.compose_dialog(self.top, cols, rows))

    def _overlay_visible(self) -> bool:
        """Whether the busy overlay should be painted this frame.

        Gated on an empty screen stack so the ring only appears in the "black screen" gaps a
        device operation opens between screens (a menu navigation before the tool's first
        prompt, say) and never buries a dialog the user is meant to be reading. It also stays
        unpainted through the overlay's initial hold (:attr:`BusyOverlay.brightness` is 0), so
        an operation that finishes within the hold shows nothing and never flashes.
        """
        return self._overlay is not None and not self._stack and self._overlay.brightness > 0

    def _render_overlay(self) -> ANSI:
        """Render the busy overlay's ring (only when :meth:`_overlay_visible`)."""
        if self._overlay is None:
            return ANSI("")
        return ANSI(self._overlay.render())

    # --- input ---------------------------------------------------------------

    def _key_bindings(self) -> KeyBindings:
        """Build the global key bindings that dispatch normalized actions to the top."""
        kb = KeyBindings()

        def bind(key: Any, action: str) -> None:
            @kb.add(key)
            def _(event: Any, action: str = action) -> None:  # noqa: ANN401
                self._dispatch(action)

        for key, action in _KEY_ACTIONS.items():
            bind(key, action)

        @kb.add(Keys.ControlC)
        def _quit(event: Any) -> None:  # noqa: ANN401
            if self._app is not None:
                self._app.exit()

        @kb.add(Keys.Any)
        def _typed(event: Any) -> None:  # noqa: ANN401
            data = event.data
            if data and len(data) == 1 and data.isprintable():
                self._dispatch("text", data)

        return kb

    def _dispatch(self, action: str, data: str = "") -> None:
        """Forward an action to the top screen and repaint."""
        if self.top is not None:
            self.top.handle(action, data)
        self.invalidate()


__all__ = [
    "TuiSession",
    "Choice",
    "Separator",
    "SelectScreen",
    "ReorderScreen",
    "ScrollScreen",
]
