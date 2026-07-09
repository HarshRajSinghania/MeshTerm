"""Composition helpers: turn a screen + terminal size into a bounded, framed ANSI view.

The session owns input and the screen stack; this module owns *layout* — slicing a screen's
body to a scroll viewport, wrapping it in a titled panel, and assembling the persistent
header and footer so the whole view fits the terminal exactly (never overflowing it).
"""

from __future__ import annotations

from typing import Sequence

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from .render import render_lines
from .screen import Screen


def _visible_slice(screen: Screen, lines: list[str], viewport: int) -> tuple[list[str], bool, bool]:
    """Clamp the screen's scroll and return the visible lines plus clip flags.

    Keeps the screen's cursor line in view (for select/text screens) and clamps the scroll
    offset to the content, then pads the slice to exactly ``viewport`` rows so the panel
    always fills its allotted height. When the screen offers a sticky header (a grouped list's
    section heading that has scrolled off), it is pinned to the top row: that reserves one row,
    so the cursor is kept within the remaining ``viewport - 1`` and the bottom clamp is relaxed
    by one so the final content row can still reach the last visible line.

    Args:
        screen: The screen being rendered (its ``scroll`` is adjusted in place).
        lines: The screen's full, unclipped body lines.
        viewport: The visible body height in rows.

    Returns:
        A tuple of (padded visible lines, more-above, more-below).
    """
    total = len(lines)
    # Record the body height and viewport so the screen's shared scroll helpers can page by a
    # screenful and clamp to the content (see :meth:`Screen.note_metrics`).
    screen.note_metrics(total, viewport)
    cursor = screen.cursor_line()

    scroll = screen.scroll
    if cursor is not None:
        if cursor < scroll:
            scroll = cursor
        elif cursor >= scroll + viewport:
            scroll = cursor - viewport + 1
    scroll = max(0, min(scroll, max(0, total - viewport)))

    # A pinned section heading takes the top row, leaving one fewer for content; nudge the
    # scroll down if the cursor would fall in that reserved row, then re-check the heading
    # (crossing a section boundary can change which one is pinned, or drop it entirely).
    sticky = screen.sticky_header(scroll) if scroll > 0 else None
    if sticky is not None:
        cap = max(1, viewport - 1)
        if cursor is not None and cursor >= scroll + cap:
            scroll = min(cursor - cap + 1, max(0, total - cap))
            sticky = screen.sticky_header(scroll) if scroll > 0 else None

    screen.scroll = scroll

    if sticky is not None:
        cap = max(1, viewport - 1)
        visible = [sticky] + lines[scroll : scroll + cap]
        more_below = scroll + cap < total
        visible = visible + [""] * (viewport - len(visible))
        return visible, True, more_below

    visible = lines[scroll : scroll + viewport]
    more_above = scroll > 0
    more_below = scroll + viewport < total
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
    # The header is a single status line: crop it to one row so a narrow terminal never
    # wraps it onto a second line (which would push the panel down and misreport its height).
    header_lines = render_lines(header, cols, no_wrap=True)
    header_h = len(header_lines)
    viewport = max(1, rows - header_h - 1 - 2)  # minus footer(1) and panel border(2)
    panel = _panel(base, cols - 4, viewport, active=True)
    footer = Text.from_markup(f"[muted]{footer_hint}[/muted]")
    lines = header_lines + render_lines(Group(panel, footer), cols)
    # Guarantee we never exceed the terminal height (pt would otherwise clip unpredictably).
    if len(lines) > rows:
        lines = lines[:rows]
    else:
        lines += [""] * (rows - len(lines))
    return "\n".join(lines)


def _ansi_width(line: str) -> int:
    """Return the display width of an ANSI line, ignoring its trailing padding."""
    return cell_len(Text.from_ansi(line).plain.rstrip())


def _center(lines: list[str], cols: int) -> list[str]:
    """Left-pad each ANSI line so the block is horizontally centered within ``cols``."""
    out: list[str] = []
    for line in lines:
        pad = max(0, (cols - cell_len(Text.from_ansi(line).plain)) // 2)
        out.append(" " * pad + line)
    return out


def _banner_lines(banner: Sequence[str], cols: int) -> list[str]:
    """Center the wordmark rows (already-coloured ANSI) as one left-aligned block.

    Each row is padded to the block's widest display width first, so the shared left margin
    keeps the art internally aligned rather than centering every row on its own axis.
    """
    if not banner:
        return []
    widths = [cell_len(Text.from_ansi(row).plain) for row in banner]
    width = max(widths)
    padded = [row + " " * (width - w) for row, w in zip(banner, widths)]
    return _center(padded, cols)


def compose_startup(screen: Screen, cols: int, rows: int) -> str:
    """Compose a chromeless splash: a centered wordmark above a content-sized panel.

    Unlike :func:`compose_base`, this draws no header or footer status bars and does not
    stretch the panel across the terminal — the box is sized to its own content (the device
    list) and the whole block is centered on screen. Used for the startup device picker.

    Args:
        screen: The chromeless base screen (its ``banner`` supplies the wordmark).
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string of exactly ``rows`` lines, each within ``cols`` columns.
    """
    banner = _banner_lines(screen.banner or [], cols)
    banner_h = len(banner)
    gap = 1 if banner else 0  # the blank line under the banner

    # Size the box to its widest real row (probe at a generous width, then measure), never
    # wider than the terminal and never narrower than the title/hint it must show.
    probe = max(10, min(cols - 6, 100))
    measured = max((_ansi_width(line) for line in screen.render_body(probe)), default=10)
    inner_w = max(measured, cell_len(screen.title), cell_len(screen.footer_hint))
    inner_w = max(10, min(inner_w, cols - 6))

    # Pin the banner to a fixed vertical anchor that depends only on the terminal height and
    # the banner's own (constant) height, so the wordmark never moves as the box below it
    # swaps contents between splash states (device list → spinner → message). The box hangs
    # from just under the banner and only *it* grows or shrinks; the logo stays put.
    top = max(0, rows // 2 - banner_h - gap)
    footnote_h = 2 if screen.footnote else 0  # a blank spacer line plus the note itself
    # Rows left for the box below the fixed banner block: the panel border is 2 rows.
    below = rows - top - banner_h - gap
    body_lines = screen.render_body(inner_w)
    viewport = max(1, min(len(body_lines), below - 2 - footnote_h))
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
        width=inner_w + 4,
    )
    panel_lines = _center(render_lines(panel, inner_w + 4), cols)

    # A small muted line (e.g. a copyright notice) sits a blank row below the box.
    footnote_lines: list[str] = []
    if screen.footnote:
        note = Text.from_markup(f"[muted]{screen.footnote}[/muted]")
        footnote_lines = [""] + _center(render_lines(note, cell_len(screen.footnote)), cols)

    block = banner + ([""] * gap) + panel_lines + footnote_lines
    lines = [""] * top + block
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
