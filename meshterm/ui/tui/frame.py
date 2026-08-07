"""Composition helpers: turn a screen + terminal size into a bounded, framed ANSI view.

The session owns input and the screen stack; this module owns *layout* — slicing a screen's
body to a scroll viewport, wrapping it in a titled panel, and assembling the persistent
header and footer so the whole view fits the terminal exactly (never overflowing it).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Optional, Sequence

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from ...platforms import Platform, get_platform, on_platform
from ..theme import hint_style, title_style
from .glow import apply_corner_glow
from .render import render_lines
from .screen import Screen


#: How many times :func:`_visible_slice` re-settles the scroll against the rows a screen
#: pins above it. Each pin costs a content row, so gaining one can push the highlighted row
#: back out of view and call for another nudge; two or three passes always reach a fixed
#: point at the pin counts screens actually use (a column header and a section heading).
_PIN_SETTLE_PASSES = 3


def _window_start(scroll: int, total: int, viewport: int, pinned: int) -> int:
    """The body line the visible window starts at, given ``pinned`` reserved top rows.

    Normally the scroll offset itself. Scrolled fully to the bottom, the reserved rows must
    not cost the *last* body lines (a chat's input row would vanish exactly when the
    transcript fills the screen), so the window slides down by as many rows as are pinned —
    the lines it drops are at the top, right under the pins, where they are stale.
    """
    cap = max(1, viewport - pinned)
    if scroll >= total - viewport and total > cap:
        return min(scroll + pinned, total - cap)
    return scroll


def _visible_slice(screen: Screen, lines: list[str], viewport: int) -> tuple[list[str], bool, bool]:
    """Clamp the screen's scroll and return the visible lines plus clip flags.

    Keeps the screen's cursor line in view (for select/text screens) and clamps the scroll
    offset to the content, then pads the slice to exactly ``viewport`` rows so the panel
    always fills its allotted height. Any sticky rows the screen offers (a grouped list's
    section heading that has scrolled off, over a table's column header — see
    :meth:`Screen.sticky_rows`) are pinned to the top rows: each reserves one row, so the
    cursor is kept within the remaining ``viewport - pinned`` and the bottom clamp is relaxed
    by as many so the final content row can still reach the last visible line.

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

    # Each pinned row takes a top row, leaving that many fewer for content: nudge the scroll
    # down if the cursor would fall in the reserved rows, then re-ask — moving can change
    # which heading governs (crossing a section boundary pins another, or none), and a pin
    # gained that way reserves one more row, which can call for a further nudge. Settles
    # within _PIN_SETTLE_PASSES, since a screen pins at most a column header and a heading.
    pinned = screen.sticky_rows(scroll) if scroll > 0 else []
    for _ in range(_PIN_SETTLE_PASSES):
        cap = max(1, viewport - len(pinned))
        if not pinned or cursor is None or cursor < scroll + cap:
            break
        scroll = min(cursor - cap + 1, max(0, total - cap))
        pinned = screen.sticky_rows(scroll) if scroll > 0 else []

    screen.scroll = scroll

    if pinned:
        start = _window_start(scroll, total, viewport, len(pinned))
        if start != scroll:
            # The window slid down off the scroll offset, so the pins must describe the row
            # it now *starts* at — otherwise a section heading among the dropped lines would
            # simply vanish instead of being pinned. Re-ask, then re-settle the start against
            # however many rows that reserves (never unpinning: the slide depends on it).
            pinned = screen.sticky_rows(start) or pinned
            start = _window_start(scroll, total, viewport, len(pinned))
        cap = max(1, viewport - len(pinned))
        visible = pinned + lines[start : start + cap]
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


