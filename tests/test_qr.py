"""The terminal QR renderers: half-block and quadrant-mosaic forms of the same matrix."""

import segno

from meshterm.ui.qr import _BORDER, _QUAD, qr_quad, qr_text

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


def test_qr_quad_matches_matrix() -> None:
    """Every module maps to exactly its quarter of a mosaic glyph, raster order."""
    rows = _matrix(URL)
    lines = qr_quad(URL).plain.rstrip("\n").split("\n")
    quarters = {glyph: quartet for quartet, glyph in _QUAD.items()}
    for y, row in enumerate(rows):
        for x, dark in enumerate(row):
            quartet = quarters[lines[y // 2][x // 2]]
            assert quartet[(y % 2) * 2 + (x % 2)] == dark


def test_qr_quad_geometry() -> None:
    """Whole-cell padding: equal-length lines at half the matrix width and height."""
    rows = _matrix(URL)
    lines = qr_quad(URL).plain.rstrip("\n").split("\n")
    assert len(lines) == -(-len(rows) // 2)
    assert all(len(line) == -(-len(rows[0]) // 2) for line in lines)
