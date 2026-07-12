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
from rich.text import Text

from .render import render_lines
from .spinner import Spinner

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
        grow_only: Whether a floating dialog may only ever grow. Its box is sized to the
            tallest body it has shown, not the current one, so a screen whose body height
            swings as its content changes (the packet viewer paging between packets) keeps
            a steady, centred box — blank-padded when the current body is shorter — instead
            of resizing on every page.
        chrome: Whether, as the base screen, this layer is wrapped in the session's
            persistent header/footer frame. A startup splash sets this ``False`` so the
            session instead centers it under the :attr:`banner` with no status bars.
        banner: Block-glyph art (one string per row) drawn, centered, above a chromeless
            base screen. Ignored while ``chrome`` is ``True``.
        footnote: A short line drawn muted and centered *below* a chromeless base screen's
            box (e.g. a copyright notice). Ignored while ``chrome`` is ``True``.
    """

    title: str = ""
    footer_hint: str = "Esc back"
    floating: bool = True
    grow_only: bool = False
    chrome: bool = True
    banner: Optional[Sequence[str]] = None
    footnote: Optional[str] = None

    def __init__(self) -> None:
        """Initialize scroll state and the (later-assigned) result future."""
        self.scroll = 0
        self.future: Optional[asyncio.Future] = None
        # (body-line index, rendered ANSI line) for each header eligible to be pinned to the
        # top row once it scrolls off. A screen that wants sticky headers rebuilds this list
        # while rendering its body (see :meth:`sticky_header`); the default is no headers.
        self._sticky_headers: list[tuple[int, str]] = []
        # The last render's body height and viewport, recorded by the frame (:meth:`note_metrics`)
        # so the shared scroll helpers can page by a screenful of the *current* terminal and
        # clamp to the content without every caller threading the sizes through.
        self._scroll_total = 1
        self._scroll_viewport = 1
        # A grow-only screen's high-water body height: the tallest body it has rendered
        # into a dialog, so its box can be held at that size once reached (see
        # :meth:`ratchet_viewport`). Zero until the first paint; irrelevant while not
        # :attr:`grow_only`.
        self._viewport_floor = 0

    # --- rendering -----------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Return the body's ANSI lines at ``width`` columns (unclipped).

        Args:
            width: Inner content width in columns.

        Returns:
            One ANSI string per body line; the session slices these to the viewport.
        """
        raise NotImplementedError

    def ratchet_viewport(self, body_h: int) -> int:
        """The body height a :attr:`grow_only` dialog is sized to: its running maximum.

        The frame calls this each paint with the current body's height. A grow-only
        screen returns the tallest height it has yet shown — so its box never shrinks,
        and the frame blank-pads a now-shorter body to fill it — while an ordinary
        screen returns the height unchanged and is sized to each body as it comes.
        """
        if not self.grow_only:
            return body_h
        self._viewport_floor = max(self._viewport_floor, body_h)
        return self._viewport_floor

    def cursor_line(self) -> Optional[int]:
        """Return a body line that must stay visible, or ``None`` for free scrolling.

        Selection/text screens return the active row so the session can keep it in view;
        plain scroll screens return ``None``.
        """
        return None

    def consume_edge_scrub(self) -> int:
        """Right-edge columns the session should force-repaint on the next paint (0 = none).

        The default is 0 — no screen needs this. A screen whose body can emit glyphs the
        terminal renders at an unexpected width (the map's braille) overrides this to have the
        session redraw just the smeared edge cells, which prompt_toolkit's differential paint
        would otherwise never rewrite (see :class:`~meshterm.ui.map_screen.MapScreen`).
        """
        return 0

    def sticky_header(self, scroll: int) -> Optional[str]:
        """An already-rendered body line to pin to the top row once ``scroll`` moves past it.

        Lets a grouped list keep its current section heading in view after the heading itself
        has scrolled off — a select screen's ``Channels``/``Direct`` divider, or the chat
        transcript's ``── Wed Jul 8 ──`` day divider. ``scroll`` is the offset the body is
        about to be sliced at; the frame draws the returned line as the top row.

        The shared rule: among the headers a screen recorded in :attr:`_sticky_headers` while
        rendering, find the last one at or above ``scroll`` (the one *governing* the top visible
        row) and pin it — unless it is itself the top visible row (nothing to duplicate) or there
        is none above. A screen opts in simply by populating :attr:`_sticky_headers`; the empty
        default means no pinning.
        """
        governing: Optional[str] = None
        governing_at = -1
        for idx, line in self._sticky_headers:
            if idx <= scroll:
                governing, governing_at = line, idx
            else:
                break  # headers are recorded in body order; nothing past here can govern
        if governing is None or governing_at == scroll:
            return None  # no header above, or it's already the top visible row
        return governing

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """React to a normalized key action from the session.

        Args:
            action: One of ``up``, ``down``, ``pageup``, ``pagedown``, ``home``, ``end``,
                ``ctrl_home``, ``ctrl_end``, ``ctrl_pageup``, ``ctrl_pagedown``, ``left``,
                ``right``, ``ctrl_left``, ``ctrl_right``, ``shift_up``, ``shift_down``,
                ``shift_left``, ``shift_right``, ``enter``, ``escape``, ``backspace``,
                ``delete``, ``space``, ``tab``, or ``text`` (with ``data`` set to the character).
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

    def note_metrics(self, total: int, viewport: int) -> None:
        """Record the last render's body height and viewport (the frame calls this each paint).

        The shared scroll/section jumps below size themselves from these, so a PageDown moves
        by a screenful of the current terminal rather than a fixed constant, and the top/bottom
        clamps track the real content height without the caller passing the sizes in.
        """
        self._scroll_total = max(1, total)
        self._scroll_viewport = max(1, viewport)

    def note_viewport(self, viewport: int) -> None:
        """Record just the viewport height (for callers that don't know the body total)."""
        self._scroll_viewport = max(1, viewport)

    @property
    def _page_step(self) -> int:
        """Lines a PageUp/PageDown moves: a screenful, bar one row kept for continuity."""
        return max(1, self._scroll_viewport - 1)

    def scroll_by(self, delta: int, total: int, viewport: int) -> None:
        """Adjust :attr:`scroll` by ``delta`` lines, clamped to the given content bounds."""
        self.scroll = _clamp_scroll(self.scroll + delta, total, viewport)

    def scroll_lines(self, delta: int) -> None:
        """Scroll by ``delta`` lines, clamped to the last-recorded content bounds."""
        self.scroll = _clamp_scroll(
            self.scroll + delta, self._scroll_total, self._scroll_viewport
        )

    def scroll_pages(self, pages: int) -> None:
        """Scroll by ``pages`` screenfuls (negative scrolls up)."""
        self.scroll_lines(pages * self._page_step)

    def scroll_to_top(self) -> None:
        """Jump the view to the first line."""
        self.scroll = 0

    def scroll_to_bottom(self) -> None:
        """Jump the view to the last screenful."""
        self.scroll = max(0, self._scroll_total - self._scroll_viewport)

    def scroll_to_section_start(self) -> None:
        """Move to the top of the section holding the top visible line.

        Sections are delimited by the recorded :attr:`_sticky_headers` (a select list's group
        dividers, the chat transcript's day dividers). If that heading is already the top line,
        move to the *previous* section instead — so repeated presses walk up section by section,
        the way a text editor's paragraph jump does. Falls back to the very top with no sections.
        """
        offsets = [idx for idx, _ in self._sticky_headers]
        governing = max((o for o in offsets if o <= self.scroll), default=None)
        if governing is None:
            target = 0
        elif governing < self.scroll:
            target = governing  # up to the top of the section we're inside
        else:
            target = max((o for o in offsets if o < self.scroll), default=0)
        self.scroll = _clamp_scroll(target, self._scroll_total, self._scroll_viewport)

    def scroll_to_next_section(self) -> None:
        """Move to the start of the next section below the top visible line (else the bottom)."""
        offsets = [idx for idx, _ in self._sticky_headers]
        nxt = min((o for o in offsets if o > self.scroll), default=None)
        target = nxt if nxt is not None else self._scroll_total
        self.scroll = _clamp_scroll(target, self._scroll_total, self._scroll_viewport)


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

    Used for tool result windows and any long list. Content taller than the viewport scrolls
    with the arrows / PageUp / PageDown (a screenful) / Home / End (or Ctrl+Home / Ctrl+End);
    Esc dismisses it. Result windows carry no sections, so Ctrl+PageUp/PageDown just reach the
    top/bottom.
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

    def render_body(self, width: int) -> list[str]:
        """Render the wrapped content to ANSI lines and remember the total count."""
        lines = render_lines(self._renderable, width)
        self._scroll_total = max(1, len(lines))
        return lines

    def handle(self, action: str, data: str = "") -> None:
        """Scroll the viewport (by line, page, or to an edge) or dismiss the screen."""
        if action == "up":
            self.scroll_lines(-1)
        elif action == "down":
            self.scroll_lines(1)
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self.scroll_to_bottom()
        elif action == "ctrl_pageup":
            self.scroll_to_section_start()
        elif action == "ctrl_pagedown":
            self.scroll_to_next_section()
        elif action in ("escape", "enter"):
            self.resolve(None)


class BusyScreen(Screen):
    """A non-interactive splash body: an animated spinner beside a message.

    Used to keep the startup splash on screen — its wordmark and box unchanged — while a
    short async task runs (e.g. smoke-testing a chosen companion device). It set as a
    chromeless base so it redraws the same centered-under-the-wordmark splash, only swapping
    the box's contents. It resolves nothing and ignores every key; the session pops it when
    the awaited task completes. The animation itself is the reusable
    :class:`~meshterm.ui.tui.spinner.Spinner`.
    """

    footer_hint = "working…"
    floating = False

    def __init__(self, message: str, *, title: str = "") -> None:
        """Start a busy splash showing ``message`` under an optional ``title``."""
        super().__init__()
        self.title = title
        self._message = message
        self._spinner = Spinner()

    def tick(self) -> None:
        """Advance the spinner to its next frame (driven by the session's animation timer)."""
        self._spinner.tick()

    def render_body(self, width: int) -> list[str]:
        """Render the current spinner frame followed by the message."""
        line = self._spinner.text()
        line.append("  ")
        line.append(self._message)
        return render_lines(line, width)

    def handle(self, action: str, data: str = "") -> None:
        """Swallow all keys: the splash dismisses itself when the task finishes."""
        return
