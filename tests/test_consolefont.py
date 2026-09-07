"""Tests for the bundled console font and the machinery that installs and selects it.

The Win32 half cannot be exercised off Windows, and should not be exercised *on* it by a
test suite — installing a font is a change to the machine, and one that cannot be undone
inside a session (Windows locks the file once it is loaded). So what is pinned here is
everything that can be checked without touching the system: that the font we ship is the
font we say we ship, that it can actually draw what MeshTerm draws, and that every entry
point degrades rather than raises where there is no console to talk to.

The font's own coverage is asserted from its ``cmap``, which makes this the desktop
counterpart to :mod:`meshterm.ui.fontset` — the PicoCalc's device-verified inventory.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

from meshterm.core import consolefont
from meshterm.ui.termfont import CHART_FONTS, face_draws_charts, installed_chart_font

BRAILLE = range(0x2800, 0x2900)


def _cmap(path: Path) -> set[int]:
    """Every codepoint a TrueType font maps, read from its ``cmap`` table.

    A short format-4 reader rather than a dependency: the question asked here is narrow
    and the alternative is trusting a font file we ship without ever looking inside it.
    """
    data = path.read_bytes()
    count = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for i in range(count):
        offset = 12 + i * 16
        tag, _, start, _length = struct.unpack(">4sIII", data[offset : offset + 16])
        tables[tag.decode("latin-1")] = start

    cmap = tables["cmap"]
    covered: set[int] = set()
    for i in range(struct.unpack(">H", data[cmap + 2 : cmap + 4])[0]):
        record = cmap + 4 + i * 8
        sub = cmap + struct.unpack(">HHI", data[record : record + 8])[2]
        if struct.unpack(">H", data[sub : sub + 2])[0] != 4:
            continue
        seg_bytes = struct.unpack(">H", data[sub + 6 : sub + 8])[0]
        segs = seg_bytes // 2
        ends = struct.unpack(f">{segs}H", data[sub + 14 : sub + 14 + seg_bytes])
        starts_at = sub + 16 + seg_bytes
        starts = struct.unpack(f">{segs}H", data[starts_at : starts_at + seg_bytes])
        deltas_at = starts_at + seg_bytes
        deltas = struct.unpack(f">{segs}h", data[deltas_at : deltas_at + seg_bytes])
        ranges_at = deltas_at + seg_bytes
        ranges = struct.unpack(f">{segs}H", data[ranges_at : ranges_at + seg_bytes])
        for seg in range(segs):
            for cp in range(starts[seg], min(ends[seg], 0xFFFF) + 1):
                if ranges[seg] == 0:
                    glyph = (cp + deltas[seg]) & 0xFFFF
                else:
                    at = ranges_at + seg * 2 + ranges[seg] + (cp - starts[seg]) * 2
                    if at + 2 > len(data):
                        continue
                    glyph = struct.unpack(">H", data[at : at + 2])[0]
                    if glyph:
                        glyph = (glyph + deltas[seg]) & 0xFFFF
                if glyph:
                    covered.add(cp)
    return covered


def test_the_font_we_ship_is_present_with_its_licence() -> None:
    """It is redistributed under the SIL OFL, which travels with the file or not at all."""
    assert consolefont.BUNDLED_FONT.is_file()
    licence = consolefont.FONT_DIR / "CascadiaMono-OFL.txt"
    assert licence.is_file()
    text = licence.read_text(encoding="utf-8")
    assert "SIL Open Font License" in text
    assert "Reserved Font Name" in text


def test_the_bundled_font_can_draw_the_charts() -> None:
    """The reason this font and not another: it has the braille block, and few do.

    Measured across every font on a development machine, no mainstream coder font carried
    it — Hack Nerd Font, JetBrains Mono, Fira Code, Source Code Pro and even DejaVu Sans
    *Mono* all hold none of the 256. Shipping one that cannot draw a chart would leave the
    offer technically successful and visibly pointless.
    """
    covered = _cmap(consolefont.BUNDLED_FONT)
    missing = [cp for cp in BRAILLE if cp not in covered]
    assert not missing, f"{len(missing)} braille cells missing from the bundled font"


def test_the_bundled_font_covers_the_marks_and_the_path_chips() -> None:
    """The rest of what a classic console has to draw from its font alone.

    The powerline separators are why the ``PL`` build is the one bundled rather than the
    plain one: 25KB more, and the path lines keep their chips.
    """
    covered = _cmap(consolefont.BUNDLED_FONT)
    for mark in "✓❯◉●○▲■─│╭╮╰╯▌▐░▒▓█←↑→↓↔↕…":
        assert ord(mark) in covered, f"{mark!r} is not in the bundled font"
    for chip in "":  # the powerline separator and its round caps
        assert ord(chip) in covered, f"U+{ord(chip):04X} is not in the bundled font"


def test_the_bundled_face_is_what_the_font_calls_itself() -> None:
    """``SetCurrentConsoleFontEx`` matches on the family name exactly, so a typo is silent.

    It would install the font, fail to select it, and report a console that "kept its own
    font" — with nothing anywhere naming the real cause.
    """
    data = consolefont.BUNDLED_FONT.read_bytes()
    count = struct.unpack(">H", data[4:6])[0]
    name_table = next(
        struct.unpack(">4sIII", data[12 + i * 16 : 28 + i * 16])[2]
        for i in range(count)
        if struct.unpack(">4sIII", data[12 + i * 16 : 28 + i * 16])[0] == b"name"
    )
    records, strings = struct.unpack(">HH", data[name_table + 2 : name_table + 6])
    families = set()
    for i in range(records):
        at = name_table + 6 + i * 12
        platform, encoding, language, name_id, length, offset = struct.unpack(
            ">HHHHHH", data[at : at + 12]
        )
        if (platform, encoding, language, name_id) == (3, 1, 0x409, 1):
            start = name_table + strings + offset
            families.add(data[start : start + length].decode("utf-16-be"))
    assert consolefont.BUNDLED_FACE in families, f"font calls itself {families}"


def test_the_bundled_face_is_one_we_would_accept() -> None:
    """The font we install has to satisfy the check that decides whether to offer it.

    Otherwise MeshTerm installs it, selects it, and offers again on the next launch.
    """
    assert face_draws_charts(consolefont.BUNDLED_FACE)


def test_chart_fonts_are_matched_by_family_however_spelled() -> None:
    """Cascadia ships plain, PL and NF builds, and the Nerd Font patches rename it.

    Matched on the *family* name, which is what a terminal's config, the ``HKCU``
    registration and ``SetCurrentConsoleFontEx`` all speak — never the filename. Those
    differ here: ``CascadiaMonoPL.ttf`` calls itself ``Cascadia Mono PL``, spaced.
    """
    assert face_draws_charts("Cascadia Mono")
    assert face_draws_charts("Cascadia Mono PL")
    assert face_draws_charts("Cascadia Mono NF")
    assert face_draws_charts("  cascadia   code  ")  # spacing and case are normalised
    assert face_draws_charts("CaskaydiaCove Nerd Font Mono")


def test_the_fonts_that_cannot_draw_charts_are_not_claimed() -> None:
    """Every one of these was measured at zero braille cells; a wrong yes here is a lie.

    It would leave a reader on a classic console with boxes for charts and no offer to
    fix them, which is the exact failure the whole check exists to prevent.
    """
    for face in (
        "Consolas",
        "Lucida Console",
        "Courier New",
        "Hack Nerd Font Mono",
        "JetBrains Mono",
        "Fira Code",
        "Source Code Pro",
        "DejaVu Sans Mono",
    ):
        assert not face_draws_charts(face), f"{face} does not carry the braille block"
    assert not face_draws_charts(None)
    assert not face_draws_charts("")


def test_every_chart_font_entry_is_normalised() -> None:
    """The list is compared against normalised faces, so an entry with capitals never hits."""
    for entry in CHART_FONTS:
        assert entry == entry.lower().strip()
        assert "  " not in entry


def test_asking_the_machine_what_it_has_never_raises() -> None:
    """Best-effort, on every platform: a font scan that throws would take startup with it."""
    assert installed_chart_font() is None or isinstance(installed_chart_font(), str)


def test_reading_the_console_font_never_raises() -> None:
    """Under pytest there is no console worth asking, and the answer must simply be None."""
    assert consolefont.current_face() is None or isinstance(consolefont.current_face(), str)


def test_the_user_font_directory_is_under_the_users_own_profile() -> None:
    """The whole point of the per-user location: no administrator rights are involved."""
    directory = consolefont.user_font_dir()
    assert directory.parts[-3:] == ("Microsoft", "Windows", "Fonts")
    assert "AppData" in str(directory) or "Local" in str(directory)


@pytest.mark.skipif(sys.platform == "win32", reason="would touch the real machine")
def test_installing_is_a_no_op_where_there_are_no_windows_fonts() -> None:
    """Off Windows the offer is never made, and the install refuses rather than pretends."""
    assert consolefont.install_bundled_font() is False
    assert consolefont.select("Cascadia Mono PL") is False
