"""Shared menu-chrome tests: the furniture every select list builds the same way.

These pin the app's exit-affordance and lane conventions at their source, so a screen
that builds its rows through :mod:`meshterm.ui.menus` inherits the standards and a
regression here fails once rather than on every screen.
"""

from __future__ import annotations

from rich.text import Text

from meshterm.ui.menus import (
    Lane,
    back_rows,
    changes_phrase,
    column_header,
    exit_rows,
    fit_cells,
    lane_header,
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


def test_column_header_lays_each_label_over_its_lane() -> None:
    """Lanes pad to their own width under the pointer indent, so labels sit over columns."""
    header = column_header([Lane("NAME", 8), Lane("KEY", 6), Lane("HEARD")], 40)
    assert header == "  NAME    KEY   HEARD"


def test_column_header_abbreviates_from_the_right_to_fit() -> None:
    """Too narrow for the full labels, lanes give their shorter forms — never a wrap."""
    from rich.cells import cell_len

    lanes = [Lane("SETTING", 10), Lane(("DESCRIPTION", "DESC", "?"))]
    assert column_header(lanes, 40) == "  SETTING   DESCRIPTION"  # room for the full word
    assert column_header(lanes, 20) == "  SETTING   DESC"  # one step shorter, and it fits
    assert column_header(lanes, 13) == "  SETTING   ?"  # the last form still fits
    # Nothing left to give: the line crops rather than wrapping onto a second row.
    tight = column_header(lanes, 10)
    assert cell_len(tight) == 10 and tight.endswith("…") and "\n" not in tight


def test_column_header_only_shortens_the_lanes_it_has_to() -> None:
    """A lane keeps its full label while a lane to its right can still give cells back."""
    from rich.cells import cell_len

    lanes = [Lane(("CONVERSATION", "CHAT"), 14), Lane(("LAST MESSAGE", "LAST MSG", "LAST"))]
    assert column_header(lanes, 24) == "  CONVERSATION  LAST MSG"
    assert column_header(lanes, 20) == "  CONVERSATION  LAST"
    # Only once the right-hand lane is spent does the left one abbreviate — and a line that
    # still cannot fit crops (its padded lanes have no cells to give), never wraps.
    tight = column_header(lanes, 14)
    assert tight.startswith("  CHAT") and tight.endswith("…") and cell_len(tight) == 14


def test_lane_header_heads_the_editor_lanes_and_shortens_description() -> None:
    """The shared editor header matches lane_row's lanes and abbreviates the last label."""
    from meshterm.ui.menus import lane_row

    row = lane_row("Node name", Text("MockCompanion"), "Advertised name", 12, 20)
    header = lane_header(12, 20, 80)
    assert header.startswith("  SETTING")
    assert header.index("VALUE") == 2 + 12 + 2  # the pointer indent, then the label lane
    assert header.index("DESCRIPTION") == 2 + 12 + 2 + 20 + 2
    # The row's own lanes start where the header's labels do (both padded the same way,
    # the header offset by the pointer column the rows draw for themselves).
    assert row.plain.index("MockCompanion") == header.index("VALUE") - 2
    assert row.plain.index("Advertised name") == header.index("DESCRIPTION") - 2
    assert lane_header(12, 20, 44).endswith("DESC")  # no room for the word — abbreviate


def test_fit_cells_measures_display_cells_not_characters() -> None:
    """Padding and truncation count display cells, so wide glyphs can't skew lanes."""
    from rich.cells import cell_len

    assert fit_cells("abc", 5) == "abc  "
    assert cell_len(fit_cells("日本語の名前", 5)) == 5  # wide chars: truncated by cells
    assert fit_cells("abcdef", 5).endswith("…")
    assert fit_cells("ab", 5, align="right") == "   ab"


def test_main_menu_sections_answer_the_menus_own_question() -> None:
    """Each section is a *doing*, in workflow order, and holds only what shares it.

    The menu asks "What would you like to do?", so the grouping is by verb rather than
    by subject: the address book sits with the features that message from it, the
    recorded history with the live views it is the past tense of, the two walks with the
    topology they walk over, and a setting lands under the scope it changes — this radio,
    or someone else's over the mesh.

    About MeshTerm is the deliberate exception, and the reason it sorts last: its pages
    do nothing at all, so it names a subject and stays out of the workflow the five
    doings above it describe.
    """
    from meshterm.tools import load_all_tools
    from meshterm.tools.base import _CATEGORY_ORDER, all_tools

    load_all_tools()
    sections: dict[str, list[str]] = {}
    for tool in all_tools():
        if tool.menu_visible:
            sections.setdefault(tool.category, []).append(tool.name)

    assert list(sections) == _CATEGORY_ORDER  # all_tools already sorts them this way
    assert sections == {
        "Message": ["chat", "channels", "courier", "contacts"],
        "Watch": ["dashboard", "livefeed", "watchtower", "timemachine"],
        "Explore": ["map", "walk", "trace", "trace-path", "records"],
        "This node": ["info", "config", "device-actions", "advert"],
        "Other nodes": ["repeater-admin", "tx-optimize"],
        "About MeshTerm": ["about", "about-author", "support"],
    }
    # No section is so big it stops being a grouping (the old Mesh bucket held seven of
    # nineteen rows), and none is a bucket of one.
    assert all(2 <= len(names) <= 5 for names in sections.values())