def _panel_box(
    title: str, visible: list[str], more_above: bool, more_below: bool, border: str
) -> Panel:
    """Wrap already-sliced body lines in a titled, scroll-aware panel.

    Args:
        title: The screen's heading (empty for none).
        visible: The viewport's ANSI lines, already sliced and padded to height.
        more_above: Whether content continues above the slice.
        more_below: Whether content continues below the slice.
        border: Border style name (``"accent"`` for the focused screen).

    Returns:
        A Rich :class:`Panel` of exactly ``len(visible) + 2`` rows.
    """
    body = Text.from_ansi("\n".join(visible))
    subtitle = None
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
        subtitle = f"[{hint_style(border)}]{arrow} more[/]"
    return Panel(
        body,
        title=f"[{title_style(border)}]{title}[/]" if title else None,
        subtitle=subtitle,
        border_style=border,
        padding=(0, 1),
    )


#: The last framed base composition: ``(content key, rendered lines)``. Re-parsing the
#: sliced body (``Text.from_ansi``) and re-rendering it through the Panel is the priciest
#: part of a repaint (~8 ms on a full frame), and the idle tick recomposes an unchanged
#: screen every second — only the header above it moves. One slot suffices: there is only
#: ever one base screen per paint, and any content change (a keystroke, a scroll, new
#: rows) simply misses and re-renders.
_BASE_BOX_CACHE: Optional[tuple[tuple, list[str]]] = None


