# SPDX-License-Identifier: Apache-2.0
"""Column pinning: a row lands on the app's grid however wide the terminal draws its glyphs.

The reasoning lives in :mod:`meshterm.ui.tui.colsnap`. What these tests pin is the claim that
makes it worth having — that alignment no longer depends on the two width tables being *right*
about a glyph, only on the row saying where its columns are.

So the fixture here is a terminal that **disagrees on purpose** (:class:`_Terminal`, handed the
glyphs it draws at a width of its own). That is not a contrived case: it is the situation the
emoji-width module documents as unprobeable and unfixable-in-general, and the one a node name
off the air walks into every time somebody puts an emoji in it. Every assertion below is about
where a glyph *landed*, never about what was measured.
"""

from __future__ import annotations

import re

from rich.cells import cell_len

from meshterm.ui.tui import colsnap
from meshterm.ui.tui.emoji_width import clusters

_WAVE = "👋"  # two cells by both stock tables; one cell on the reference terminal's font
_ROAD = "\U0001f6e3"  # 🛣 one cell by both tables — the glyph a font may still draw wide
_CA = "🇨🇦"  # a flag: one glyph out of two Regional Indicators
_FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"  # 👨‍👩‍👧 one glyph out of three joined people
_ROW = "│ ❯ Lakeside      ● ▲ ★ ─── 5m │"  # chrome only: nothing here needs an address


class _Terminal:
    """A terminal that lays out what it is written using a width table of its own.

    Honours exactly one control sequence — ``CSI n G``, the absolute column address — and
    ignores every other one, which is how a real terminal treats the theme's colour runs for
    the purposes of column arithmetic.

    Args:
        width: The terminal's width in cells.
        draws: Glyphs this terminal draws at a width the app did not measure, as
            ``{glyph: cells}``. Everything absent is drawn at the measured width, so a test
            names its disagreement and nothing else.
    """

    _CSI = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")

    def __init__(self, width: int, *, draws: dict[str, int] | None = None) -> None:
        """Start with a blank row and the cursor in column 0."""
        self.width = width
        self.draws = draws or {}
        self.cells: list[str] = [" "] * width
        self.column = 0

    def write(self, data: str) -> None:
        """Draw ``data``, obeying any column address in it."""
        position = 0
        for match in self._CSI.finditer(data):
            self._draw(data[position : match.start()])
            if match.group(2) == "G":
                self.column = int(match.group(1) or "1") - 1
            position = match.end()
        self._draw(data[position:])

    def _draw(self, text: str) -> None:
        """Place each glyph of one escape-free stretch, advancing by this terminal's widths."""
        for cluster in clusters(text):
            size = self.draws.get(cluster, cell_len(cluster))
            if 0 <= self.column < self.width:
                self.cells[self.column] = cluster
                for overhang in range(1, size):
                    if self.column + overhang < self.width:
                        self.cells[self.column + overhang] = ""
            self.column += size

    def at(self, column: int) -> str:
        """The glyph drawn in ``column``."""
        return self.cells[column]


def test_a_row_of_chrome_is_handed_back_untouched() -> None:
    """The common row costs one scan and no allocation — the same object comes back.

    Borders, block elements, braille and the node marks are what the app draws by the
    thousand, and pinning after each of them would be five bytes and a cluster walk to
    address the column the cursor is already in.
    """
    assert colsnap.snap_row(_ROW) is _ROW
    assert colsnap.snap_row("plain ascii row") is "plain ascii row"  # noqa: F632 - identity
    assert colsnap.snap_row("⠁⠂⠃⡿ braille chart ▁▃▅█ ╭─╮") is not None


def test_an_emoji_is_followed_by_the_column_it_was_measured_into() -> None:
    """The whole mechanism: the glyph, then the one-based column the next glyph belongs in."""
    assert cell_len(_WAVE) == 2, "the stock table measures the wave at two cells"
    # "a" fills column 0, the wave is measured into columns 1-2, so "b" belongs in column 3.
    assert colsnap.snap_row(f"a{_WAVE}b") == f"a{_WAVE}\x1b[4Gb"


def test_nothing_is_pinned_after_the_last_glyph_on_the_row() -> None:
    """An address is held over until something drawable follows it, and dropped if none does.

    A chat line ending on an emoji is the common case, and there is nothing after it whose
    column could be wrong.
    """
    assert colsnap.snap_row(f"hi {_WAVE}") == f"hi {_WAVE}"


def test_a_style_change_occupies_no_column_and_stays_where_the_theme_put_it() -> None:
    """A colour run is copied through without advancing the column it would have shifted."""
    pinned = colsnap.snap_row(f"\x1b[31m{_WAVE}\x1b[0m|")
    assert pinned == f"\x1b[31m{_WAVE}\x1b[0m\x1b[3G|"


def test_a_flag_and_a_joined_sequence_are_pinned_once_as_whole_glyphs() -> None:
    """One address per glyph, not per codepoint — the cut that halves a flag is never made."""
    for glyph in (_CA, _FAMILY):
        pinned = colsnap.snap_row(f"{glyph}|")
        assert pinned == f"{glyph}\x1b[{cell_len(glyph) + 1}G|"
        assert pinned.count("\x1b[") == 1


