# SPDX-License-Identifier: Apache-2.0
"""Terminal-aligned emoji width: VS16 sequences and curated lone-codepoint emoji.

The border-alignment story lives in :mod:`meshterm.ui.tui.emoji_width`. These tests pin the
two things that make a chat row with ``👋`` frame flush: Rich must *measure* the glyph as one
cell (so it pads the row to the right width) and prompt_toolkit must *count* it as one (so it
places the panel's right border where the terminal actually draws it). An emoji the terminal
draws two wide — a menu icon like ``📡``, never on the allowlist — must stay two in both.

The factory helpers (``_make_cell_len``, ``_make_pt_cache``) are exercised in isolation so no
process-global width table is touched; the two :func:`calibrate` tests that do patch the real
tables snapshot and restore them in a ``finally`` so nothing leaks into a later test.
"""

from __future__ import annotations

import prompt_toolkit.utils as ptu
import rich.cells as cells

from meshterm.ui.tui import emoji_width as ew

# One confirmed narrow glyph, plus one the same terminal draws two wide, used throughout.
_WAVE = "👋"  # the confirmed lone-codepoint emoji this terminal draws in one cell
_DISH = "📡"  # a menu icon the terminal draws two wide — must never be narrowed
_CA = "🇨🇦"  # a flag: two Regional Indicators; prompt_toolkit miscounts it as four cells
_CN = "🇨🇳"  # a second flag sharing the "C" indicator — the whole category must be handled
_PLANE = "🛩️"  # a VS16 sequence this terminal draws two wide despite the narrow-VS16 verdict
_PLANE_BASE = "\U0001f6e9"  # its base codepoint (no selector) — what the wide set is keyed on
_ROAD = "\U0001f6e3"  # 🛣 a bare text-default emoji: one cell, and both authorities agree
_WEB = "\U0001f578"  # 🕸 its untouched sibling one Trophy board down — the control
_SHRUG = "\U0001f937‍♂️"  # 🤷‍♂️ a ZWJ sequence: base, joiner, male sign, selector — one glyph
_FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"  # 👨‍👩‍👧 three joined people, still one glyph
_ZWJ = "‍"  # the joiner itself: the tell that its neighbours are a single glyph
_THUMB = "\U0001f44d\U0001f3fd"  # 👍🏽 a base and its skin tone, no joiner — still one glyph
_TECHIE = "\U0001f468\U0001f3fb‍\U0001f4bb"  # 👨🏻‍💻 a toned base, then a joined laptop
_FLAG = "\U0001f3f4"  # 🏴 a black flag, the base of the pirate flag below
_STRANDED = f"{_FLAG}‍"  # 🏴‍☠️ cut after its joiner by a name's byte limit: joins nothing


def test_narrow_lone_set_defaults_extends_and_disables(monkeypatch) -> None:
    """The allowlist seeds to the confirmed glyph; the env var overrides it outright."""
    monkeypatch.delenv("MESHTERM_NARROW_EMOJI", raising=False)
    assert _WAVE in ew._narrow_lone_set()

    monkeypatch.setenv("MESHTERM_NARROW_EMOJI", "👋🤙")
    assert {"👋", "🤙"} <= ew._narrow_lone_set()

    # Empty string is the off switch — a way back to Rich/pt defaults with no code change.
    monkeypatch.setenv("MESHTERM_NARROW_EMOJI", "")
    assert ew._narrow_lone_set() == frozenset()


