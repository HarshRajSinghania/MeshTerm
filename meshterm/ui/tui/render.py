"""The single Rich-to-prompt_toolkit bridge.

The TUI keeps rendering in Rich (all existing tables, panels, and text widgets are reused
verbatim) and lets prompt_toolkit own the terminal, input, and resize events. This module
is the seam between the two: it renders any Rich renderable to an ANSI string at a chosen
width, which prompt_toolkit then draws via :class:`prompt_toolkit.formatted_text.ANSI`.

Because rendering is redone at the *current* width on every repaint, content reflows
automatically when the terminal is resized.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console, RenderableType
from rich.text import Text

from ..theme import MESH_THEME

#: Cache one headless render console per width. Consoles are cheap but repaint happens on
#: every keystroke and on the live-monitor timer, so caching avoids needless churn. Keyed
#: by width because a console's width is fixed at construction.
_CONSOLES: dict[int, Console] = {}


def _console(width: int) -> Console:
    """Return a themed, headless console that emits ANSI at the given width.

    Args:
        width: Target render width in columns.

    Returns:
        A cached :class:`rich.console.Console` writing to an internal buffer.
    """
    console = _CONSOLES.get(width)
    if console is None:
        console = Console(
            theme=MESH_THEME,
            width=width,
            file=StringIO(),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            soft_wrap=False,
        )
        _CONSOLES[width] = console
    return console


def render_to_ansi(renderable: RenderableType, width: int, *, no_wrap: bool = False) -> str:
    """Render a Rich renderable to an ANSI string at ``width`` columns.

    Args:
        renderable: Any Rich renderable (table, panel, text, group, markup string).
        width: Target width in columns; content wraps/pads to it.
        no_wrap: When ``True``, keep the content on a single line — show what fits and
            crop the overflow (with an ellipsis) instead of wrapping onto more rows.

    Returns:
        The rendered output as an ANSI-escaped string, without a trailing newline.
    """
    width = max(1, width)
    console = _console(width)
    with console.capture() as capture:
        if no_wrap:
            console.print(renderable, end="", no_wrap=True, overflow="ellipsis", crop=True)
        else:
            console.print(renderable, end="")
    return capture.get()


def render_lines(renderable: RenderableType, width: int, *, no_wrap: bool = False) -> list[str]:
    """Render a Rich renderable to a list of ANSI lines at ``width`` columns.

    Each returned line is an independently styled ANSI string, so the caller can slice a
    vertical viewport (for scrolling) without splitting escape sequences.

    Args:
        renderable: Any Rich renderable.
        width: Target width in columns.
        no_wrap: When ``True``, keep the content to a single cropped line (see
            :func:`render_to_ansi`).

    Returns:
        The rendered lines, newline-free. A trailing empty line (from the final newline)
        is dropped so line counts match visible rows.
    """
    ansi = render_to_ansi(renderable, width, no_wrap=no_wrap)
    lines = ansi.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def right_aligned_tail(body: Text, tail: Text, width: int) -> Text:
    """Lay ``body`` out with ``tail`` pinned to the right edge of its last line.

    ``body`` wraps at ``width`` as usual; ``tail`` — a short status such as the chat
    compose bar's byte counter — is right-aligned on ``body``'s final wrapped line when
    it still fits there (after at least one blank cell), otherwise on a new line of its
    own, also right-aligned. So the tail reads as a steady gauge in the corner rather
    than trailing the cursor and wrapping along with the text.

    Args:
        body: The wrappable leading content (e.g. the input line, cursor block included).
        tail: The short run to pin to the right edge.
        width: Total render width in columns.

    Returns:
        A single :class:`Text` with embedded newlines, ready for :func:`render_lines`.
    """
    width = max(1, width)
    console = _console(width)
    lines = list(body.wrap(console, width)) or [Text("")]
    combined = Text()
    for line in lines[:-1]:
        combined.append_text(line)
        combined.append("\n")
    last = lines[-1]
    gap = width - last.cell_len - tail.cell_len
    if gap >= 1:
        combined.append_text(last)
        combined.append(" " * gap)
    else:
        combined.append_text(last)
        combined.append("\n")
        combined.append(" " * max(0, width - tail.cell_len))
    combined.append_text(tail)
    return combined


def render_hanging(prefix: Text, body: Text, width: int, *, indent: int) -> list[str]:
    """Render ``prefix + body`` at ``width``, wrapping the body with a hanging indent.

    The first visual line carries ``prefix`` followed by the body; every wrapped
    continuation line is padded by ``indent`` columns so it aligns under the body rather
    than falling back to column zero. Used by the chat transcript so a wrapped message
    lines up with its own first line instead of with the timestamp gutter.

    Args:
        prefix: The leading run (e.g. a pointer + timestamp) shown once, on the first line.
        body: The wrappable message text; styling (mentions, glyphs) is preserved.
        width: Total render width in columns.
        indent: Columns to indent continuation lines by (typically ``prefix.cell_len``).

    Returns:
        The rendered lines, newline-free.
    """
    width = max(1, width)
    console = _console(width)
    avail = max(1, width - indent)
    wrapped = list(body.wrap(console, avail)) or [Text("")]
    pad = " " * indent
    combined = Text()
    for i, line in enumerate(wrapped):
        if i:
            combined.append("\n")
        combined.append_text(prefix if i == 0 else Text(pad))
        combined.append_text(line)
    return render_lines(combined, width)
