"""Shared menu-chrome tests: the furniture every select list builds the same way.

These pin the app's exit-affordance and lane conventions at their source, so a screen
that builds its rows through :mod:`meshterm.ui.menus` inherits the standards and a
regression here fails once rather than on every screen.
"""

from __future__ import annotations

from rich.text import Text

from meshterm.ui.menus import (
    back_rows,
    changes_phrase,
    exit_rows,
    fit_cells,
    menu_rows,
    section_heading,
)
from meshterm.ui.tui import Choice, Separator


def test_back_rows_are_one_blank_line_then_bare_back() -> None:
    """The exit group is exactly: a blank separator, then the unadorned word Back."""
    rows = back_rows("sentinel")
    assert len(rows) == 2
    assert isinstance(rows[0], Separator) and rows[0].title == " "
    assert isinstance(rows[1], Choice)
    assert rows[1].title == "Back"  # no arrow, no icon — the app-wide exit word
    assert rows[1].value == "sentinel"


def test_exit_rows_clean_state_is_plain_back() -> None:
    """With nothing staged the editor exit group collapses to the plain Back group."""
    rows = exit_rows(0, apply_value="A", back_value="B")
    assert [type(r) for r in rows] == [Separator, Choice]
    assert rows[1].title == "Back" and rows[1].value == "B"


def test_exit_rows_staged_state_spells_out_the_consequence() -> None:
    """Staged changes produce ✓ Apply above ✗ Back — discard, after one blank line."""
    rows = exit_rows(3, apply_value="A", back_value="B")
    assert isinstance(rows[0], Separator) and rows[0].title == " "
    apply_row, back_row = rows[1], rows[2]
    assert apply_row.value == "A" and back_row.value == "B"
    assert apply_row.title.plain == "✓ Apply 3 staged changes"
    assert back_row.title.plain == "✗ Back — discard staged changes"


def test_menu_rows_align_descriptions_in_display_cells() -> None:
    """Every description starts at the same cell column, wide emoji labels included."""
    rows = menu_rows(
        [
            ("🔄 Reboot device…", "Restart it", 1),  # emoji = 2 cells
            ("Plain", "Second", 2),
        ]
    )
    from rich.cells import cell_len

    starts = {cell_len(r.title.plain[: r.title.plain.index(d)]) for r, d in
              zip(rows, ["Restart it", "Second"])}
    assert len(starts) == 1  # one shared description column


def test_menu_rows_keep_a_styled_label_styled() -> None:
    """A Text label (an err-tinted destructive row) keeps its spans in the built row."""
    rows = menu_rows([(Text("⚠ Danger", style="err"), "Careful", "x")])
    title = rows[0].title
    assert title.plain.startswith("⚠ Danger")
    assert any(span.style == "err" for span in title.spans)


def test_section_heading_wears_the_dashes_and_accent() -> None:
    """Grouped-list headings read ── Label ── in the accent style, everywhere."""
    sep = section_heading("Outbox")
    assert sep.title == "── Outbox ──"
    assert sep.style == "accent"


def test_changes_phrase_pluralizes() -> None:
    assert changes_phrase(1) == "1 staged change"
    assert changes_phrase(2) == "2 staged changes"


def test_fit_cells_measures_display_cells_not_characters() -> None:
    """Padding and truncation count display cells, so wide glyphs can't skew lanes."""
    from rich.cells import cell_len

    assert fit_cells("abc", 5) == "abc  "
    assert cell_len(fit_cells("日本語の名前", 5)) == 5  # wide chars: truncated by cells
    assert fit_cells("abcdef", 5).endswith("…")
    assert fit_cells("ab", 5, align="right") == "   ab"