def test_wide_base_set_defaults_extends_and_disables(monkeypatch) -> None:
    """The wide set seeds to the confirmed bases; the env var overrides it outright."""
    monkeypatch.delenv("MESHTERM_WIDE_EMOJI", raising=False)
    # One source only: a VS16 sequence the narrow verdict gets wrong. A *bare* codepoint both
    # authorities already measure at one is not a candidate — see test_bare_text_default_emoji.
    assert _PLANE_BASE in ew._wide_base_set()
    assert not {_ROAD, _WEB} & ew._wide_base_set()

    # ✈ (U+2708) is a second VS16 airplane base; both list cleanly.
    monkeypatch.setenv("MESHTERM_WIDE_EMOJI", "\U0001f6e9✈")
    assert {"\U0001f6e9", "✈"} <= ew._wide_base_set()

    # Pasting the whole rendered glyph keeps the base but strips the zero-width selector, so
    # the selector is never itself counted as a wide cell.
    monkeypatch.setenv("MESHTERM_WIDE_EMOJI", _PLANE)
    wide = ew._wide_base_set()
    assert _PLANE_BASE in wide and "️" not in wide

    # Empty string trusts the narrow-VS16 verdict for every sequence.
    monkeypatch.setenv("MESHTERM_WIDE_EMOJI", "")
    assert ew._wide_base_set() == frozenset()


def test_rich_cell_len_narrows_only_allowlisted_lone_emoji() -> None:
    """Rich's replacement measures a listed lone emoji as one, an unlisted one still as two."""
    cell_len = ew._make_cell_len(frozenset(_WAVE), frozenset(_PLANE_BASE))

    assert cell_len(_WAVE) == 1
    assert cell_len(_DISH) == 2  # unlisted: the terminal draws it wide, so leave it wide
    assert cell_len("Bob 👋 hi") == 8  # the narrowed glyph flows through a whole line
    # The pre-existing VS16 handling still applies: a narrow base is measured alone, its
    # variation selector skipped, so "☀️" stays one cell rather than being promoted to two.
    assert cell_len("☀️") == 1
    # A wide-VS16 exception is forced back to two, selector still skipped, so the airplane
    # frames flush instead of collapsing to one and smearing the row.
    assert cell_len(_PLANE) == 2
    assert cell_len("hi 🛩️") == len("hi ") + 2
    # A ZWJ sequence is one glyph, measured as its base: the codepoints the joiner folds in
    # cost nothing, however many of them there are. Rich's stock loop already does this, and
    # the selector-skipping replacement must not lose it — counting the male sign as a cell
    # of its own is what pulls the row's right border a column in.
    assert cell_len(_SHRUG) == 2
    assert cell_len(_FAMILY) == 2
    assert cell_len(f"Bob {_FAMILY} hi") == len("Bob  hi") + 2


def test_pt_cache_narrows_only_allowlisted_lone_emoji() -> None:
    """prompt_toolkit's cache — the authority that places the border — matches Rich."""
    cache = ew._make_pt_cache(frozenset(_WAVE), frozenset(_PLANE_BASE))

    assert cache[_WAVE] == 1
    assert cache[_DISH] == 2
    # The base cache sums a multi-char string per character through the cache, so the
    # one-cell wave is inherited by any line that contains it.
    assert cache["Bob 👋"] == 5
    # The wide-VS16 airplane is the reverse: its base is forced to two, the selector stays
    # zero, so the whole glyph (and any line holding it) keeps the terminal's two cells.
    assert cache[_PLANE] == 2
    assert cache["🛩️ hi"] == 2 + len(" hi")
    # The sequences prompt_toolkit added up part by part: three cells for the shrug and six
    # for the family, each of them one two-cell glyph, each pulling a border in by the excess.
    assert cache[_SHRUG] == 2
    assert cache[_FAMILY] == 2
    assert cache[f"Bob {_FAMILY} hi"] == len("Bob  hi") + 2
    # A lone joiner is still zero, and a codepoint a sequence joins keeps its own width when
    # it stands alone — ``↕`` is the reorder icon, not part of anything.
    assert cache[_ZWJ] == 0
    assert cache["↕"] == 1


