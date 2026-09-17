# SPDX-License-Identifier: Apache-2.0
"""Reserve-two emoji widths: one rule in every width authority, and every glyph kept whole.

The measuring story lives in :mod:`meshterm.ui.tui.emoji_width`, and the placement half in
:mod:`meshterm.ui.tui.colsnap`. What these tests pin:

* every glyph that may be drawn as an emoji is reserved two cells, in Rich and in prompt_toolkit;
* the app's own marks, Rich's single-cell ranges and ordinary text keep their stock widths;
* Rich's three measurements and prompt_toolkit's agree about every string, which is what keeps
  Rich's segment splitter from walking past a cut it can never meet;
* the app's source draws no text-default emoji its vocabulary does not name;
* a glyph built from several codepoints is split, cut and laid out as the one glyph it is.

:func:`~meshterm.ui.tui.emoji_width.install` patches process-global tables, so every test that
needs it goes through the ``installed`` fixture, which puts every table and cache back afterwards.
"""

from __future__ import annotations

import ast
import pathlib
import threading
from collections.abc import Iterator

import prompt_toolkit.utils as ptu
import pytest
import rich.cells as cells
import rich.segment as segment
from prompt_toolkit.utils import get_cwidth

from meshterm.ui.tui import emoji_width as ew

_ZWJ = "\u200d"  # the joiner: the tell that its neighbours are a single glyph
_VS16 = "\ufe0f"  # the request for emoji presentation
_WAVE = "\U0001f44b"  # 👋 emoji presentation by default: two cells by the stock tables already
_DISH = "\U0001f4e1"  # 📡 a menu icon, the same
_CA = "\U0001f1e8\U0001f1e6"  # 🇨🇦 a flag: two Regional Indicators, one glyph
_CN = "\U0001f1e8\U0001f1f3"  # 🇨🇳 a second flag sharing the "C" indicator
_SUN = "☀"  # ☀ a text-default emoji, alone
_PLANE = "\U0001f6e9"  # 🛩 another, which a font draws either way
_HEART = "❤"  # ❤ another
_ROAD = "\U0001f6e3"  # 🛣 text-default too, but one of the app's own marks
_WEB = "\U0001f578"  # 🕸 the same
_BIN = "\U0001f5d1"  # 🗑 the same
_WARN = "⚠"  # ⚠ the same
_SHRUG = f"\U0001f937{_ZWJ}♂{_VS16}"  # the shrug: base, joiner, sign, selector
_FAMILY = f"\U0001f468{_ZWJ}\U0001f469{_ZWJ}\U0001f467"  # the family: three joined people
_THUMB = "\U0001f44d\U0001f3fd"  # a thumb and its skin tone, no joiner: still one glyph
_TECHIE = f"\U0001f468\U0001f3fb{_ZWJ}\U0001f4bb"  # the technologist: toned, then joined
_KEYCAP = f"1{_VS16}\u20e3"  # a keycap: a digit, a selector and the enclosing mark
_FLAG = "\U0001f3f4"  # a black flag, the base of a pirate flag
_STRANDED = f"{_FLAG}{_ZWJ}"  # a pirate flag cut after its joiner by a name's byte limit

#: Strings every authority has to agree about: emoji of every shape, the app's own marks, and the
#: scripts and marks that must come through the rule untouched.
_SAMPLES = (
    "plain ascii",
    f"Bob {_FAMILY} x",
    f"{_CA}{_CN}",
    f"{_SUN}{_VS16}{_SHRUG}",
    f"{_SUN} bare, {_PLANE} bare, {_HEART} bare",
    f"a{_ZWJ}b",
    f"{_STRANDED}  x",
    f"{_KEYCAP}!",
    f"│ {_WARN}{_VS16} warn │ {_WARN} mark │",
    f"Tech {_TECHIE} x {_THUMB}{_THUMB}",
    "漢字 names",
    "ष\u094d\u200dक",  # a Devanagari conjunct: a joiner between letters, not pictographs
    f"{_CA[0]} a lone indicator",
    "e\u0301 a combining accent",
    f"{_ROAD}{_WEB}{_BIN} ↔ ↕",
)


@pytest.fixture
def installed() -> Iterator[None]:
    """Install the reserve-two rule for one test, and put every table and cache back after."""
    from meshterm.ui.tui import colsnap
    from meshterm.ui.tui.render import _ANSI_CACHE

    saved = (
        cells._cell_len,
        cells.get_character_cell_size,
        cells.split_graphemes,
        segment.get_character_cell_size,
        ptu._CHAR_SIZES_CACHE,
        ew._INSTALLED,
        ew._CLUSTERS,
    )
    ew._INSTALLED = False
    ew.install()
    try:
        yield
    finally:
        (
            cells._cell_len,
            cells.get_character_cell_size,
            cells.split_graphemes,
            segment.get_character_cell_size,
            ptu._CHAR_SIZES_CACHE,
            ew._INSTALLED,
            ew._CLUSTERS,
        ) = saved
        cells.cached_cell_len.cache_clear()
        _ANSI_CACHE.clear()
        colsnap._CACHE.clear()


