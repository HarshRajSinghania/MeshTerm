"""Render a string as a scannable QR code in the terminal, using half-block characters.

Each character cell stacks two vertical modules — ``▀`` (upper only), ``▄`` (lower only),
``█`` (both), space (neither) — so a code renders at half the row height of a full-block
one and comfortably fits an interactive window. Modules are always drawn black on a white
background (with the mandatory quiet-zone border), so a phone camera reads the code no
matter what colour theme the terminal itself is using.

:func:`qr_braille` is the dense variant: one braille dot per module packs a 2×4 block of
modules into every cell — half the columns and a quarter of the rows of the half-block
form. The trade is ink coverage: a braille dot is a dot, not a filled cell, so dark
regions carry white in-between and scanning leans on the camera's tolerance for
dot-style codes.
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

#: Braille dot bit for each (column, row) of a cell's 2×4 module block, rows top-down —
#: the Unicode block numbers dots 1,2,3,7 down the left column and 4,5,6,8 down the
#: right (``braillechart`` keeps the same tables flipped bottom-up for bar math; QR
#: rows arrive top-down, so these stay in raster order).
_BRAILLE_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


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


def qr_braille(data: str, *, error: str = "m") -> Text:
    """Render ``data`` as a QR code built from braille characters, one dot per module.

    Each cell carries a 2-wide × 4-tall block of modules, so the code draws at half the
    width and a quarter of the height of :func:`qr_text` — small enough to leave room
    around it even on the 40-column PicoCalc panel. Same black-on-white contract; the
    empty cell is ``U+2800`` (the blank braille pattern) rather than a space so every
    glyph comes from the one Unicode block and renders at one consistent width.

    Args:
        data: The string to encode.
        error: QR error-correction level — ``l``/``m``/``q``/``h`` (default ``m``).

    Returns:
        A Rich :class:`~rich.text.Text` (no-wrap) whose lines draw the code.
    """
    import segno

    qr = segno.make(data, error=error)
    rows = [[bool(v) for v in row] for row in qr.matrix_iter(border=_BORDER)]
    # Pad to whole cells with light modules: out to a multiple of 4 rows and 2 columns.
    while len(rows) % 4:
        rows.append([False] * len(rows[0]))
    if len(rows[0]) % 2:
        for row in rows:
            row.append(False)

    text = Text(no_wrap=True)
    for band in zip(*(rows[i::4] for i in range(4)), strict=True):
        line = ""
        for x in range(0, len(band[0]), 2):
            bits = 0
            for col in (0, 1):
                for dot_row in range(4):
                    if band[dot_row][x + col]:
                        bits |= _BRAILLE_BITS[col][dot_row]
            line += chr(0x2800 + bits)
        text.append(line, style=_STYLE)
        text.append("\n")
    return text