def test_bare_text_default_emoji_are_left_exactly_as_measured() -> None:
    """An emoji outside Emoji_Presentation, drawn *bare*, is one cell — and stays one.

    ``🛣`` and ``🕸`` have East-Asian width Neutral, so Rich and wcwidth both measure them at
    one; with no variation selector asking for emoji presentation, the font draws a one-cell
    text glyph, which is exactly what they said. Both authorities already agreeing is not a
    bug to correct: listing ``🛣`` in the wide set made the Trophy case's Longest-distance
    heading reserve a cell the terminal never drew and pulled that row's border a column in,
    while ``🕸`` — same class, one board down, never listed — framed flush throughout.
    """
    cell_len = ew._make_cell_len(frozenset(_WAVE), ew._wide_base_set())
    cache = ew._make_pt_cache(frozenset(_WAVE), ew._wide_base_set())
    for bare in (_ROAD, _WEB):
        assert cell_len(bare) == 1 and cache[bare] == 1
    # A heading built around one costs its own cells and no more, so the row frames flush.
    caption = cell_len(f"── {_ROAD} Longest distance ──")
    assert caption == len("── ") + 1 + len(" Longest distance ──")
    # The Trophy case's *visible* gap after the road is not this module's business and must
    # not become it: the heading pads the mark out to the widest of the seven disciplines
    # (records_screen.discipline_label), which costs a real cell the terminal advances over
    # — where widening the glyph here would claim one it does not.


def test_flags_measure_two_cells_in_both_authorities() -> None:
    """A country flag measures two cells in both width authorities.

    A flag is a Regional Indicator pair: prompt_toolkit's wcwidth calls it four cells,
    while Rich and the terminal draw it as one two-cell glyph. Narrowing the
    indicators as a category makes every flag sum to two in both — no per-country
    allowlist entry, and flags sharing an indicator (🇨🇦 / 🇨🇳) are fixed at once.
    """
    # Only the lone-emoji allowlist is passed; flags are handled by category, not by listing.
    cell_len = ew._make_cell_len(frozenset(_WAVE), frozenset())
    cache = ew._make_pt_cache(frozenset(_WAVE), frozenset())

    for flag in (_CA, _CN):
        assert cell_len(flag) == 2
        assert cache[flag] == 2  # was 4 unpatched: two indicators at wcwidth 2 each

    # A flag rides a chat line without dragging the border: "eh? 🇨🇦" measures its plain
    # cells plus the flag's two.
    assert cell_len("eh 🇨🇦") == len("eh ") + 2


def _snapshot() -> tuple:
    """Capture the mutable width state :func:`calibrate` patches, to restore afterwards."""
    return (cells._cell_len, ptu._CHAR_SIZES_CACHE, ew._CALIBRATED, ew._CLUSTERS)


def _restore(snap: tuple) -> None:
    """Put the width authorities (and the once-only flag) back, clearing Rich's memo cache."""
    cells._cell_len, ptu._CHAR_SIZES_CACHE, ew._CALIBRATED, ew._CLUSTERS = snap
    cells.cached_cell_len.cache_clear()


def test_calibrate_width1_narrows_the_wave_in_both_authorities(monkeypatch) -> None:
    """A renderer that draws emoji narrow gets both Rich and pt aligned to the terminal."""
    monkeypatch.delenv("MESHTERM_NARROW_EMOJI", raising=False)
    monkeypatch.delenv("MESHTERM_WIDE_EMOJI", raising=False)
    from prompt_toolkit.utils import get_cwidth

    snap = _snapshot()
    try:
        ew._CALIBRATED = False
        ew.calibrate(force_width=1)
        assert cells.cell_len(_WAVE) == 1
        assert cells.cell_len(_DISH) == 2  # unlisted icon stays wide
        assert cells.cell_len(_CA) == 2  # flag handled by category, no allowlist entry
        assert cells.cell_len(_PLANE) == 2  # wide-VS16 exception carved back out of the narrowing
        assert cells.cell_len(_ROAD) == 1  # bare text-default emoji: left exactly as measured
        assert cells.cell_len(_WEB) == 1
        assert get_cwidth(_WAVE) == 1  # pt now places the border a cell earlier
        assert get_cwidth(_DISH) == 2
        assert get_cwidth(_CA) == 2  # was 4 unpatched
        assert get_cwidth(_PLANE) == 2  # was 1 unpatched: the airplane smeared a cell short
        assert get_cwidth(_ROAD) == 1 and get_cwidth(_WEB) == 1  # untouched, in both authorities
        assert cells.cell_len(_SHRUG) == 2 and get_cwidth(_SHRUG) == 2  # pt counted three
        assert cells.cell_len(_FAMILY) == 2 and get_cwidth(_FAMILY) == 2  # pt counted six
    finally:
        _restore(snap)