def test_whatever_may_be_drawn_as_an_emoji_is_reserved_two_cells(installed) -> None:  # noqa: ANN001
    """Every shape of emoji measures two in both authorities, whatever the stock tables said.

    The stock answers were all over the place — prompt_toolkit gave a flag four, a sun asking
    for emoji presentation one, a toned thumb four and a family six — and no answer below two
    is safe for a glyph a font may draw in two: the pin after it would overwrite its right half.
    """
    for glyph in (
        _WAVE,
        _DISH,
        _SUN,
        f"{_SUN}{_VS16}",
        _PLANE,
        f"{_PLANE}{_VS16}",
        _HEART,
        _CA,
        _CA[0],
        _SHRUG,
        _FAMILY,
        _THUMB,
        _TECHIE,
        _KEYCAP,
        f"{_WARN}{_VS16}",  # a mark asking for emoji presentation is an emoji like any other
    ):
        assert cells.cell_len(glyph) == 2, f"Rich: {glyph!r}"
        assert get_cwidth(glyph) == 2, f"prompt_toolkit: {glyph!r}"


def test_text_the_app_draws_keeps_its_stock_width(installed) -> None:  # noqa: ANN001
    """The rule reserves nothing it has no reason to: text, chrome and the app's own marks.

    The marks are text-default emoji too, but they are the app's own vocabulary, drawn in one
    cell on the terminals it is used on, so they keep the one cell every screen was laid out
    with. Rich's single-cell ranges are measured by a fast path no patch reaches, so everything
    has to agree with it there.
    """
    for text, width in (
        ("a", 1),
        ("#", 1),
        ("1", 1),
        ("é", 1),
        ("漢", 2),
        ("─", 1),
        ("⠿", 1),
        ("©", 1),
        ("▶", 1),
        (_WARN, 1),
        (_ROAD, 1),
        (_WEB, 1),
        (_BIN, 1),
        ("↕", 1),
        ("↔", 1),
        ("↩", 1),
        ("⌨", 1),
        ("⚙", 1),
        ("a\U0001f3fd", 1),  # a stray skin tone on a letter leaves it a letter
    ):
        assert cells.cell_len(text) == width, f"Rich: {text!r}"
        assert get_cwidth(text) == width, f"prompt_toolkit: {text!r}"


def test_every_authority_measures_every_string_the_same(installed) -> None:  # noqa: ANN001
    """Rich's whole-string width, its grapheme spans and prompt_toolkit's cache all agree.

    And every lone character measures the same in all of them. These are the four places a width
    is read, and a row is only laid out once if they give it one answer.
    """
    for text in _SAMPLES:
        spans, total = cells.split_graphemes(text)
        assert cells.cell_len(text) == total == get_cwidth(text), text
        assert total == sum(width for _start, _end, width in spans), text
        assert spans[0][0] == 0 and spans[-1][1] == len(text), f"spans cover {text!r}"
        for char in text:
            size = cells.get_character_cell_size(char)
            assert size == segment.get_character_cell_size(char) == get_cwidth(char), repr(char)


def test_no_prefix_outgrows_the_character_that_ends_it(installed) -> None:  # noqa: ANN001
    """The invariant Rich's segment splitter terminates on, then the splitter itself.

    ``Segment.split_cells`` steps a string a codepoint at a time until a whole-string width meets
    the cut, stepping over a two-cell character only where that character measures two on its
    own. A prefix that grew by two at a character measuring one would be a cut the walk steps past
    in both directions forever — which is why the character width is patched alongside the string
    width, and why this checks the invariant before trusting the splitter with it.
    """
    for text in _SAMPLES:
        for index, char in enumerate(text):
            grew = cells.cell_len(text[: index + 1]) - cells.cell_len(text[:index])
            assert 0 <= grew <= 2, f"{text!r} at {index}"
            if grew == 2:
                assert cells.get_character_cell_size(char) == 2, f"{text!r} at {index}"

    def split_everywhere() -> None:
        for text in _SAMPLES:
            for cut in range(cells.cell_len(text) + 1):
                segment.Segment(text).split_cells(cut)

    worker = threading.Thread(target=split_everywhere, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), "a segment split never met its cut"


