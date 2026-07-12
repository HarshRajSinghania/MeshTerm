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

from ..theme import hint_style, title_style
from .glow import apply_corner_glow
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
        start = scroll
        if scroll >= total - viewport and total > cap:
            # Scrolled fully to the bottom: the pinned header's reserved row must not
            # cost the *last* body line (a chat's input row would vanish exactly when
            # the transcript fills the screen). Slide the window down one instead —
            # the dropped row is at the top, right under the pin, where it's stale.
            start = min(scroll + 1, total - cap)
        visible = [sticky] + lines[start : start + cap]
        more_below = start + cap < total
        visible = visible + [""] * (viewport - len(visible))
        return visible, True, more_below

    visible = lines[scroll : scroll + viewport]
    more_above = scroll > 0
    more_below = scroll + viewport < total
    visible = visible + [""] * (viewport - len(visible))
    return visible, more_above, more_below


def _breathing_room(body_len: int, budget: int) -> int:
    """Rows of blank vertical padding a content-sized box should draw inside its borders.

    Every popup box (:func:`compose_dialog` floats and the :func:`compose_startup` splash
    boxes alike) aerates its layout with a blank row above and below the body whenever that
    doesn't cost visible content; on a terminal too short for both, the content wins and the
    box sits flush. Full-screen panels (:func:`compose_base`) deliberately skip this — they
    hold dense content and would only waste rows.

    Args:
        body_len: The body's full height in rows.
        budget: Rows the box may spend between its borders — on body and padding alike.

    Returns:
        The vertical padding (1 or 0) to pass to the panel.
    """
    return 1 if body_len + 2 <= budget else 0


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
    # Record the viewport *before* the body renders, so a screen that windows a
    # list inside itself (see :class:`~meshterm.ui.tui.screen.ListWindow`) can size
    # its chrome to the frame it is about to be sliced into.
    screen.note_viewport(viewport)
    body_lines = screen.render_body(inner_w)
    visible, more_above, more_below = _visible_slice(screen, body_lines, viewport)
    body = Text.from_ansi("\n".join(visible))
    border = "accent" if active else "muted"
    subtitle = None
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
        subtitle = f"[{hint_style(border)}]{arrow} more[/]"
    return Panel(
        body,
        title=f"[{title_style(border)}]{screen.title}[/]" if screen.title else None,
        subtitle=subtitle,
        border_style=border,
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
    # The glow pass lights the outer frame *and* any tool panels nested in the body.
    lines = header_lines + apply_corner_glow(render_lines(Group(panel, footer), cols))
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
    # from just under the banner and only *it* grows or shrinks; the logo stays put. The
    # anchor sits at 2/5 of the terminal rather than the midline so the block reads centered
    # once the device list has populated (the box only ever grows downward); the brief small
    # states sit slightly high, the conventional optical placement for dialogs.
    top = max(0, rows * 2 // 5 - banner_h - gap)
    footnote_h = 2 if screen.footnote else 0  # a blank spacer line plus the note itself
    # Rows left for the box below the fixed banner block: the panel border is 2 rows.
    below = rows - top - banner_h - gap
    body_lines = screen.render_body(inner_w)
    budget = below - 2 - footnote_h
    vpad = _breathing_room(len(body_lines), budget)
    viewport = max(1, min(len(body_lines), budget - 2 * vpad))
    visible, more_above, more_below = _visible_slice(screen, body_lines, viewport)

    body = Text.from_ansi("\n".join(visible))
    hint = hint_style("accent")
    subtitle = f"[{hint}]{screen.footer_hint}[/]"
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
        subtitle = f"[{hint}]{arrow} · {screen.footer_hint}[/]"
    panel = Panel(
        body,
        title=f"[{title_style('accent')}]{screen.title}[/]" if screen.title else None,
        subtitle=subtitle,
        border_style="accent",
        padding=(vpad, 1),
        width=inner_w + 4,
    )
    # Glow before centering, while the box still starts at column 0 of its own lines.
    panel_lines = _center(apply_corner_glow(render_lines(panel, inner_w + 4)), cols)

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


def _dialog_layout(screen: Screen, cols: int, rows: int) -> tuple[int, int, int, list[str]]:
    """Size a floating dialog: ``(outer_width, vpad, viewport, body_lines)``."""
    # Most dialogs stretch to a generous cap; a screen may instead request a natural width
    # (a short confirm sized to its content), still bounded to the terminal.
    cap = min(cols - 6, 100)
    natural = getattr(screen, "dialog_width", None)
    max_w = cap if natural is None else max(24, min(cap, natural))
    # Rows the box may spend between its borders — on body lines and breathing room alike.
    budget = max(3, rows - 6)
    # Record the budget as the provisional viewport before the body renders, so a
    # dialog that windows a list inside itself (the path composer) can size to what
    # it may spend; the slice records the real, body-sized viewport afterwards.
    screen.note_viewport(budget)
    body_lines = screen.render_body(max_w - 4)
    # A grow-only screen (the packet viewer paging between packets) sizes to the tallest
    # body it has shown, not this one, so a shorter body keeps the larger box instead of
    # re-centring smaller — the frame blank-pads the slack. The breathing room is taken
    # from that ratcheted height, so the box stays put as the body shrinks below it.
    body_h = screen.ratchet_viewport(max(1, len(body_lines)))
    vpad = _breathing_room(body_h, budget)
    viewport = min(budget - 2 * vpad, body_h)
    return max_w, vpad, viewport, body_lines


def compose_dialog(screen: Screen, cols: int, rows: int) -> str:
    """Compose a centered dialog panel for a floating screen, bounded to the terminal.

    Args:
        screen: The floating (top) screen.
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string sized to the dialog's content (never larger than the terminal).
    """
    max_w, vpad, viewport, body_lines = _dialog_layout(screen, cols, rows)
    visible, more_above, more_below = _visible_slice(screen, body_lines, viewport)
    body = Text.from_ansi("\n".join(visible))
    border = getattr(screen, "border_style", "accent")
    hint = hint_style(border)
    subtitle = f"[{hint}]{screen.footer_hint}[/]"
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
        subtitle = f"[{hint}]{arrow} · {screen.footer_hint}[/]"
    panel = Panel(
        body,
        title=f"[{title_style(border)}]{screen.title}[/]" if screen.title else None,
        subtitle=subtitle,
        border_style=border,
        padding=(vpad, 1),
        width=max_w,
    )
    return "\n".join(apply_corner_glow(render_lines(panel, max_w)))