def test_calibrate_width2_narrows_nothing_but_still_joins_clusters(monkeypatch) -> None:
    """A width-2 terminal narrows nothing, but still gets the cluster join.

    Narrowing would itself break the border there — but a joined sequence is
    over-measured at *every* width, so that one correction still lands.

    prompt_toolkit sums a ZWJ sequence's codepoints wherever it runs, which no terminal draws:
    the family is one two-cell glyph on the widest terminal as surely as on the narrowest. So
    the cluster rule is installed on its own here, and nothing else is — the curated sets and
    the flag category stay unconfirmed on this renderer, and Rich is not touched at all.
    """
    monkeypatch.delenv("MESHTERM_NARROW_EMOJI", raising=False)
    from prompt_toolkit.utils import get_cwidth

    snap = _snapshot()
    try:
        ew._CALIBRATED = False
        ew.calibrate(force_width=2)
        assert cells.cell_len(_WAVE) == 2
        assert get_cwidth(_WAVE) == 2
        # Rich untouched: it still promotes a VS16 sequence to the two cells this terminal draws.
        assert cells.cell_len(_PLANE) == 2 and cells.cell_len("☀️") == 2
        # Neither curated set nor the flag category applies here — pt keeps its stock answers.
        assert get_cwidth("☀️") == 1
        assert get_cwidth(_CA) == 4
        # The one correction that holds at either width.
        assert get_cwidth(_SHRUG) == 2  # was 3
        assert get_cwidth(_FAMILY) == 2  # was 6
        assert get_cwidth(_TECHIE) == 2  # was 6: a skin tone is no cell, at either width
    finally:
        _restore(snap)


def test_a_skin_tone_is_part_of_its_glyph_in_both_authorities() -> None:
    """A skin-tone modifier recolours the emoji before it and takes no cell of its own.

    wcwidth counts each modifier as two, so prompt_toolkit reserved four cells for ``👍🏽`` and
    for the joined ``👨🏻‍💻`` — a two-cell notch in every row naming such a node, and every lane
    after the name drawn two columns off. Rich's table already says zero; the cache now agrees,
    on both calibration paths.
    """
    for narrow, flags in ((frozenset(_WAVE), True), (frozenset(), False)):
        cache = ew._make_pt_cache(narrow, frozenset(), flags=flags)
        assert cache[_THUMB] == 2
        assert cache[_TECHIE] == 2
        assert cache[f"Tech {_TECHIE} x"] == len("Tech ") + 2 + len(" x")
    assert ew._make_cell_len(frozenset(_WAVE), frozenset())(_TECHIE) == 2


