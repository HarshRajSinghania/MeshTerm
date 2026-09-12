"""Render a string as a scannable QR code in the terminal, using half-block characters.

Each character cell stacks two vertical modules — ``▀`` (upper only), ``▄`` (lower only),
``█`` (both), space (neither) — so a code renders at half the row height of a full-block
one. Modules are always drawn **white on black**: the code's dark modules as white ink,
its light modules and quiet zone as a black field, whatever colour theme the terminal is
running. A phone camera reads contrast, and pure white on pure black is the most of it a
screen has; a light-on-dark code is what every scanner made this decade expects to meet
on a screen.

When the code *is* the answer it is a **full-screen** thing: :func:`share_screen` puts it
on a bare frame — no header, no footer, no title, no box — with nothing beside it but the
URL it encodes, so the whole panel is contrast for the camera and the one line a reader
might type out instead. The one place a code sits *inside* a page is a ``qr`` fence in a
written page (:mod:`~meshterm.ui.markdown`), which draws :func:`qr_text` in the flow of
its prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Group
from rich.text import Text

from .tui.render import render_lines
from .tui.screen import ScrollScreen

if TYPE_CHECKING:
    import segno

    from ..context import AppContext

#: Quiet-zone width (modules) around the code. Scanners want 4 to lock on; 2 still reads
#: on a screen, and is what a code falls back to when its frame is too small for 4.
_BORDER = 4

#: The glyph for each (top-module-dark, bottom-module-dark) pair, drawn as ink — so a
#: dark module is the ink colour and a light module shows the field behind it.
_GLYPH = {
    (True, True): "█",  # █ full block
    (True, False): "▀",  # ▀ upper half
    (False, True): "▄",  # ▄ lower half
    (False, False): " ",
}

#: Every glyph's style — the theme's ``qr``: pure white ink on a pure black field, both
#: ends named so the contrast holds regardless of the surrounding terminal palette. A
#: theme *name* rather than the hex itself so the PicoCalc's slots are chosen on purpose
#: (its bright white and its black), as every fixed hue in the app is. A console without
#: the theme draws the glyphs unstyled, which the plain CLI face would anyway.
_STYLE = "qr"

#: The fits a code tries, in order, until one is small enough for the frame drawing it:
#: the standard code first, then a lighter error level (a screen is never smudged, so the
#: redundancy buys nothing a camera needs), then the narrower quiet zone. A contact card
#: — a name, a 64-hex key — is 57 cells across and 29 rows tall at the standard fit; the
#: PicoCalc panel is 53 across, and a regular terminal is 24 rows.
_FITS: tuple[tuple[str, int], ...] = (("m", 4), ("l", 4), ("m", 2), ("l", 2))


def _draw(code: segno.QRCode, border: int, *, indent: int = 0) -> Text:
    """Draw a segno code as half-block rows, ``border`` light modules around it.

    ``indent`` is the one way a code is ever moved across a line: the same run of bare
    cells in front of *every* row. A code must never be justified — Rich's centring
    strips each row's trailing spaces before it pads, so a row whose right edge is light
    modules loses cells and lands a column off its neighbours, and a finder square one
    row skewed is a code no camera can lock on to.
    """
    rows = [[bool(v) for v in row] for row in code.matrix_iter(border=border)]
    # Pair rows top-to-bottom; pad an odd final row with light modules so the last
    # half-block cell renders cleanly.
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))

    text = Text(no_wrap=True)
    for i, (top, bottom) in enumerate(zip(rows[0::2], rows[1::2], strict=True)):
        if i:
            text.append("\n")
        if indent:
            text.append(" " * indent)
        line = "".join(_GLYPH[(t, b)] for t, b in zip(top, bottom, strict=True))
        text.append(line, style=_STYLE)
    return text


def qr_text(data: str, *, error: str = "m", border: int = _BORDER) -> Text:
    """Render ``data`` as a QR code built from half-block characters.

    Args:
        data: The string to encode (e.g. a ``meshcore://channel/add`` share URL).
        error: QR error-correction level — ``l``/``m``/``q``/``h`` (default ``m``).
        border: Quiet-zone width in modules (default :data:`_BORDER`).

    Returns:
        A Rich :class:`~rich.text.Text` (no-wrap) whose lines draw the code, white on
        black, ready to hand to ``ctx.ui.view`` / ``ctx.ui.show`` or to sit in a page.
    """
    import segno

    return _draw(segno.make(data, error=error), border)


def _fit(data: str, width: int, height: int | None) -> tuple[segno.QRCode, int] | None:
    """The first of :data:`_FITS` within ``width`` cells and ``height`` rows, as (code, border).

    ``None`` when no fit is that small — the caller decides what to relax.
    """
    import segno

    for error, border in _FITS:
        code = segno.make(data, error=error)
        modules = code.symbol_size(border=border)[0]
        if modules <= width and (height is None or (modules + 1) // 2 <= height):
            return code, border
    return None


def _smallest(data: str) -> tuple[segno.QRCode, int]:
    """The last of :data:`_FITS` — what a frame too small for any fit gets anyway."""
    import segno

    error, border = _FITS[-1]
    return segno.make(data, error=error), border


def fit_qr(data: str, width: int, height: int | None = None) -> Text:
    """The code for ``data`` at the first of :data:`_FITS` that fits the space given.

    A code that fits at no fit is drawn at the smallest anyway: a cut code is not
    scannable, but neither is no code, and a larger terminal is one resize away.

    Args:
        data: The string to encode.
        width: Cells available across.
        height: Rows available, or ``None`` to fit the width alone.

    Returns:
        The code, as :func:`qr_text` draws it.
    """
    code, border = _fit(data, width, height) or _smallest(data)
    return _draw(code, border)


class QrScreen(ScrollScreen):
    """THE share screen: a code on a bare frame, and under it the URL it encodes.

    A **bare frame, not a popup** (:attr:`~meshterm.ui.tui.screen.Screen.bare`): no
    header, no footer, no box, no title, no instruction — a camera pointed at the screen
    wants the code and nothing arguing with it for contrast, and the reader wants the
    one line they might type out instead. Esc leaves, as it does everywhere.

    The code is **fitted to the frame every paint** (:func:`fit_qr`), which is why this is
    a screen of its own rather than a renderable handed to a result window: only the
    frame knows its rows, and a code that outgrows them is cut, and a cut code scans as
    nothing. The fit is asked to hold the code *and* the URL first; failing that, the
    code alone, with the URL a page down; failing that, the smallest code there is, and
    the frame windows it from the top so a larger terminal shows it whole.
    """

    bare = True

    def __init__(self, url: str, *, title: str) -> None:
        """Set up the share screen for ``url``.

        Args:
            url: The ``meshcore://…`` share URL, encoded as the code and printed under it.
            title: ``Share {name}`` — never drawn on the bare frame; the screen's name
                for the log and the stack.
        """
        super().__init__(Text(""), title=title, floating=False)
        self.url = url

    def render_body(self, width: int) -> list[str]:
        """The code fitted to ``width`` and the frame's rows, then a blank row, then the URL."""
        # The frame notes its height before it asks for the body (compose_bare), so
        # this is the whole terminal's rows; before any paint it is the default 1, and
        # the smallest code stands in until the first paint corrects it.
        rows = self._scroll_viewport
        link = Text(self.url, style="accent", justify="center")
        link_rows = len(render_lines(link, width))
        code, border = (
            _fit(self.url, width, rows - 1 - link_rows)
            or _fit(self.url, width, rows)
            or _fit(self.url, width, None)
            or _smallest(self.url)
        )
        # Centred as a block — one indent for every row (see _draw), never justified.
        indent = max(0, (width - code.symbol_size(border=border)[0]) // 2)
        self.replace_content(Group(_draw(code, border, indent=indent), Text(""), link))
        return super().render_body(width)


async def share_screen(ctx: AppContext, *, name: str, url: str) -> None:
    """Show the share screen for ``url``: the code, full-frame, and the URL under it.

    One surface for every ``Share {name}`` — the channel share and the contact card both
    come through here, so a change to how MeshTerm presents a share code lands on all of
    them at once. In the menu it is :class:`QrScreen`; on the plain CLI face it prints
    the same two things in order, there being no frame to fit.

    Args:
        ctx: Shared application context (provides the UI surface).
        name: What is being shared — the screen is titled ``Share {name}`` where a
            title is stated at all.
        url: The ``meshcore://…`` share URL, encoded as the QR code and printed under it.
    """
    from .surface import TuiUi

    title = f"Share {name}"
    if isinstance(ctx.ui, TuiUi):
        await ctx.ui.session.run_screen(QrScreen(url, title=title))
        return
    await ctx.ui.view(Group(qr_text(url), Text(""), Text(url, style="accent")), title=title)
