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
from typing import Any, Callable, Optional

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
from .progress import TuiProgress
from .prompt import AutocompleteScreen, ConfirmScreen, TextScreen, Validator
from .screen import CANCEL, Screen, ScrollScreen
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
        self.invalidate()

    def invalidate(self) -> None:
        """Request a repaint if the application is running."""
        if self._app is not None:
            self._app.invalidate()

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

    async def select(self, title: str, items: list, *, default: Any = None) -> Any:
        """Show a select screen; return the chosen value or ``None`` if cancelled."""
        result = await self.run_screen(SelectScreen(title, items, default=default))
        return None if result is CANCEL else result

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Optional[Any] = None,
        footer_hint: str = "↑↓ move · Enter select · Esc skip",
    ) -> Any:
        """Show a chromeless select splash (banner above a content-sized box).

        Like :meth:`select`, but drawn without the header/footer status bars and centered
        under ``banner`` — the startup device picker's presentation. Type-to-filter is off:
        the device list is short and fixed, so stray keys never narrow it.
        """
        screen = SelectScreen(
            title, items, default=default, footer_hint=footer_hint, filterable=False
        )
        screen.chrome = False
        screen.banner = banner
        result = await self.run_screen(screen)
        return None if result is CANCEL else result

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Optional[Any] = None,
        footer_hint: str = "Enter continue",
    ) -> None:
        """Show a chromeless message splash (banner above a boxed renderable) until dismissed."""
        screen = ScrollScreen(renderable, title=title, footer_hint=footer_hint)
        screen.chrome = False
        screen.banner = banner
        await self.run_screen(screen)

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

        await self._app.run_async(pre_run=pre_run)
        if "exc" in box:
            raise box["exc"]

    def _build_app(self) -> Application:
        """Construct the prompt_toolkit application, layout, and key bindings."""
        base_control = FormattedTextControl(self._render_base, focusable=True)
        base_window = Window(base_control, always_hide_cursor=True)
        float_window = Window(
            FormattedTextControl(self._render_float), always_hide_cursor=True
        )
        root = FloatContainer(
            content=base_window,
            floats=[
                Float(
                    ConditionalContainer(
                        float_window, filter=Condition(self._has_float)
                    )
                )
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