def test_a_bare_text_emoji_is_pinned_even_though_both_tables_agree_about_it() -> None:
    """Agreement is not certainty: what a font draws is still the font's business.

    ``🛣`` is the glyph whose measurement must not be *forced* to two (that pulled the Trophy
    case's border in). Pinning is the other answer to the same doubt: leave the measurement
    alone and say where the next column is, so the row holds either way.
    """
    assert cell_len(_ROAD) == 1
    assert colsnap.snap_row(f"{_ROAD}|") == f"{_ROAD}\x1b[2G|"


def test_the_row_frames_flush_on_a_terminal_that_draws_the_glyph_narrow() -> None:
    """The notch, and its absence: a border lands in its measured column once pinned."""
    row = f"│ {_WAVE} Lakeside │"
    edge = cell_len(row) - 1

    loose = _Terminal(cell_len(row), draws={_WAVE: 1})
    loose.write(row)
    assert loose.at(edge) != "│", "unpinned, the narrow glyph pulls the border a column in"
    assert loose.column == cell_len(row) - 1

    pinned = _Terminal(cell_len(row), draws={_WAVE: 1})
    pinned.write(colsnap.snap_row(row))
    assert pinned.at(edge) == "│"
    assert pinned.column == cell_len(row)


def test_the_row_frames_flush_on_a_terminal_that_draws_the_glyph_wide() -> None:
    """The mirror case: the overhang is written over, and the row still ends where it should.

    One cell of the glyph is lost to its neighbour, which is the trade — a cell of cosmetic
    damage inside the glyph's own lane, in place of every lane after it moving.
    """
    row = f"│ {_ROAD} Lakeside │"
    edge = cell_len(row) - 1

    loose = _Terminal(cell_len(row), draws={_ROAD: 2})
    loose.write(row)
    assert loose.at(edge) != "│"

    pinned = _Terminal(cell_len(row), draws={_ROAD: 2})
    pinned.write(colsnap.snap_row(row))
    assert pinned.at(edge) == "│"


def test_every_lane_of_a_purge_preview_row_lands_where_the_screen_measured_it() -> None:
    """The screen this started on: a swept contact whose name carries an emoji.

    The name lane is already fitted in display cells (:func:`~meshterm.ui.menus.fit_cells`),
    which is the padding that cannot help — it is computed with the same measurement the
    terminal disagrees with. What holds the evidence lanes under their headers is the pinning.
    """
    from meshterm.core.contact_score import ContactSignals, ScoredContact
    from meshterm.core.models import Contact
    from meshterm.ui.purge_screen import _NAME_W, _lane_widths, _victim_row
    from meshterm.ui.tui.render import render_to_ansi

    key = "ab" * 32
    victim = ScoredContact(
        contact=Contact(name=f"{_WAVE} Lakeside", public_key=key, key_prefix=key[:12]),
        signals=ContactSignals(node=key[:12], packets=12, hops=2.0),
        score=1.0,
        percentile=3,
    )
    row = _victim_row(victim, _lane_widths([victim]))
    measured = cell_len(row.plain)
    ansi = render_to_ansi(row, 72, no_wrap=True)

    loose = _Terminal(80, draws={_WAVE: 1})
    loose.write(ansi)
    assert loose.column != measured, "unpinned, one emoji shifts every lane after it"

    pinned = _Terminal(80, draws={_WAVE: 1})
    pinned.write(colsnap.snap_row(ansi))
    assert pinned.column == measured
    # Not just the right edge: *every* glyph of the row is in the column the screen's own
    # cell arithmetic put it in, which is what keeps a lane under the header naming it.
    column = 0
    for cluster in clusters(row.plain):
        if cluster != " ":
            assert pinned.at(column) == cluster, f"column {column} holds the wrong glyph"
        column += cell_len(cluster)
    assert column == measured
    assert _NAME_W == 22  # the name lane the evidence starts after, gap included


def test_pinning_is_on_with_an_escape_hatch(monkeypatch) -> None:  # noqa: ANN001
    """On by default; only an explicit 0 writes rows the way they were composed."""
    monkeypatch.delenv("MESHTERM_COLUMN_SNAP", raising=False)
    assert colsnap.enabled()
    monkeypatch.setenv("MESHTERM_COLUMN_SNAP", "0")
    assert not colsnap.enabled()


def test_the_row_writer_pins_what_it_writes(monkeypatch) -> None:  # noqa: ANN001
    """The wiring: a composed frame reaches the terminal carrying its column addresses."""
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.styles import Style

    from meshterm.ui.tui.fastrender import FastRenderer

    class _Capturing:
        """Just enough output for one paint: what was written, and how big the screen is."""

        def __init__(self) -> None:
            self.written: list[str] = []

        def write_raw(self, data: str) -> None:
            self.written.append(data)

        def get_size(self) -> Size:
            return Size(rows=26, columns=72)

        def __getattr__(self, name: str):  # noqa: ANN001, ANN204 - the rest is inert here
            return lambda *args, **kwargs: None

    frame = f"contact {_WAVE} 5m\nsecond row\n"
    out = _Capturing()
    renderer = FastRenderer(Style([]), out, full_screen=True, frame_source=lambda: frame)
    renderer.render(None, None)
    assert "\x1b[11G" in "".join(out.written)  # the wave measured into columns 8-9

    monkeypatch.setenv("MESHTERM_COLUMN_SNAP", "0")
    off = _Capturing()
    plain = FastRenderer(Style([]), off, full_screen=True, frame_source=lambda: frame)
    plain.render(None, None)
    assert "\x1b[11G" not in "".join(off.written)
