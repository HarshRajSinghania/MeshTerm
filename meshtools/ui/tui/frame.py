"""Composition helpers: turn a screen + terminal size into a bounded, framed ANSI view.

The session owns input and the screen stack; this module owns *layout* — slicing a screen's
body to a scroll viewport, wrapping it in a titled panel, and assembling the persistent
header and footer so the whole view fits the terminal exactly (never overflowing it).
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from .render import render_lines
from .screen import Screen, ScrollScreen


def _visible_slice(screen: Screen, lines: list[str], viewport: int) -> tuple[list[str], bool, bool]:
    """Clamp the screen's scroll and return the visible lines plus clip flags.

    Keeps the screen's cursor line in view (for select/text screens) and clamps the scroll
    offset to the content, then pads the slice to exactly ``viewport`` rows so the panel
    always fills its allotted height.

    Args:
        screen: The screen being rendered (its ``scroll`` is adjusted in place).
        lines: The screen's full, unclipped body lines.
        viewport: The visible body height in rows.

    Returns:
        A tuple of (padded visible lines, more-above, more-below).
    """
    total = len(lines)
    if isinstance(screen, ScrollScreen):
        screen.note_viewport(viewport)
    cursor = screen.cursor_line()
    if cursor is not None:
        if cursor < screen.scroll:
            screen.scroll = cursor
        elif cursor >= screen.scroll + viewport:
            screen.scroll = cursor - viewport + 1
    screen.scroll = max(0, min(screen.scroll, max(0, total - viewport)))

    visible = lines[screen.scroll : screen.scroll + viewport]
    more_above = screen.scroll > 0
    more_below = screen.scroll + viewport < total
    visible = visible + [""] * (viewport - len(visible))
    return visible, more_above, more_below


def _panel(screen: Screen, inner_w: int, viewport: int, active: bool) -> Panel:
    """Render a screen's body into a titled, scroll-aware panel.

    Args:
        screen: The screen to frame.
        inner_w: Inner content width in columns.
        viewport: Visible body height in rows.
        active: Whether this is the focused (top) screen, brightening its border.

    Returns:
        A Rich :class:`Panel` of exactly ``viewport + 2`` rows.
    """
    body_lines = screen.render_body(inner_w)
    visible, more_above, more_below = _visible_slice(screen, body_lines, viewport)
    body = Text.from_ansi("\n".join(visible))
    subtitle = None
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
        subtitle = f"[muted]{arrow} more[/muted]"
    return Panel(
        body,
        title=f"[accent]{screen.title}[/accent]" if screen.title else None,
        subtitle=subtitle,
        border_style="accent" if active else "muted",
        padding=(0, 1),
    )


def compose_base(
    header: RenderableType,
    base: Screen,
    footer_hint: str,
    cols: int,
    rows: int,
) -> str:
    """Compose the full-screen ANSI view: header, the base screen's panel, and a footer.

    Args:
        header: The persistent header renderable (banner + live status).
        base: The screen filling the background (the deepest non-floating layer).
        footer_hint: The active screen's key hint, shown at the very bottom.
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string of exactly ``rows`` lines, each within ``cols`` columns.
    """
    header_lines = render_lines(header, cols)
    header_h = len(header_lines)
    viewport = max(1, rows - header_h - 1 - 2)  # minus footer(1) and panel border(2)
    panel = _panel(base, cols - 4, viewport, active=True)
    footer = Text.from_markup(f"[muted]{footer_hint}[/muted]")
    group = Group(header, panel, footer)
    lines = render_lines(group, cols)
    # Guarantee we never exceed the terminal height (pt would otherwise clip unpredictably).
    if len(lines) > rows:
        lines = lines[:rows]
    else:
        lines += [""] * (rows - len(lines))
    return "\n".join(lines)


def compose_dialog(screen: Screen, cols: int, rows: int) -> str:
    """Compose a centered dialog panel for a floating screen, bounded to the terminal.

    Args:
        screen: The floating (top) screen.
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string sized to the dialog's content (never larger than the terminal).
    """
    max_w = min(cols - 6, 100)
    max_h = max(3, rows - 6)
    body_lines = screen.render_body(max_w - 4)
    viewport = min(max_h, max(1, len(body_lines)))
    visible, more_above, more_below = _visible_slice(screen, body_lines, viewport)
    body = Text.from_ansi("\n".join(visible))
    subtitle = f"[muted]{screen.footer_hint}[/muted]"
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
        subtitle = f"[muted]{arrow} · {screen.footer_hint}[/muted]"
    panel = Panel(
        body,
        title=f"[accent]{screen.title}[/accent]" if screen.title else None,
        subtitle=subtitle,
        border_style="accent",
        padding=(0, 1),
        width=max_w,
    )
    return render_to_ansi_dialog(panel, max_w)


def render_to_ansi_dialog(panel: Panel, width: int) -> str:
    """Render a dialog panel to ANSI at a fixed width (thin wrapper for clarity)."""
    from .render import render_to_ansi

    return render_to_ansi(panel, width)
