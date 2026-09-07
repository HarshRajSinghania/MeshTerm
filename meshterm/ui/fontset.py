"""The PicoCalc console font's codepoint inventory — the glyph contract.

The PicoCalc panel draws through the kernel console with one 512-glyph PSF font
(``meshterm.psf.gz``, built by ``scripts/calculinux-console-font.sh``): the stock
Terminus 6×12 base (a Cyrillic-coverage table — note there are **no accented Latin
letters**), the full braille block, MeshTerm's own marks drawn into donor slots, and a
few aliases. A character outside this set renders as a blank on the panel, so *nothing
outside it may ever be emitted* on the picocalc platform — the render boundary folds
text down to it (:func:`meshterm.ui.theme.fold_text`), and the dual-platform gallery
asserts the fold held.

This table is the **single source of truth** for that contract, shared by the runtime
fold and the tests. It mirrors the font actually installed on the device: the base
inventory was dumped live from the PSF's Unicode table (2026-08-01), minus the donor
codepoints the build script now repurposes, plus the marks it draws in their place.
If the build script's ``MARKS``/``DONORS``/``ALIASES`` change, this table must move in
the same commit — and vice versa.
"""

from __future__ import annotations

#: Inclusive ``(first, last)`` codepoint ranges present in the font. 519 codepoints over
#: 512 glyphs — aliases (the rounded corners onto the square ones, ``⋯`` onto ``…``) and
#: shared slots give a few glyphs more than one codepoint. Verified against a live table
#: dump of the installed font (2026-08-01, post-P3 build).
# fmt: off
FONT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0020, 0x007F), (0x00A0, 0x00A0), (0x00A7, 0x00A7), (0x00A9, 0x00A9),
    (0x00B0, 0x00B0), (0x00B2, 0x00B2), (0x00B6, 0x00B7), (0x03C0, 0x03C0),
    (0x0401, 0x0401),
    (0x0404, 0x0404), (0x0406, 0x0407), (0x0410, 0x044F), (0x0451, 0x0451),
    (0x0454, 0x0454), (0x0456, 0x0457), (0x0490, 0x0491), (0x2014, 0x2014),
    (0x2022, 0x2022), (0x2026, 0x2026), (0x2190, 0x2195), (0x21A8, 0x21A8),
    (0x21BB, 0x21BB), (0x21E7, 0x21E7), (0x221A, 0x221A), (0x221F, 0x221F),
    (0x2248, 0x2248), (0x2264, 0x2265), (0x22EF, 0x22EF), (0x2302, 0x2302),
    (0x2316, 0x2316), (0x232B, 0x232B), (0x2500, 0x2500), (0x2502, 0x2502),
    (0x250C, 0x250C), (0x2510, 0x2510), (0x2514, 0x2514), (0x2518, 0x2518),
    (0x251C, 0x251C), (0x2524, 0x2524), (0x252C, 0x252C), (0x2534, 0x2534),
    (0x253C, 0x253C), (0x2550, 0x2551), (0x2554, 0x2554), (0x2557, 0x2557),
    (0x2559, 0x255B),
    (0x255D, 0x2561), (0x2563, 0x2563), (0x2566, 0x256A), (0x256C, 0x2570),
    (0x2580, 0x2580), (0x2584, 0x2584), (0x2588, 0x2588), (0x258C, 0x258C),
    (0x2590, 0x2593), (0x25A0, 0x25A0), (0x25AC, 0x25AC), (0x25B2, 0x25B2),
    (0x25B6, 0x25B6), (0x25B8, 0x25B8), (0x25BA, 0x25BA), (0x25BC, 0x25BC),
    (0x25C0, 0x25C0), (0x25C4, 0x25C4), (0x25C9, 0x25C9), (0x25CB, 0x25CB),
    (0x25CF, 0x25CF), (0x25F7, 0x25F7), (0x2605, 0x2605), (0x263C, 0x263C),
    (0x2699, 0x2699), (0x26A0, 0x26A0), (0x26BF, 0x26BF), (0x2713, 0x2713),
    (0x2717, 0x2717), (0x276F, 0x276F), (0x2800, 0x28FF), (0xFFFD, 0xFFFD),
)
# fmt: on

#: Every codepoint the console font can draw, as one frozen set (built once at import;
#: 520 members, so the set is small and the per-character membership test is a dict hit).
FONT_CODEPOINTS: frozenset[int] = frozenset(
    cp for first, last in FONT_RANGES for cp in range(first, last + 1)
)


def in_font(char: str) -> bool:
    """Whether the console font has a glyph for ``char``."""
    return ord(char) in FONT_CODEPOINTS