def test_a_stranded_joiner_leaves_the_next_character_its_cell(installed) -> None:  # noqa: ANN001
    """A joiner with no pictograph after it joins nothing, in both authorities.

    A MeshCore name is cut at a byte limit, so a name ending on a pirate flag arrives as the
    black flag and a bare joiner, followed by the lane's padding. Rich's stock loop folded the
    padding into the flag, so the name lane measured a cell short of what the terminal drew.
    """
    padded = f"{_STRANDED}  x"  # the flag's two cells, two of padding, then the next lane
    assert cells.cell_len(padded) == 5 and get_cwidth(padded) == 5
    # A real sequence still joins: the shrug's male sign is a pictograph, not a padding space.
    assert cells.cell_len(f"{_SHRUG} x") == 4 and get_cwidth(f"{_SHRUG} x") == 4


def test_install_happens_once(installed) -> None:  # noqa: ANN001
    """A second call is a no-op: the tables patched by the first stay exactly as they are."""
    patched = (cells._cell_len, ptu._CHAR_SIZES_CACHE)
    ew.install()
    assert (cells._cell_len, ptu._CHAR_SIZES_CACHE) == patched


def test_the_app_draws_no_text_default_emoji_its_vocabulary_does_not_name() -> None:
    """Every text-default emoji the package draws is one of its own marks, and every mark is used.

    This is what keeps :data:`~meshterm.ui.tui.emoji_width._APP_TEXT_MARKS` a closed vocabulary
    rather than a list kept by care: a new one-cell mark added to a screen without being named
    there fails here, instead of quietly gaining a blank cell beside it. Prose is skipped
    (docstrings and bare strings are read, not drawn), and so is the module that defines the list.
    """
    from rich._unicode_data import load

    text_default = {
        char
        for char in load("auto").narrow_to_wide
        if not char.isascii() and char not in cells._SINGLE_CELLS
    }
    # Inputs to the PicoCalc's substitution table, which maps them to glyphs its font has. They
    # are never drawn on a terminal that shows emoji, so they are not marks.
    fold_inputs = {"↪", "✔", "✖"}

    package = pathlib.Path(ew.__file__).parents[2]
    drawn: set[str] = set()
    for path in package.rglob("*.py"):
        if path.name == "emoji_width.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        prose = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in prose:
                    drawn |= set(node.value) & text_default

    assert drawn - fold_inputs <= ew._APP_TEXT_MARKS, (
        f"name these in _APP_TEXT_MARKS: {sorted(drawn - fold_inputs - ew._APP_TEXT_MARKS)}"
    )
    assert ew._APP_TEXT_MARKS <= drawn, (
        f"no longer drawn anywhere: {sorted(ew._APP_TEXT_MARKS - drawn)}"
    )


def test_clusters_split_between_glyphs_and_never_inside_one() -> None:
    """The unit a lane may cut on and a width is measured by: one entry per glyph drawn.

    A joined run, a flag's indicator *pair*, a toned base and a keycap each come back whole,
    because each is one glyph — while a joiner that joins nothing stands on its own (see
    :func:`~meshterm.ui.tui.emoji_width._joins`), and so do letters around a joiner.
    """
    text = f"a{_FAMILY}{_CA}{_THUMB}{_KEYCAP}b"
    assert list(ew.clusters(text)) == ["a", _FAMILY, _CA, _THUMB, _KEYCAP, "b"]
    # Two flags in a row pair up one at a time rather than running together into four.
    assert list(ew.clusters(_CA + _CN)) == [_CA, _CN]
    # A name cut at its byte limit just past a joiner: the joiner is not part of the flag.
    assert list(ew.clusters(_STRANDED)) == [_FLAG, _ZWJ]
    # A joiner between letters joins nothing a font draws as one emoji.
    assert list(ew.clusters(f"a{_ZWJ}b")) == ["a", _ZWJ, "b"]


def test_cut_cells_keeps_every_glyph_whole_and_strands_no_joiner() -> None:
    """A lane's truncation lands between glyphs, so nothing is measured that is not drawn."""
    assert ew.cut_cells(f"Bob {_FAMILY}", 6) == f"Bob {_FAMILY}"  # it fits, so it is kept
    assert ew.cut_cells(f"Bob {_FAMILY}", 5) == "Bob "  # it doesn't: dropped whole
    assert ew.cut_cells(_CA, 1) == ""  # half a flag is a letter, not half a glyph
    assert ew.cut_cells(_STRANDED, 4) == _FLAG  # the joiner does not trail the cut
    assert ew.cut_cells("plain name", 5) == "plain"


