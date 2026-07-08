"""The screen abstraction: one layer in the TUI's window stack.

A :class:`Screen` knows how to render its body (as Rich, converted to ANSI lines by
:mod:`~meshterm.ui.tui.render`) and how to react to normalized key *actions* dispatched by
the :class:`~meshterm.ui.tui.session.TuiSession`. Screens never touch prompt_toolkit or the
terminal directly, so they stay small, uniform, and testable.

Interactive screens (select, text, confirm) resolve an :class:`asyncio.Future` when the user
commits or cancels; the session awaits that future and then pops the screen, giving the
push/await/pop model behind ``await session.select(...)`` and friends.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, Sequence

from rich.console import RenderableType

from .render import render_lines

#: Sentinel a screen resolves with when the user presses Esc to cancel, distinct from a
#: legitimately selected ``None`` value.
CANCEL = object()


class Screen:
    """Base class for every TUI layer.

    Attributes:
        title: Heading shown at the top of the screen/dialog.
        footer_hint: One-line key hint shown in the footer.
        scroll: Current vertical scroll offset into the body's rendered lines.
        future: Resolved with the screen's result (or :data:`CANCEL`) when it commits.
        floating: Whether the session should draw this screen as a centered dialog over
            the dimmed screen beneath it (deeper layers float; the base does not).
        chrome: Whether, as the base screen, this layer is wrapped in the session's
            persistent header/footer frame. A startup splash sets this ``False`` so the
            session instead centers it under the :attr:`banner` with no status bars.
        banner: Block-glyph art (one string per row) drawn, centered, above a chromeless
            base screen. Ignored while ``chrome`` is ``True``.
    """

    title: str = ""
    footer_hint: str = "Esc back"
    floating: bool = True
    chrome: bool = True
    banner: Optional[Sequence[str]] = None

    def __init__(self) -> None:
        """Initialize scroll state and the (later-assigned) result future."""
        self.scroll = 0
        self.future: Optional[asyncio.Future] = None

    # --- rendering -----------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Return the body's ANSI lines at ``width`` columns (unclipped).

        Args:
            width: Inner content width in columns.

        Returns:
            One ANSI string per body line; the session slices these to the viewport.
        """
        raise NotImplementedError

    def cursor_line(self) -> Optional[int]:
        """Return a body line that must stay visible, or ``None`` for free scrolling.

        Selection/text screens return the active row so the session can keep it in view;
        plain scroll screens return ``None``.
        """
        return None

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """React to a normalized key action from the session.

        Args:
            action: One of ``up``, ``down``, ``pageup``, ``pagedown``, ``home``, ``end``,
                ``left``, ``right``, ``shift_up``, ``shift_down``, ``shift_left``,
                ``shift_right``, ``enter``, ``escape``, ``backspace``, ``delete``,
                ``space``, ``tab``, or ``text`` (with ``data`` set to the character).
            data: The typed character when ``action`` is ``text``.
        """
        if action == "escape":
            self.resolve(CANCEL)

    def resolve(self, value: Any) -> None:
        """Resolve this screen's future, committing ``value`` (or :data:`CANCEL`).

        Args:
            value: The result to hand back to the awaiting caller.
        """
        if self.future is not None and not self.future.done():
            self.future.set_result(value)

    # --- scrolling helpers (shared) ------------------------------------------

    def scroll_by(self, delta: int, total: int, viewport: int) -> None:
        """Adjust :attr:`scroll` by ``delta`` lines, clamped to the content.

        Args:
            delta: Lines to move (negative scrolls up).
            total: Total body line count.
            viewport: Visible body height in lines.
        """
        self.scroll = _clamp_scroll(self.scroll + delta, total, viewport)


def _clamp_scroll(scroll: int, total: int, viewport: int) -> int:
    """Clamp a scroll offset to the valid range for the content and viewport.

    Args:
        scroll: Proposed offset.
        total: Total line count.
        viewport: Visible height in lines.

    Returns:
        The offset clamped to ``0 .. max(0, total - viewport)``.
    """
    return max(0, min(scroll, max(0, total - viewport)))


class ScrollScreen(Screen):
    """A read-only screen that shows a Rich renderable in a scrollable viewport.

    Used for tool result windows and any long list. Content taller than the viewport
    scrolls with the arrow keys / PageUp / PageDown / Home / End; Esc dismisses it.
    """

    def __init__(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        footer_hint: str = "↑↓ PgUp/PgDn scroll · Esc back",
        floating: bool = True,
    ) -> None:
        """Wrap a renderable in a dismissable, scrollable screen.

        Args:
            renderable: The Rich content to display.
            title: Heading for the screen/dialog.
            footer_hint: Footer key hint.
            floating: Whether to draw as a centered dialog over the parent.
        """
        super().__init__()
        self._renderable = renderable
        self.title = title
        self.footer_hint = footer_hint
        self.floating = floating
        # The last viewport height the session rendered with, so PageUp/PageDown and the
        # End key can move by a full page without the session having to pass it in.
        self._viewport = 1
        self._total = 1

    def render_body(self, width: int) -> list[str]:
        """Render the wrapped content to ANSI lines and remember the total count."""
        lines = render_lines(self._renderable, width)
        self._total = max(1, len(lines))
        return lines

    def note_viewport(self, viewport: int) -> None:
        """Record the viewport height the session is about to render with.

        Args:
            viewport: Visible body height in lines.
        """
        self._viewport = max(1, viewport)

    def handle(self, action: str, data: str = "") -> None:
        """Scroll the viewport or dismiss the screen."""
        page = max(1, self._viewport - 1)
        if action == "up":
            self.scroll_by(-1, self._total, self._viewport)
        elif action == "down":
            self.scroll_by(1, self._total, self._viewport)
        elif action == "pageup":
            self.scroll_by(-page, self._total, self._viewport)
        elif action in ("pagedown", "space"):
            self.scroll_by(page, self._total, self._viewport)
        elif action == "home":
            self.scroll = 0
        elif action == "end":
            self.scroll = max(0, self._total - self._viewport)
        elif action in ("escape", "enter"):
            self.resolve(None)
