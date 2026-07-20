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


def test_narrow_lone_set_defaults_extends_and_disables(monkeypatch) -> None:
    """The allowlist seeds to the confirmed glyph; the env var overrides it outright."""
    monkeypatch.delenv("MESHTERM_NARROW_EMOJI", raising=False)
    assert _WAVE in ew._narrow_lone_set()

    monkeypatch.setenv("MESHTERM_NARROW_EMOJI", "👋🤙")
    assert {"👋", "🤙"} <= ew._narrow_lone_set()

    # Empty string is the off switch — a way back to Rich/pt defaults with no code change.
    monkeypatch.setenv("MESHTERM_NARROW_EMOJI", "")
    assert ew._narrow_lone_set() == frozenset()


def test_wide_vs16_set_defaults_extends_and_disables(monkeypatch) -> None:
    """The wide set seeds to the confirmed airplane base; the env var overrides it outright."""
    monkeypatch.delenv("MESHTERM_WIDE_EMOJI", raising=False)
    assert _PLANE_BASE in ew._wide_vs16_set()

    # ✈ (U+2708) is a second VS16 airplane base; both list cleanly.
    monkeypatch.setenv("MESHTERM_WIDE_EMOJI", "\U0001f6e9✈")
    assert {"\U0001f6e9", "✈"} <= ew._wide_vs16_set()

    # Pasting the whole rendered glyph keeps the base but strips the zero-width selector, so
    # the selector is never itself counted as a wide cell.
    monkeypatch.setenv("MESHTERM_WIDE_EMOJI", _PLANE)
    wide = ew._wide_vs16_set()
    assert _PLANE_BASE in wide and "️" not in wide

    # Empty string trusts the narrow-VS16 verdict for every sequence.
    monkeypatch.setenv("MESHTERM_WIDE_EMOJI", "")
    assert ew._wide_vs16_set() == frozenset()


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


def test_flags_measure_two_cells_in_both_authorities() -> None:
    """A country flag is a Regional Indicator pair: prompt_toolkit's wcwidth calls it four
    cells, Rich and the terminal draw it as one two-cell glyph. Narrowing the indicators as a
    category makes every flag sum to two in both authorities — no per-country allowlist entry,
    and flags sharing an indicator (🇨🇦 / 🇨🇳) are all fixed at once."""
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
    return (cells._cell_len, ptu._CHAR_SIZES_CACHE, ew._CALIBRATED)


def _restore(snap: tuple) -> None:
    """Put the width authorities (and the once-only flag) back, clearing Rich's memo cache."""
    cells._cell_len, ptu._CHAR_SIZES_CACHE, ew._CALIBRATED = snap
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
        assert get_cwidth(_WAVE) == 1  # pt now places the border a cell earlier
        assert get_cwidth(_DISH) == 2
        assert get_cwidth(_CA) == 2  # was 4 unpatched
        assert get_cwidth(_PLANE) == 2  # was 1 unpatched: the airplane smeared a cell short
    finally:
        _restore(snap)


def test_calibrate_width2_leaves_both_authorities_untouched(monkeypatch) -> None:
    """On a terminal that draws emoji two wide, narrowing would itself break the border."""
    monkeypatch.delenv("MESHTERM_NARROW_EMOJI", raising=False)
    from prompt_toolkit.utils import get_cwidth

    snap = _snapshot()
    try:
        ew._CALIBRATED = False
        ew.calibrate(force_width=2)
        assert cells.cell_len(_WAVE) == 2
        assert get_cwidth(_WAVE) == 2
    finally:
        _restore(snap)
