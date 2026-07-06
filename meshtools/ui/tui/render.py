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