def test_a_stranded_joiner_leaves_the_next_character_its_cell(monkeypatch) -> None:
    """A joiner with no pictograph after it joins nothing, in both authorities and both paths.

    A MeshCore name is cut at a byte limit, so ``That's So Fetch 🏴‍☠️`` arrives as the flag and
    a bare joiner. Both authorities folded whatever came next into the flag — the padding after
    the name — so the name lane measured a cell short and every lane after it began a column
    before the terminal drew it.
    """
    monkeypatch.delenv("MESHTERM_NARROW_EMOJI", raising=False)
    padded = f"{_STRANDED}  x"  # the flag's two cells, two of padding, then the next lane
    cell_len = ew._make_cell_len(frozenset(_WAVE), frozenset())
    assert cell_len(padded) == 5
    assert ew._make_pt_cache(frozenset(_WAVE), frozenset())[padded] == 5
    # A real sequence still joins: the shrug's male sign is a pictograph, not a padding space.
    assert cell_len(f"{_SHRUG} x") == 4

    snap = _snapshot()
    try:
        ew._CALIBRATED = False
        ew.calibrate(force_width=2)
        # Rich's own loop, trusted on this path, skipped the space and counted four.
        assert cells.cell_len(padded) == 5
        assert cells.cell_len(_SHRUG) == 2 and cells.cell_len(_FAMILY) == 2
    finally:
        _restore(snap)


def test_clusters_split_between_glyphs_and_never_inside_one() -> None:
    """The unit a lane may cut on: one entry per glyph the terminal actually draws.

    A joined run, a flag's indicator *pair*, and a toned base each come back whole, because
    each is one glyph — while a joiner that joins nothing stands on its own, as it does
    everywhere else in this module (see :func:`~meshterm.ui.tui.emoji_width._joins`).
    """
    assert list(ew.clusters(f"a{_FAMILY}{_CA}{_THUMB}b")) == ["a", _FAMILY, _CA, _THUMB, "b"]
    # Two flags in a row pair up one at a time rather than running together into four.
    assert list(ew.clusters(_CA + _CN)) == [_CA, _CN]
    # A name cut at its byte limit just past a joiner: the joiner is not part of the flag.
    assert list(ew.clusters(_STRANDED)) == [_FLAG, _ZWJ]


def test_cut_cells_keeps_every_glyph_whole_and_strands_no_joiner() -> None:
    """A lane's truncation lands between glyphs, so nothing measures what is not drawn.

    The failure this exists for: cutting a codepoint at a time leaves a trailing joiner,
    which folds whatever the caller appends — the lane's ellipsis — into the glyph before
    it. The ellipsis then measures nothing while the terminal still draws it, and every
    column right of the name starts a cell late.
    """
    assert ew.cut_cells(f"Bob {_FAMILY}", 6) == f"Bob {_FAMILY}"  # it fits, so it is kept
    assert ew.cut_cells(f"Bob {_FAMILY}", 5) == "Bob "  # it doesn't: dropped whole
    assert ew.cut_cells(_CA, 1) == ""  # half a flag is a letter, not half a glyph
    assert ew.cut_cells(_STRANDED, 4) == _FLAG  # the joiner cannot swallow what follows
    assert ew.cut_cells("plain name", 5) == "plain"


def test_drawable_folds_only_what_no_terminal_can_draw() -> None:
    """A newline, an escape or a bidi override in an advert name is folded to a space.

    None of them is a width bug an allowlist could fix — they are text that must not reach
    the terminal at all. The joiner is format-class too and is the one kept, because it is
    what holds an emoji sequence together.
    """
    assert ew.drawable("line\nbreak") == "line break"
    assert ew.drawable("esc\x1b[31mape") == "esc [31mape"
    assert ew.drawable("flip‮me") == "flip me"
    assert ew.drawable(f"Bob {_FAMILY}{_THUMB}") == f"Bob {_FAMILY}{_THUMB}"