def _title_bar(screen: Screen, cols: int, more_above: bool, more_below: bool) -> Text:
    """The borderless frame's one-row title bar: a centered title on a bold rule, clip arrows.

    The Panel border's whole vocabulary — where you are (title) and whether the list
    continues (the ``↑↓ more`` subtitle) — compressed into a single row so the body wins
    back three rows and four columns on the PicoCalc. Shape:
    ``──── Title ──────────── ↑↓``, echoing
    :func:`~meshterm.ui.menus.section_heading`'s heading language.

    The title is centered in the rule exactly as Rich's ``Panel`` centers its own
    ``title`` by default — the desktop's bordered frame this one stands in for. The
    rule itself takes ``border_style`` (``"accent"``) directly — the same bold weight a
    real border draws in — rather than :func:`~meshterm.ui.theme.hint_style`'s muted
    variant, which is for auxiliary text riding *alongside* a border (a footer hint, the
    subtitle's own "more" label below), not the border's own glyphs. The ``↑↓`` clip
    arrows keep that muted hint style, matching a bordered panel's own subtitle. There
    is no corner to light the way :func:`~meshterm.ui.tui.glow.apply_corner_glow` lights
    a real frame's top-left — that pass needs a truecolor blend this platform's 16-slot
    palette can't render, so the rule is uniformly bold rather than fading from one.
    """
    border = "accent"
    tail = ""
    if more_above or more_below:
        tail = ("↑" if more_above else " ") + ("↓" if more_below else " ")
    tail_span = len(tail) + 1 if tail else 0  # the space in front of the arrows
    rule_span = max(0, cols - tail_span)

    bar = Text()
    if screen.title:
        label_w = cell_len(screen.title) + 2  # a space padding it on either side
        left = max(1, (rule_span - label_w) // 2)
        right = max(1, rule_span - label_w - left)
        bar.append("─" * left, style=border)
        bar.append(" ", style=None)
        bar.append(screen.title, style=title_style(border))
        bar.append(" ", style=None)
        bar.append("─" * right, style=border)
    else:
        bar.append("─" * rule_span, style=border)
    if tail:
        bar.append(" " + tail, style=hint_style(border))
    bar.truncate(cols)
    return bar


def compose_base(
    header: RenderableType,
    base: Screen,
    footer_hint: str,
    cols: int,
    rows: int,
    footer_lane: Callable[[], RenderableType] | None = None,
) -> str:
    """Compose the full-screen ANSI view: header, the base screen's panel, and a footer.

    Args:
        header: The persistent header renderable (banner + live status).
        base: The screen filling the background (the deepest non-floating layer).
        footer_hint: The active screen's key hint, shown at the very bottom.
        cols: Terminal width.
        rows: Terminal height.
        footer_lane: Builds the footer row (the PicoCalc F-key lane) that replaces the
            hint string when the platform runs fixed F-key hints. Called *after* the body
            renders, since a lane dims its slots from the screen's scroll metrics — which
            this paint has only just recorded (see :func:`~meshterm.ui.tui.fkeys.default_lane`).

    Returns:
        An ANSI string of exactly ``rows`` lines, each within ``cols`` columns.
    """
    global _BASE_BOX_CACHE
    platform = get_platform()
    # The header is a single status line: crop it to one row so a narrow terminal never
    # wraps it onto a second line (which would push the panel down and misreport its height).
    header_lines = render_lines(header, cols, no_wrap=True)
    header_h = len(header_lines)

    def footer_row() -> RenderableType:
        """The bottom line: the platform's F-key lane, else the muted hint string."""
        if footer_lane is not None:
            return footer_lane()
        return Text.from_markup(f"[muted]{footer_hint}[/muted]")

    if platform.frame_border:
        viewport = max(1, rows - header_h - 1 - 2)  # minus footer(1) and panel border(2)
        # Record the viewport *before* the body renders, so a screen that windows a
        # list inside itself (see :class:`~meshterm.ui.tui.screen.ListWindow`) can size
        # its chrome to the frame it is about to be sliced into.
        base.note_viewport(viewport)
        body_lines = base.render_body(cols - 4)
        visible, more_above, more_below = _visible_slice(base, body_lines, viewport)
        # The panel wrap is a pure function of what's between its borders: memoize it so
        # the repaints that change nothing below the header (the 1 Hz tick) skip the
        # ANSI re-parse and Panel re-render. A dynamic footer (the F-key lane, which can
        # flip with the physical Shift key alone) has no place in the key, so it renders
        # uncached — that combination doesn't arise: the lane belongs to the borderless
        # platform below.
        key = (cols, rows, base.title, footer_hint, more_above, more_below, *visible)
        if footer_lane is None and _BASE_BOX_CACHE is not None and _BASE_BOX_CACHE[0] == key:
            body = _BASE_BOX_CACHE[1]
        else:
            panel = _panel_box(base.title, visible, more_above, more_below, "accent")
            body = render_lines(Group(panel, footer_row()), cols)
            if footer_lane is None:
                _BASE_BOX_CACHE = (key, body)
    else:
        # Borderless chrome: a one-row title bar instead of the Panel's border and
        # padding — the body wins the full terminal width and one extra row.
        viewport = max(1, rows - header_h - 1 - 1)  # minus footer(1) and title bar(1)
        base.note_viewport(viewport)
        body_lines = base.render_body(cols)
        visible, more_above, more_below = _visible_slice(base, body_lines, viewport)
        bar = _title_bar(base, cols, more_above, more_below)
        body = (
            render_lines(bar, cols, no_wrap=True)
            + visible
            + render_lines(footer_row(), cols, no_wrap=True)
        )
    # The glow pass lights the outer frame *and* any tool panels nested in the body. It only
    # ever recolours truecolor foregrounds (see glow._advance_fg), so on a platform without
    # effects — which is also a platform without truecolor — it would scan every line of
    # every frame and hand back the identical list. Skipping it outright is the same picture
    # for none of the work. Read live rather than bound at import: set_platform() runs in the
    # CLI callback, long after this module is imported (see meshterm.platforms).
    lines = header_lines + (apply_corner_glow(body) if platform.effects else body)
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

    # The logo's own left margin and width, so the footnote below can hang off its right edge
    # (the wordmark is centered as one block, so every row shares this margin).
    raw_banner = screen.banner or []
    logo_w = max((cell_len(Text.from_ansi(row).plain) for row in raw_banner), default=0)
    logo_right = max(0, (cols - logo_w) // 2) + logo_w

    # Size the box to its widest real row (probe at a generous width, then measure), never
    # wider than the terminal and never narrower than the title/hint it must show.
    probe = max(10, min(cols - 6, 100))
    measured = max((_ansi_width(line) for line in screen.render_body(probe)), default=10)
    # Size to the fullest the footer can get, not this frame's — a screen whose hint grows
    # as the highlight moves (a select's per-row "Del remove") reports that width here, so
    # the box is reserved for it up front and never widens mid-navigation.
    sizing_footer = getattr(screen, "sizing_footer_hint", screen.footer_hint)
    inner_w = max(measured, cell_len(screen.title), cell_len(sizing_footer))
    inner_w = max(10, min(inner_w, cols - 6))

    # Pin the banner to a fixed vertical anchor that depends only on the terminal height and
    # the banner's own (constant) height, so the wordmark never moves as the box below it
    # swaps contents between splash states (device list → spinner → message). The box hangs
    # from just under the banner and only *it* grows or shrinks; the logo stays put. The
    # anchor sits at 2/5 of the terminal rather than the midline so the block reads centered
    # once the device list has populated (the box only ever grows downward); the brief small
    # states sit slightly high, the conventional optical placement for dialogs.
    top = max(0, rows * 2 // 5 - banner_h - gap)
    footnote_h = 1 if screen.footnote else 0  # the note sits directly under the logo
    # Rows left for the box below the fixed banner block: the panel border is 2 rows.
    below = rows - top - banner_h - footnote_h - gap
    body_lines = screen.render_body(inner_w)
    budget = below - 2
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

    # A small muted line (e.g. a copyright notice) sits immediately under the logo, its right
    # edge hung off the logo's right edge so the two read as one signed block.
    footnote_lines: list[str] = []
    if screen.footnote:
        note = Text.from_markup(f"[muted]{screen.footnote}[/muted]")
        rendered = render_lines(note, cell_len(screen.footnote))
        pad = max(0, logo_right - cell_len(screen.footnote))
        footnote_lines = [" " * pad + line for line in rendered]

    block = banner + footnote_lines + ([""] * gap) + panel_lines
    lines = [""] * top + block
    if len(lines) > rows:
        lines = lines[:rows]
    else:
        lines += [""] * (rows - len(lines))
    return "\n".join(lines)


def _dialog_layout(screen: Screen, cols: int, rows: int) -> tuple[int, int, int, list[str]]:
    """Size a floating dialog: ``(outer_width, vpad, viewport, body_lines)``."""
    # Most dialogs stretch to a generous cap; a screen may instead request a natural width
    # (a short confirm sized to its content), still bounded to the terminal. A grow-only
    # screen's natural width ratchets like its height, so the box never narrows either.
    cap = min(cols - 6, 100)
    natural = getattr(screen, "dialog_width", None)
    max_w = cap if natural is None else max(24, min(cap, screen.ratchet_width(natural)))
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


#: Memoized dialog compositions, keyed by everything the box is a function of. Dialogs
#: stack (a confirm over a picker over a menu), and each layer recomposes on every
#: repaint of the frame beneath it, so a single slot would thrash — a handful covers the
#: deepest realistic stack, LRU-evicted as dialogs change.
_DIALOG_CACHE: "OrderedDict[tuple, str]" = OrderedDict()

#: Dialog compositions the memo keeps.
_DIALOG_CACHE_MAX = 12


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
    border = getattr(screen, "border_style", "accent")
    key = (
        max_w, vpad, screen.title, screen.footer_hint, border,
        more_above, more_below, *visible,
    )
    cached = _DIALOG_CACHE.get(key)
    if cached is not None:
        _DIALOG_CACHE.move_to_end(key)
        return cached
    body = Text.from_ansi("\n".join(visible))
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
    out = "\n".join(apply_corner_glow(render_lines(panel, max_w)))
    _DIALOG_CACHE[key] = out
    if len(_DIALOG_CACHE) > _DIALOG_CACHE_MAX:
        _DIALOG_CACHE.popitem(last=False)
    return out


@on_platform
def _bind(platform: Platform) -> None:
    """Drop the composition memos on a platform switch — their output bakes the theme in.

    Registered at module bottom so the immediate first run (see
    :func:`~meshterm.platforms.on_platform`) finds both caches already defined.
    """
    global _BASE_BOX_CACHE
    _BASE_BOX_CACHE = None
    _DIALOG_CACHE.clear()
