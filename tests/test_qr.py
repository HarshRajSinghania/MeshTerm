"""The terminal QR renderers: half-block and braille forms of the same matrix."""

import segno

from meshterm.ui.qr import _BORDER, _BRAILLE_BITS, qr_braille, qr_text

URL = "meshcore://contact/add?name=Test&public_key=" + "ab" * 32 + "&type=1"


def _matrix(data: str) -> list[list[bool]]:
    qr = segno.make(data, error="m")
    return [[bool(v) for v in row] for row in qr.matrix_iter(border=_BORDER)]


def test_qr_text_matches_matrix() -> None:
    rows = _matrix(URL)
    lines = qr_text(URL).plain.rstrip("\n").split("\n")
    glyph_bits = {"█": (True, True), "▀": (True, False), "▄": (False, True), " ": (False, False)}
    for y, row in enumerate(rows):
        for x, dark in enumerate(row):
            top, bottom = glyph_bits[lines[y // 2][x]]
            assert (top if y % 2 == 0 else bottom) == dark


def test_qr_braille_matches_matrix() -> None:
    """Every module maps to exactly its braille dot — one dot per module, raster order."""
    rows = _matrix(URL)
    lines = qr_braille(URL).plain.rstrip("\n").split("\n")
    for y, row in enumerate(rows):
        for x, dark in enumerate(row):
            cell = ord(lines[y // 4][x // 2]) - 0x2800
            assert bool(cell & _BRAILLE_BITS[x % 2][y % 4]) == dark


def test_qr_braille_geometry() -> None:
    """Whole-cell padding: every line is braille-block glyphs at half width, quarter height."""
    rows = _matrix(URL)
    lines = qr_braille(URL).plain.rstrip("\n").split("\n")
    assert len(lines) == -(-len(rows) // 4)
    assert all(len(line) == -(-len(rows[0]) // 2) for line in lines)
    assert all(0x2800 <= ord(ch) <= 0x28FF for line in lines for ch in line)