def test_join_zwj_clusters_merges_only_joined_runs() -> None:
    """The fragment merge gathers a whole sequence and leaves everything else alone.

    prompt_toolkit's ANSI text arrives one codepoint per fragment, so a sequence is a run:
    a base, an optional selector, then *joiner + component + optional selector* groups.
    """

    def line(text: str) -> list:
        return [("", char) for char in text]

    def texts(fragments: list) -> list:
        return [text for _style, text in fragments]

    # A line with no joiner is handed straight back — the same object, not a copy.
    plain = line("hi 📡")
    assert ew._join_zwj_clusters(plain) is plain

    # The shrug's four fragments become one; the bars around it are untouched.
    assert texts(ew._join_zwj_clusters(line(f"|{_SHRUG}|"))) == ["|", _SHRUG, "|"]
    # Three joined people, five fragments, still one glyph.
    assert texts(ew._join_zwj_clusters(line(_FAMILY))) == [_FAMILY]
    # A selector on the *base*, before the joiner, belongs to the run: ❤️‍🔥.
    burning = "❤️‍\U0001f525"
    assert texts(ew._join_zwj_clusters(line(burning))) == [burning]
    # A bare VS16 pair is not a sequence — prompt_toolkit already folds it into the cell
    # before it, so those fragments are left exactly as they came.
    assert texts(ew._join_zwj_clusters(line(f"☀️{_SHRUG}"))) == ["☀", "️", _SHRUG]
    # A trailing joiner with nothing to join is not a sequence either.
    assert texts(ew._join_zwj_clusters(line(f"a{_ZWJ}"))) == ["a", _ZWJ]
    # A skin tone on the base belongs to the run the way its selector does.
    assert texts(ew._join_zwj_clusters(line(f"|{_TECHIE}|"))) == ["|", _TECHIE, "|"]
    # A stranded joiner, followed by the name lane's padding, is not a sequence: the space
    # after it keeps a fragment — and so a cell — of its own.
    assert texts(ew._join_zwj_clusters(line(f"{_STRANDED} x"))) == [_FLAG, _ZWJ, " ", "x"]

    # A merged fragment iterates as the whole sequence, which is what makes prompt_toolkit
    # build one Char of it instead of one per codepoint.
    (merged,) = texts(ew._join_zwj_clusters(line(_SHRUG)))
    assert list(merged) == [_SHRUG] and merged == _SHRUG


def test_cluster_control_lays_a_sequence_into_a_single_screen_cell(monkeypatch) -> None:
    """The delivery half: one ``Char`` per glyph, with every codepoint still written out.

    Measuring the sequence right is not enough on its own — prompt_toolkit lays out one
    codepoint at a time, so an uncorrected screen puts the male sign in a cell of its own and
    everything after it a column late. The control merges the run, so the window makes a
    single two-cell ``Char`` of it and the text after lands where the terminal draws it. The
    codepoints themselves are untouched: all four still reach the screen, in order, so the
    terminal composes the same glyph.
    """
    monkeypatch.delenv("MESHTERM_NARROW_EMOJI", raising=False)
    monkeypatch.delenv("MESHTERM_WIDE_EMOJI", raising=False)
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition
    from prompt_toolkit.output import DummyOutput

    text = f"|{_SHRUG}|{_FAMILY}|end"

    def paint() -> list:
        window = Window(ew.ClusterTextControl(lambda: ANSI(text)), always_hide_cursor=True)
        app = Application(layout=Layout(window), input=DummyInput(), output=DummyOutput())
        with set_app(app):
            screen = Screen(default_char=None, initial_width=40, initial_height=1)
            window.write_to_screen(
                screen, MouseHandlers(), WritePosition(0, 0, 40, 1), "", False, None
            )
        return screen.data_buffer[0]

    snap = _snapshot()
    try:
        ew._CALIBRATED = False
        ew.calibrate(force_width=1)
        row = paint()
        # Each sequence is one cell-occupying Char, two cells wide, exactly as drawn.
        assert (row[1].char, row[1].width) == (_SHRUG, 2)
        assert (row[4].char, row[4].width) == (_FAMILY, 2)
        # So the text after them starts at column 6 rather than 13 — seven cells of border
        # drift removed (one for the shrug, four for the family, two from the second bar).
        assert "".join(row[x].char for x in range(6, 10)) == "|end"
        # Nothing was dropped on the way: the row still spells the source exactly.
        assert "".join(row[x].char for x in range(40)).rstrip() == text
    finally:
        _restore(snap)