def test_drawable_folds_only_what_no_terminal_can_draw() -> None:
    """A newline, an escape or a bidi override in an advert name is folded to a space.

    None of them is a width question — they are text that must not reach the terminal at all.
    The joiner is format-class too and is the one kept, because it holds an emoji together.
    """
    assert ew.drawable("line\nbreak") == "line break"
    assert ew.drawable("esc\x1b[31mape") == "esc [31mape"
    assert ew.drawable("flip\u202eme") == "flip me"
    assert ew.drawable(f"Bob {_FAMILY}{_THUMB}") == f"Bob {_FAMILY}{_THUMB}"


def test_join_clusters_merges_every_multi_codepoint_glyph() -> None:
    """The fragment merge gathers each glyph built from several codepoints into one fragment.

    prompt_toolkit's ANSI text arrives one codepoint per fragment, so a glyph is a run to be
    gathered — and every such run is merged now, not only the joined ones, because a mark
    measured one cell and followed by a selector must not be folded into a one-cell slot.
    """

    def line(text: str) -> list:
        return [("", char) for char in text]

    def texts(fragments: list) -> list:
        return [text for _style, text in fragments]

    # A line with nothing to merge is handed straight back — the same object, not a copy.
    plain = line(f"hi {_DISH} {_WARN}")
    assert ew._join_clusters(plain) is plain

    assert texts(ew._join_clusters(line(f"|{_SHRUG}|"))) == ["|", _SHRUG, "|"]
    assert texts(ew._join_clusters(line(_FAMILY))) == [_FAMILY]
    burning = f"❤{_VS16}{_ZWJ}\U0001f525"  # ❤\ufe0f\u200d🔥 a selector on the base, then a join
    assert texts(ew._join_clusters(line(burning))) == [burning]
    # A selector pair, a toned emoji and a keycap are one glyph each, merged like the rest.
    assert texts(ew._join_clusters(line(f"{_SUN}{_VS16}{_SHRUG}"))) == [f"{_SUN}{_VS16}", _SHRUG]
    assert texts(ew._join_clusters(line(f"{_WARN}{_VS16}|"))) == [f"{_WARN}{_VS16}", "|"]
    assert texts(ew._join_clusters(line(f"{_THUMB}{_KEYCAP}"))) == [_THUMB, _KEYCAP]
    # A flag's two indicators are one glyph, written as one: a cursor pin between the halves
    # would leave a terminal drawing two letters instead of a flag.
    assert texts(ew._join_clusters(line(f"|{_CA}|"))) == ["|", _CA, "|"]
    assert texts(ew._join_clusters(line(f"{_CA}{_CN}"))) == [_CA, _CN]
    # A lone indicator is no pair, and a trailing or stranded joiner joins nothing.
    assert texts(ew._join_clusters(line(f"{_CA[0]} x"))) == [_CA[0], " ", "x"]
    assert texts(ew._join_clusters(line(f"a{_ZWJ}"))) == ["a", _ZWJ]
    assert texts(ew._join_clusters(line(f"{_STRANDED} x"))) == [_FLAG, _ZWJ, " ", "x"]

    # A merged fragment iterates as the whole glyph, which is what makes prompt_toolkit build
    # one Char of it instead of one per codepoint.
    (merged,) = texts(ew._join_clusters(line(_SHRUG)))
    assert list(merged) == [_SHRUG] and merged == _SHRUG


def test_cluster_control_lays_each_glyph_into_a_single_screen_cell(installed) -> None:  # noqa: ANN001
    """The delivery half: one two-cell ``Char`` per glyph, every codepoint still written out.

    Measuring a glyph right is not enough on its own — prompt_toolkit lays out one codepoint at a
    time, so an unmerged shrug puts its male sign in a cell of its own, and a warning mark with a
    selector becomes a two-cell glyph in a one-cell slot. Merged, each is one ``Char`` two cells
    wide, and the text after lands where the pins will put it.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition
    from prompt_toolkit.output import DummyOutput

    warn = f"{_WARN}{_VS16}"
    text = f"|{_SHRUG}|{_FAMILY}|{warn}|end"
    window = Window(ew.ClusterTextControl(lambda: ANSI(text)), always_hide_cursor=True)
    app = Application(layout=Layout(window), input=DummyInput(), output=DummyOutput())
    with set_app(app):
        screen = Screen(default_char=None, initial_width=40, initial_height=1)
        window.write_to_screen(screen, MouseHandlers(), WritePosition(0, 0, 40, 1), "", False, None)
    row = screen.data_buffer[0]

    assert (row[1].char, row[1].width) == (_SHRUG, 2)
    assert (row[4].char, row[4].width) == (_FAMILY, 2)
    assert (row[7].char, row[7].width) == (warn, 2)
    assert "".join(row[x].char for x in range(9, 13)) == "|end"
    # Nothing was dropped on the way: the row still spells the source exactly.
    assert "".join(row[x].char for x in range(40)).rstrip() == text
