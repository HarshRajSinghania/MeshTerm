"""Render a string as a scannable QR code in the terminal, using half-block characters.

Each character cell stacks two vertical modules — ``▀`` (upper only), ``▄`` (lower only),
``█`` (both), space (neither) — so a code renders at half the row height of a full-block
one and comfortably fits an interactive window. Modules are always drawn black on a white
background (with the mandatory quiet-zone border), so a phone camera reads the code no
matter what colour theme the terminal itself is using.

:func:`qr_quad` is the dense variant: quadrant mosaic glyphs (``▘▚▛█``…) pack a 2×2
block of modules into every cell — half the columns of the half-block form at the same
row count. Ink stays solid (every dark module is a filled quarter-cell), so it scans
exactly as well as the half-block render, just drawn smaller. (A braille one-dot-per-
module form was tried first: it drew even smaller but the dots' white surrounds starve
the scanner of ink, and phones would not read it.)
"""

from __future__ import annotations

from rich.text import Text

#: Quiet-zone width (modules) around the code. Scanners need at least 4 to lock on.
_BORDER = 4

#: Half-block glyph for each (top-module-dark, bottom-module-dark) pair. Drawn as black
#: ink, so a dark module is black and a light module shows the white cell background.
_GLYPH = {
    (True, True): "█",  # █ full block
    (True, False): "▀",  # ▀ upper half
    (False, True): "▄",  # ▄ lower half
    (False, False): " ",
}

#: Constant style for every glyph: black ink on a white field. Naming both ends means the
#: contrast holds regardless of the surrounding terminal palette.
_STYLE = "black on white"

#: Quadrant glyph for each (top-left, top-right, bottom-left, bottom-right) dark-module
#: quartet — the sixteen 2×2 mosaics Unicode provides (space and the half/full blocks
#: fill the patterns the Block Elements quadrant range skips).
_QUAD = {
    (False, False, False, False): " ",
    (True, False, False, False): "▘",
    (False, True, False, False): "▝",
    (True, True, False, False): "▀",
    (False, False, True, False): "▖",
    (True, False, True, False): "▌",
    (False, True, True, False): "▞",
    (True, True, True, False): "▛",
    (False, False, False, True): "▗",
    (True, False, False, True): "▚",
    (False, True, False, True): "▐",
    (True, True, False, True): "▜",
    (False, False, True, True): "▄",
    (True, False, True, True): "▙",
    (False, True, True, True): "▟",
    (True, True, True, True): "█",
}


def qr_text(data: str, *, error: str = "m") -> Text:
    """Render ``data`` as a QR code built from half-block characters.

    Args:
        data: The string to encode (e.g. a ``meshcore://channel/add`` share URL).
        error: QR error-correction level — ``l``/``m``/``q``/``h`` (default ``m``).

    Returns:
        A Rich :class:`~rich.text.Text` (no-wrap) whose lines draw the code, ready to hand
        to ``ctx.ui.view`` / ``ctx.ui.show``.
    """
    import segno

    qr = segno.make(data, error=error)
    rows = [[bool(v) for v in row] for row in qr.matrix_iter(border=_BORDER)]
    # Pair rows top-to-bottom; pad an odd final row with light (white) modules so the last
    # half-block cell renders cleanly.
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))

    text = Text(no_wrap=True)
    for top, bottom in zip(rows[0::2], rows[1::2], strict=True):
        line = "".join(_GLYPH[(t, b)] for t, b in zip(top, bottom, strict=True))
        text.append(line, style=_STYLE)
        text.append("\n")
    return text


def qr_quad(data: str, *, error: str = "m") -> Text:
    """Render ``data`` as a QR code built from quadrant mosaics, 2×2 modules per cell.

    Half the width of :func:`qr_text` at the same row count, and every dark module is
    still a solid quarter-cell of ink — the property a scanner actually needs — so the
    smaller draw reads as well as the half-block form.

    Args:
        data: The string to encode.
        error: QR error-correction level — ``l``/``m``/``q``/``h`` (default ``m``).

    Returns:
        A Rich :class:`~rich.text.Text` (no-wrap) whose lines draw the code.
    """
    import segno

    qr = segno.make(data, error=error)
    rows = [[bool(v) for v in row] for row in qr.matrix_iter(border=_BORDER)]
    # Pad to whole cells with light modules: out to an even row and column count.
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))
    if len(rows[0]) % 2:
        for row in rows:
            row.append(False)

    text = Text(no_wrap=True)
    for top, bottom in zip(rows[0::2], rows[1::2], strict=True):
        line = "".join(
            _QUAD[(top[x], top[x + 1], bottom[x], bottom[x + 1])]
            for x in range(0, len(top), 2)
        )
        text.append(line, style=_STYLE)
        text.append("\n")
    return text
