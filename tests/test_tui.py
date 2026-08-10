"""Unit tests for the reusable text-UI library (``meshterm.ui.tui``).

These exercise the pure logic — ANSI rendering/slicing, selection filtering and navigation,
scroll math, the frame composition's terminal-fit guarantee, prompt editing/validation, and
the progress handle's Rich-``Progress`` parity — without standing up a real prompt_toolkit
application, so they run fast and headless.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from meshterm import copyright_notice
from tests.conftest import plain as _plain
from meshterm.ui.menus import section_heading
from meshterm.ui.pathline import CRACK_TAIL, PathHop, PathLine
from meshterm.ui.tui import frame, glow
from meshterm.ui.tui.glow import apply_corner_glow
from meshterm.ui.tui.progress import ProgressScreen
from meshterm.ui.tui.prompt import (
    AutocompleteScreen,
    ButtonDialog,
    ConfirmScreen,
    TextScreen,
)
from meshterm.ui.tui.render import render_lines, render_to_ansi
from meshterm.ui.tui.screen import CANCEL, Screen, ScrollScreen
from meshterm.ui.tui.select import Choice, ReorderScreen, SelectScreen, Separator
from meshterm.ui.tui.session import TuiSession

_UNSET = object()


class _Fut:
    """A minimal stand-in for an asyncio.Future used to capture a screen's result."""

    def __init__(self) -> None:
        self.result = _UNSET
        self._done = False

    def done(self) -> bool:
        return self._done

    def set_result(self, value: object) -> None:
        self.result = value
        self._done = True


def _run(screen, action: str, data: str = ""):
    """Attach a fake future, dispatch one action, and return the captured result."""
    screen.future = _Fut()
    screen.handle(action, data)
    return screen.future.result


# --- render ------------------------------------------------------------------


def test_render_lines_counts_visible_rows() -> None:
    """A three-row table renders to a matching count of ANSI lines (no phantom blank)."""
    table = Table(show_header=False, box=None)
    table.add_column("a")
    for value in ("one", "two", "three"):
        table.add_row(value)
    lines = render_lines(table, 40)
    assert len(lines) == 3
    assert not lines[-1].strip().endswith("\n")


def test_render_to_ansi_wraps_to_width() -> None:
    """Rendering honors the requested width, wrapping long text onto multiple lines."""
    long = Text("word " * 40)  # 200 chars, must wrap under width 20
    lines = render_to_ansi(long, 20).split("\n")
    assert len(lines) > 1


# --- select ------------------------------------------------------------------


def _menu() -> SelectScreen:
    items = [
        Separator("── group ──"),
        Choice("alpha", 1),
        Choice("beta", 2),
        Choice("gamma", 3),
    ]
    return SelectScreen("pick", items, default=2)


def test_select_default_and_arrow_wrap() -> None:
    """The default choice is preselected and Down wraps around the selectable choices."""
    screen = _menu()
    assert _run(screen, "enter") == 2  # default is beta
    screen = _menu()
    screen.handle("down")  # beta -> gamma
    screen.handle("down")  # gamma -> alpha (wrap)
    assert _run(screen, "enter") == 1


def test_select_filter_narrows_but_keeps_separators() -> None:
    """Typing filters to matching choices while the section headings stay in place."""
    screen = _menu()
    screen.handle("text", "a")  # matches alpha, beta, gamma — all contain 'a'
    rows = screen._rows()
    assert [r.title for r in rows if isinstance(r, Separator)] == ["── group ──"]
    assert {r.title for r in rows if isinstance(r, Choice)} == {"alpha", "beta", "gamma"}
    screen.handle("text", "l")  # now 'al' -> only alpha (the heading still shows)
    rows = screen._rows()
    assert [r.title for r in rows if isinstance(r, Choice)] == ["alpha"]
    assert [r.title for r in rows if isinstance(r, Separator)] == ["── group ──"]
    assert _run(screen, "enter") == 1


def test_select_filter_ignores_leading_and_trailing_spaces() -> None:
    """A leading space never begins the filter; a trailing one is dropped when matching."""
    screen = _menu()
    screen.handle("text", " ")  # ignored — the filter never starts with whitespace
    assert screen._filter == ""
    for ch in "alpha ":  # "alpha", then a trailing space
        screen.handle("text", ch)
    assert screen._filter == "alpha "  # the space stays in the buffer…
    # …but matching strips it, so the trailing space doesn't stop "alpha" from matching.
    assert [r.title for r in screen._rows() if isinstance(r, Choice)] == ["alpha"]


def test_select_non_filterable_ignores_typing() -> None:
    """With filtering off, typed keys neither narrow the list nor add a filter line."""
    screen = SelectScreen("pick", [Choice("alpha", 1), Choice("beta", 2)], filterable=False)
    screen.handle("text", "a")
    screen.handle("backspace")
    assert screen._filter == ""
    assert len(screen._rows()) == 2  # nothing was filtered out


def test_select_delete_hint_follows_the_highlight() -> None:
    """The 'Del remove' atom shows only while the cursor sits on a deletable row."""
    screen = SelectScreen(
        "pick",
        [Choice("keep", 1), Choice("drop", 2, deletable=True)],
        footer_hint="↑↓ move · Enter select · Esc quit",
        delete_hint="Del remove",
        filterable=False,
        wrap=False,
    )
    # On the non-deletable row, the footer is the plain base hint.
    assert "Del remove" not in screen.footer_hint
    screen.handle("down")  # move onto the deletable row
    # The atom appears, spliced before the trailing Esc clause (Esc stays last).
    assert screen.footer_hint == "↑↓ move · Enter select · Del remove · Esc quit"
    screen.handle("up")  # back to the plain row
    assert "Del remove" not in screen.footer_hint
    # The box is always sized for the fullest footer, so it never widens on the move.
    assert "Del remove" in screen.sizing_footer_hint


def test_select_no_delete_hint_leaves_footer_fixed() -> None:
    """Without a delete_hint, a deletable row doesn't touch the footer."""
    screen = SelectScreen(
        "pick", [Choice("drop", 1, deletable=True)], footer_hint="↑↓ move · Esc quit"
    )
    assert screen.footer_hint == "↑↓ move · Esc quit"
    assert screen.sizing_footer_hint == "↑↓ move · Esc quit"


def test_select_escape_cancels() -> None:
    """Esc resolves the sentinel rather than a value."""
    assert _run(_menu(), "escape") is CANCEL


def test_select_cursor_line_tracks_selection() -> None:
    """The reported cursor line accounts for separators (and the filter line)."""
    screen = _menu()  # default beta -> row index 2 (sep, alpha, beta)
    screen.render_body(40)
    assert screen.cursor_line() == 2


def test_select_callable_title_re_renders_live() -> None:
    """A callable title is resolved on every repaint, so a live badge tracks state."""
    unread = {"n": 0}
    screen = SelectScreen("pick", [Choice(lambda: f"chan ● {unread['n']}", 1)])
    assert "chan ● 0" in "\n".join(screen.render_body(40))
    unread["n"] = 3  # a message arrived while the list is open
    assert "chan ● 3" in "\n".join(screen.render_body(40))


def test_select_width_aware_title_fits_itself_to_the_row() -> None:
    """A title taking the render width fits itself (a route middle-elides); the natural
    form still feeds filtering and the dialog's own width measure."""
    seen: list[int] = []

    def fitted(width: int) -> str:
        seen.append(width)
        return "you → hub → far" if width >= 20 else "you ⋯ far"

    screen = SelectScreen("pick", [Choice(fitted, 1)])
    assert "you ⋯ far" in "\n".join(screen.render_body(12))
    assert seen[-1] == 10  # the row's content area: the width less the 2-cell pointer
    assert "you → hub → far" in "\n".join(screen.render_body(40))
    assert screen.dialog_width > cell_len("you → hub → far")  # measured at its fullest
    screen.handle("text", "hub")  # the filter reads the natural (unbounded) form
    assert screen._rows() == screen._items


def test_select_row_cracks_a_chip_path_and_ellipsizes_everything_else() -> None:
    """A row too wide for the list is *cut*, not truncated: a row carrying a path line
    (a trophy walk, a probe candidate) breaks its chip off on the crack, while an
    ordinary prose row keeps the ellipsis. The row itself decides which — the list has
    no idea it is ever holding a route."""
    route = PathLine(
        [PathHop(f"NODE{i:02d}", key=f"{i:02x}aa") for i in range(8)], mode="powerline"
    ).text()
    screen = SelectScreen("pick", [Choice(route, 1), Choice("a plainly worded row", 2)])
    rows = [ln for ln in _plain(screen.render_body(16)).split("\n") if ln.strip()]
    route_row = next(ln for ln in rows if "NODE" in ln)
    prose_row = next(ln for ln in rows if "plainly" in ln)
    assert route_row.rstrip().endswith(CRACK_TAIL) and "…" not in route_row
    assert prose_row.rstrip().endswith("…")  # prose was shortened; that is what happened


def test_select_hscroll_highlight_keeps_the_natural_row() -> None:
    """In an hscroll list the highlighted row skips self-fitting — ←→ slide the full
    line — while every other row still elides itself to the width."""

    def fitted(width: int) -> str:
        return "start middle end" if width >= 20 else "start ⋯ end"

    items = [Choice(fitted, 1), Choice(fitted, 2)]
    screen = SelectScreen("pick", items, hscroll=True)
    body = "\n".join(screen.render_body(14))
    assert "start middl" in body  # the highlighted row: natural form, cropped by the screen
    assert "start ⋯ end" in body  # the unhighlighted row fitted itself


def test_select_filter_matches_callable_title() -> None:
    """Type-to-filter matches against a callable title's current text."""
    screen = SelectScreen("pick", [Choice(lambda: "alpha", 1), Choice("beta", 2)])
    screen.handle("text", "alp")
    assert [r.label for r in screen._rows()] == ["alpha"]
    assert _run(screen, "enter") == 1


def test_select_no_wrap_clamps_at_the_ends() -> None:
    """With wrap off, Up on the first row and Down on the last stay put (no cycling)."""
    items = [Choice("alpha", 1), Choice("beta", 2), Choice("gamma", 3)]
    screen = SelectScreen("pick", items, wrap=False)
    screen.handle("up")  # already on the first choice — must not jump to the last
    assert _run(screen, "enter") == 1
    screen = SelectScreen("pick", items, default=3, wrap=False)  # last choice
    screen.handle("down")  # already on the last — must not wrap to the first
    assert _run(screen, "enter") == 3


def test_select_pageup_pagedown_jump_by_a_screenful() -> None:
    """PageDown/PageUp move the highlight a screenful at a time, clamped to the choice range."""
    items = [Choice(f"c{i}", i) for i in range(30)]
    screen = SelectScreen("pick", items)  # starts on the first choice
    screen.note_metrics(total=30, viewport=11)  # a screenful is viewport - 1 = 10 rows
    screen.handle("pagedown")
    assert _run(screen, "enter") == 10  # advanced one page (viewport - 1) down
    screen = SelectScreen("pick", items, default=25)
    screen.note_metrics(total=30, viewport=11)
    screen.handle("pageup")
    assert _run(screen, "enter") == 25 - 10  # and one page back up


def _grouped_menu(default: object = None) -> SelectScreen:
    """A two-section menu long enough that each section scrolls past a small viewport."""
    items: list = [section_heading("Channels")]
    items += [Choice(f"chan{i}", ("c", i)) for i in range(6)]
    items += [section_heading("Direct")]
    items += [Choice(f"peer{i}", ("d", i)) for i in range(8)]
    return SelectScreen("pick", items, default=default)


def _top_plain(screen: SelectScreen, viewport: int) -> str:
    """Slice the screen at its selection-driven scroll and return the top row's plain text."""
    lines = screen.render_body(40)
    visible, _above, _below = frame._visible_slice(screen, lines, viewport)
    return Text.from_ansi(visible[0]).plain.strip()


def test_select_pins_section_heading_when_it_scrolls_off() -> None:
    """Selecting deep in a section keeps that section's heading pinned to the top row."""
    # Highlighting a channel far enough down pushes the "Channels" heading off the top, so it
    # is re-pinned rather than vanishing.
    assert _top_plain(_grouped_menu(default=("c", 5)), viewport=6) == "── Channels ──"
    # Deep into the Direct group, the pinned heading switches to that section's.
    assert _top_plain(_grouped_menu(default=("d", 6)), viewport=6) == "── Direct ──"


def test_select_does_not_pin_a_heading_that_is_still_visible() -> None:
    """With the list scrolled to the top, the real heading shows — nothing is pinned over it."""
    screen = _grouped_menu()  # default selection is the first choice, so scroll stays at 0
    lines = screen.render_body(40)
    visible, above, _below = frame._visible_slice(screen, lines, 6)
    assert Text.from_ansi(visible[0]).plain.strip() == "── Channels ──"
    assert above is False  # top of the list; no pinned duplicate and no "more above"


def _trophy_shaped() -> SelectScreen:
    """The Trophy case's shape: a heading, its description, then that board's rows."""
    items: list = [section_heading("Longest haul")]
    items += [Separator(f"   description line {i}", style="muted") for i in range(2)]
    items += [Choice(f"rec{i}", ("l", i)) for i in range(6)]
    items += [section_heading("Widest arc"), Separator("   no records yet", style="muted")]
    items += [Choice(f"arc{i}", ("a", i)) for i in range(6)]
    return SelectScreen("Trophy case", items, default=("l", 5), wrap=False)


def _blocks(screen: SelectScreen) -> list[list[str]]:
    """Each recorded sticky block's rows, as plain text."""
    return [
        [Text.from_ansi(line).plain.strip() for line in rows]
        for _idx, rows in screen._sticky_headers
    ]


def test_select_blocks_a_heading_with_the_prose_written_under_it() -> None:
    """A heading's landmark runs on through the separators that immediately follow it.

    The Trophy case's shape: each discipline's ``── heading ──`` is followed by its wrapped
    description, which explains the rows below and so belongs overhead with the heading —
    while prose that follows a *row* (a stray note, the exit group's blank) labels nothing
    and is no landmark at all.
    """
    screen = _trophy_shaped()
    screen.render_body(40)
    assert _blocks(screen) == [
        ["── Longest haul ──", "description line 0", "description line 1"],
        ["── Widest arc ──", "no records yet"],
    ]


def test_select_pins_a_block_row_only_once_it_has_scrolled_off() -> None:
    """A block hands its rows over one at a time, so the pins continue into the body.

    While the description is still the top content row the heading alone pins over it; once
    both are gone the two pin together. Never the prose alone — the row that says *which*
    section this is leads whatever is overhead.
    """
    screen = _trophy_shaped()
    screen.render_body(40)
    screen.note_metrics(total=20, viewport=12)
    plain = lambda scroll: [  # noqa: E731 - a one-liner reader for the assertions below
        Text.from_ansi(line).plain.strip() for line in screen.sticky_rows(scroll)
    ]
    assert plain(0) == []  # the heading is the top row itself; nothing to duplicate
    assert plain(1) == ["── Longest haul ──"]  # its description is still on screen
    assert plain(2) == ["── Longest haul ──", "description line 0"]
    assert plain(4) == [
        "── Longest haul ──", "description line 0", "description line 1",
    ]
    # A block never eats more than half the viewport — the rows go from the end, so the
    # heading is the last thing a short terminal gives up.
    screen.note_metrics(total=20, viewport=4)
    assert plain(4) == ["── Longest haul ──", "description line 0"]
    screen.note_metrics(total=20, viewport=2)
    assert plain(4) == ["── Longest haul ──"]


def test_select_pins_the_heading_not_the_prose_beneath_it() -> None:
    """The pinned rows always *lead* with the heading, never the last muted line under it."""
    screen = _trophy_shaped()
    assert _top_plain(screen, viewport=6) == "── Longest haul ──"
    # And the empty-state note can't stand in for its heading either.
    assert _top_plain(_reselect(screen, ("a", 4)), viewport=6) == "── Widest arc ──"


def _reselect(screen: SelectScreen, value: object) -> SelectScreen:
    """Move a select screen's highlight to ``value`` (by walking Down to it)."""
    while screen._choices()[screen._index].value != value:
        screen.handle("down")
    return screen


def test_select_pinned_heading_keeps_the_last_row_reachable() -> None:
    """Even with a heading pinned, the bottom choice stays fully visible (not clipped)."""
    screen = _grouped_menu()
    screen.handle("end")  # highlight the final choice
    lines = screen.render_body(40)
    visible, _above, below = frame._visible_slice(screen, lines, 6)
    assert Text.from_ansi(visible[0]).plain.strip() == "── Direct ──"  # heading pinned
    assert any("peer7" in Text.from_ansi(row).plain for row in visible)  # last row shown
    assert below is False  # and we know we're at the bottom


def _columned_menu(default: object = None) -> SelectScreen:
    """A grouped menu led by a pinned column header, like the config editor's."""
    items: list = [Separator("  SETTING          VALUE", pinned=True)]
    items += [section_heading("Channels")]
    items += [Choice(f"chan{i}", ("c", i)) for i in range(6)]
    items += [section_heading("Direct")]
    items += [Choice(f"peer{i}", ("d", i)) for i in range(8)]
    return SelectScreen("pick", items, default=default, wrap=False)


def test_select_pins_a_column_header_above_the_section_heading() -> None:
    """A pinned column header rides the whole list, the governing heading under it."""
    screen = _columned_menu(default=("d", 6))  # deep in the second section
    lines = screen.render_body(40)
    visible, above, _below = frame._visible_slice(screen, lines, 7)
    assert [Text.from_ansi(row).plain.strip() for row in visible[:2]] == [
        "SETTING          VALUE",  # the lanes, pinned for every section
        "── Direct ──",  # over the section the highlight is in
    ]
    assert above is True
    assert any("peer6" in Text.from_ansi(row).plain for row in visible)  # highlight in view
    # The pinned header is no section landmark — only the two headings are.
    assert _blocks(screen) == [["── Channels ──"], ["── Direct ──"]]


def test_select_column_header_shows_itself_at_the_top_and_pins_alone() -> None:
    """Unscrolled it just draws; past it, it pins even before any heading scrolls off."""
    screen = _columned_menu()  # highlight on the first choice — the list sits at the top
    lines = screen.render_body(40)
    visible, above, _below = frame._visible_slice(screen, lines, 8)
    assert Text.from_ansi(visible[0]).plain.strip() == "SETTING          VALUE"
    assert above is False  # nothing pinned over the real row, nothing above it
    # Scrolled one row on, the header pins while its own section heading is still the top
    # content row — so it is the only pin.
    assert screen.sticky_rows(1) == [screen._pinned_header[1]]


def test_select_pinned_column_header_keeps_the_last_row_reachable() -> None:
    """Two pinned rows still leave the bottom choice fully visible (not clipped)."""
    screen = _columned_menu()
    screen.handle("end")  # highlight the final choice
    lines = screen.render_body(40)
    visible, _above, below = frame._visible_slice(screen, lines, 7)
    assert Text.from_ansi(visible[0]).plain.strip() == "SETTING          VALUE"
    assert any("peer7" in Text.from_ansi(row).plain for row in visible)
    assert below is False


def test_select_resolves_a_width_aware_separator_at_the_render_width() -> None:
    """A callable separator title is handed the render width, so a header can fit itself."""
    screen = SelectScreen(
        "pick", [Separator(lambda w: f"HEADER@{w}", pinned=True), Choice("row", 1)]
    )
    assert Text.from_ansi(screen.render_body(30)[0]).plain.strip() == "HEADER@30"
    assert Text.from_ansi(screen.render_body(48)[0]).plain.strip() == "HEADER@48"
    # Natural-width measurement asks for the fullest form, not a terminal-sized one, so the
    # box is sized to the whole header and only the terminal can force it to abbreviate.
    natural = SelectScreen(
        "pick",
        [Separator(lambda w: "H" * min(w, 120), pinned=True), Choice("row", 1)],
        footer_hint="Esc back",
    )
    assert natural.dialog_width >= 120


def test_select_pinned_header_crops_where_a_plain_separator_wraps() -> None:
    """A pinned row must stay exactly one row: too wide, it ellipsizes rather than wraps."""
    wide = "SETTING" + " " * 40 + "DESCRIPTION"
    screen = SelectScreen("pick", [Separator(wide, pinned=True), Choice("row", 1)])
    lines = screen.render_body(24)
    assert screen._pinned_header == (0, lines[0])
    assert Text.from_ansi(lines[0]).plain.rstrip().endswith("…")
    assert len(lines) == 2  # the header and the one choice — nothing wrapped onto a row
    # An ordinary separator still wraps, each row of it counted as its own body line.
    plain = SelectScreen("pick", [Separator(wide), Choice("row", 1)])
    assert len(plain.render_body(24)) == 3


def test_screen_sticky_rows_stack_the_pinned_header_over_the_section_heading() -> None:
    """The shared rule composing both pins: whole-list header first, then the section's."""
    screen = Screen()
    screen.note_metrics(total=40, viewport=12)
    screen._pinned_header = (0, "COLUMNS")
    screen._sticky_headers = [(1, ["A"]), (5, ["B"])]
    assert screen.sticky_rows(0) == []  # nothing has scrolled off yet
    assert screen.sticky_rows(1) == ["COLUMNS"]  # heading A is itself the top row
    assert screen.sticky_rows(3) == ["COLUMNS", "A"]  # inside section A
    assert screen.sticky_rows(9) == ["COLUMNS", "B"]  # below every heading → the last one
    assert Screen().sticky_rows(9) == []  # nothing recorded → nothing pinned


def test_screen_sticky_block_picks_the_governing_recorded_block() -> None:
    """The base Screen.sticky_block logic is generic over any recorded landmark list.

    Both the select list and the chat transcript reuse it by populating ``_sticky_headers``;
    this exercises the shared rule directly: take the last block starting at or above the
    offset, and pin exactly the rows of it the offset has passed.
    """
    screen = Screen()
    screen.note_metrics(total=40, viewport=12)
    screen._sticky_headers = [(0, ["A"]), (5, ["B", "b"]), (12, ["C"])]
    assert screen.sticky_block(0) == []    # block A is itself the top row
    assert screen.sticky_block(3) == ["A"]  # scrolled past A, before B → A governs
    assert screen.sticky_block(5) == []    # block B's heading is now the top row
    assert screen.sticky_block(6) == ["B"]  # its second row is still on screen
    assert screen.sticky_block(7) == ["B", "b"]  # both gone → both pin
    assert screen.sticky_block(20) == ["C"]  # below every block → the last one pins
    assert Screen().sticky_block(9) == []  # no recorded landmarks → nothing to pin


def test_select_ctrl_page_jumps_between_sections() -> None:
    """Ctrl+PageDown lands on the next section's first choice; Ctrl+PageUp walks back up."""
    screen = _grouped_menu()  # Channels (6) then Direct (8), highlight on the first choice
    screen.handle("ctrl_pagedown")
    assert _run(screen, "enter") == ("d", 0)  # jumped to the first Direct choice
    screen.handle("ctrl_pageup")
    assert _run(screen, "enter") == ("c", 0)  # already atop Direct → back to Channels' first
    screen.handle("ctrl_pagedown")
    assert _run(screen, "enter") == ("d", 0)  # and forward to Direct again


# --- scroll ------------------------------------------------------------------


def test_screen_scroll_helpers_page_and_clamp_to_metrics() -> None:
    """The shared scroll helpers page by a screenful and clamp to the recorded body/viewport."""
    screen = Screen()
    screen.note_metrics(total=100, viewport=10)
    screen.scroll_pages(1)
    assert screen.scroll == 9  # a page is viewport - 1
    screen.scroll_to_bottom()
    assert screen.scroll == 90  # total - viewport
    screen.scroll_lines(50)
    assert screen.scroll == 90  # clamped, never past the bottom
    screen.scroll_to_top()
    assert screen.scroll == 0


def test_screen_section_scroll_walks_recorded_headers() -> None:
    """Ctrl+PageUp/PageDown move the scroll offset between recorded section boundaries."""
    screen = Screen()
    screen.note_metrics(total=100, viewport=10)
    screen._sticky_headers = [(0, ["A"]), (20, ["B"]), (60, ["C"])]
    screen.scroll_to_next_section()
    assert screen.scroll == 20  # from the top → start of section B
    screen.scroll_to_next_section()
    assert screen.scroll == 60  # → start of C
    screen.scroll_to_next_section()
    assert screen.scroll == 90  # no section past C → clamp to the bottom
    screen.scroll = 40  # mid-section B
    screen.scroll_to_section_start()
    assert screen.scroll == 20  # up to B's start
    screen.scroll_to_section_start()
    assert screen.scroll == 0  # already atop B → previous section (A at the top)


def test_scroll_screen_ctrl_edges_and_sectionless_fallback() -> None:
    """Ctrl+Home/End reach the edges; with no sections Ctrl+PageUp/PageDown do too."""
    body = Text("\n".join(f"line {i}" for i in range(100)))
    screen = ScrollScreen(body, title="log")
    screen.render_body(40)  # sets total = 100
    screen.note_viewport(10)
    screen.handle("ctrl_end")
    assert screen.scroll == 90
    screen.handle("ctrl_home")
    assert screen.scroll == 0
    screen.handle("ctrl_pagedown")  # no sections recorded → falls through to the bottom
    assert screen.scroll == 90
    screen.handle("ctrl_pageup")
    assert screen.scroll == 0


# --- scroll (existing) -------------------------------------------------------


def test_scroll_screen_paging_and_clamp() -> None:
    """PageDown advances by a page, End jumps to the bottom, both clamped to content."""
    body = Text("\n".join(f"line {i}" for i in range(100)))
    screen = ScrollScreen(body, title="log")
    screen.render_body(40)  # sets total = 100
    screen.note_viewport(10)
    screen.handle("pagedown")
    assert screen.scroll == 9  # page = viewport - 1
    screen.handle("end")
    assert screen.scroll == 90  # total - viewport
    screen.handle("home")
    assert screen.scroll == 0
    screen.handle("up")
    assert screen.scroll == 0  # clamped, never negative


def test_scroll_screen_escape_resolves_none() -> None:
    """A result window dismisses to ``None`` on Esc or Enter."""
    assert _run(ScrollScreen(Text("x")), "escape") is None
    assert _run(ScrollScreen(Text("x")), "enter") is None


# --- reorder -----------------------------------------------------------------


def test_reorder_apply_row_commits_new_order() -> None:
    """Enter grabs and drops a list row; Enter on Apply commits the rearrangement."""
    screen = ReorderScreen("order", ["a", "b", "c"])
    screen.handle("enter")  # grab "a"
    assert screen._grabbed and "Enter drop" in screen.footer_hint
    screen.handle("down")  # carry it past "b"
    screen.handle("enter")  # drop
    assert not screen._grabbed and "Enter grab" in screen.footer_hint
    screen.handle("down")  # cursor from position 1 past "c"…
    screen.handle("down")  # …onto the Apply row
    assert _run(screen, "enter") == [1, 0, 2]


def test_reorder_actions_follow_the_dirty_state() -> None:
    """Untouched order offers a lone Back; a change swaps in Apply plus discard-Back."""
    screen = ReorderScreen("order", ["a", "b", "c"])
    assert [key for key, _ in screen._actions()] == ["back"]
    screen.handle("enter")
    screen.handle("down")  # dirty now
    assert [key for key, _ in screen._actions()] == ["apply", "back"]
    screen.handle("up")  # moved back home — clean again
    assert [key for key, _ in screen._actions()] == ["back"]


def test_reorder_back_row_and_escape_cancel_discarding_moves() -> None:
    """Enter on Back — like Esc — resolves the sentinel, so the caller keeps the old order."""
    screen = ReorderScreen("order", ["a", "b", "c"])
    screen.handle("enter")
    screen.handle("down")
    assert _run(screen, "escape") is CANCEL

    screen = ReorderScreen("order", ["a", "b", "c"])
    screen.handle("enter")
    screen.handle("down")
    screen.handle("enter")  # drop at position 1; the order is dirty
    for _ in range(3):  # cursor 1 → 2 → Apply → Back
        screen.handle("down")
    assert _run(screen, "enter") is CANCEL


def test_reorder_cursor_wraps_through_the_action_rows() -> None:
    """↑ from the first row lands on the last action row; ↓ from there wraps back to the top."""
    screen = ReorderScreen("order", ["a", "b"])
    screen.handle("up")  # wrap: onto the lone Back row
    assert screen._index == 2
    screen.handle("down")  # wrap forward to the first list row
    assert screen._index == 0


def test_reorder_ignores_typed_characters_including_space() -> None:
    """Typed characters — the spacebar included — neither grab a row nor resolve the screen."""
    screen = ReorderScreen("order", ["a", "b"])
    screen.future = _Fut()
    screen.handle("text", "x")
    screen.handle("text", " ")  # Space no longer grabs; Enter is the grab key
    screen.handle("space")
    assert not screen._grabbed
    assert not screen.future.done()


def test_reorder_dialog_width_is_stable_across_states() -> None:
    """The natural width fits the widest of rows, hints, and dirty actions — and never
    changes as the user grabs a row or dirties the order, so the popup doesn't resize."""
    screen = ReorderScreen("order", ["🔒 alpha", "＃ b"])
    w = screen.dialog_width
    screen.handle("enter")  # grab
    assert screen.dialog_width == w
    screen.handle("down")  # dirty: Apply/discard rows appear
    assert screen.dialog_width == w


# --- frame -------------------------------------------------------------------


def test_compose_base_fills_exactly_terminal_height() -> None:
    """The composed base view is exactly ``rows`` lines regardless of content size."""
    tall = Text("\n".join(f"row {i}" for i in range(200)))
    screen = ScrollScreen(tall, title="big")
    for rows in (10, 24, 50):
        out = frame.compose_base(Text("header"), screen, "Esc back", 80, rows)
        assert out.count("\n") + 1 == rows


def test_compose_dialog_is_bounded() -> None:
    """A dialog for a huge renderable never exceeds the terminal height."""
    tall = Text("\n".join(f"row {i}" for i in range(200)))
    out = frame.compose_dialog(ScrollScreen(tall, title="d"), 80, 20)
    assert out.count("\n") + 1 <= 20


def _box_height(screen: Screen) -> int:
    """The row height of the dialog box ``compose_dialog`` draws for ``screen``."""
    return frame.compose_dialog(screen, 80, 40).count("\n") + 1


def test_ordinary_dialog_box_resizes_to_each_body() -> None:
    """A plain (non grow-only) dialog sizes to whatever body it is currently showing."""
    short = _box_height(ScrollScreen(Text("one line"), title="d"))
    tall = _box_height(ScrollScreen(Text("\n".join(f"row {i}" for i in range(15))), title="d"))
    assert tall > short


class _GrowScreen(Screen):
    """A grow-only dialog whose body height is set per paint, for the ratchet test."""

    grow_only = True

    def __init__(self) -> None:
        super().__init__()
        self.title = "d"
        self.footer_hint = ""
        self.n = 1

    def render_body(self, width: int) -> list[str]:
        return [f"row {i}" for i in range(self.n)]


def test_grow_only_dialog_box_holds_its_tallest_size() -> None:
    """A grow-only dialog grows its box for a taller body and never shrinks for a shorter one."""
    screen = _GrowScreen()
    screen.n = 2
    small = _box_height(screen)
    screen.n = 16
    grown = _box_height(screen)
    assert grown > small       # a taller body enlarges the box
    screen.n = 2
    assert _box_height(screen) == grown  # a shorter body after keeps the larger box


def _box_width(screen: Screen) -> int:
    """The column width of the dialog box ``compose_dialog`` draws for ``screen``."""
    out = Text.from_ansi(frame.compose_dialog(screen, 80, 40)).plain
    return max(cell_len(line.rstrip()) for line in out.split("\n"))


def test_grow_only_dialog_box_holds_its_widest_size() -> None:
    """A grow-only dialog's natural width ratchets too: it widens but never narrows."""
    screen = _GrowScreen()
    screen.dialog_width = 30
    narrow = _box_width(screen)
    screen.dialog_width = 60
    wide = _box_width(screen)
    assert wide > narrow           # a wider body enlarges the box
    screen.dialog_width = 30
    assert _box_width(screen) == wide  # a narrower one after keeps the wider box
    # An ordinary dialog keeps sizing to each width as it comes.
    plain = ScrollScreen(Text("x"), title="d")
    plain.dialog_width = 60
    wide = _box_width(plain)
    plain.dialog_width = 30
    assert _box_width(plain) < wide


def test_compose_startup_is_chromeless_and_shows_banner() -> None:
    """The startup splash fills the height, draws the banner, and omits header/footer bars."""
    screen = SelectScreen("pick", [Choice("alpha", 1), Choice("beta", 2)])
    screen.chrome = False
    screen.banner = ["LOGO-ROW-A", "LOGO-ROW-B"]
    out = frame.compose_startup(screen, 80, 24)
    assert out.count("\n") + 1 == 24  # fills the terminal height exactly
    plain = Text.from_ansi(out).plain
    assert "LOGO-ROW-A" in plain and "LOGO-ROW-B" in plain  # banner is drawn
    assert "pick" in plain  # the box keeps its title
    # The box is content-sized, not full width: no rendered line spans the whole terminal.
    assert all(len(line.rstrip()) < 80 for line in plain.split("\n"))


def test_startup_splash_fits_every_platform_width() -> None:
    """The real wordmark, drawn at each platform's own width, never overruns it.

    The full-size mark is 71 cells; a PicoCalc console is 53. The splash is the one screen
    the gallery doesn't cover, and it shipped torn there until the narrow mark landed —
    so this is the gate.
    """
    from meshterm.platforms import PICOCALC, REGULAR, set_platform
    from meshterm.ui.logo import load_logo

    try:
        for platform in (REGULAR, PICOCALC):
            set_platform(platform)
            cols = platform.readable_cols
            screen = SelectScreen("Choose a device", [Choice("alpha", 1)])
            screen.chrome = False
            screen.banner = load_logo()  # what a real caller sets: the full-size mark
            screen.footnote = copyright_notice()
            out = frame.compose_startup(screen, cols, platform.readable_rows)
            for i, line in enumerate(Text.from_ansi(out).plain.split("\n")):
                assert cell_len(line) <= cols, (
                    f"{platform.name} splash line {i} is {cell_len(line)} cells, over {cols}"
                )
    finally:
        set_platform(REGULAR)


def test_logo_takes_the_widest_mark_that_fits_the_columns() -> None:
    """The screen picks the size, not the platform — a narrow desktop gets the small mark."""
    from meshterm.ui.logo import load_logo, logo_width

    wide = logo_width(load_logo())
    narrow = logo_width(load_logo(53))
    assert 0 < narrow <= 53 < wide
    assert logo_width(load_logo(wide)) == wide  # room for the big one → the big one
    assert logo_width(load_logo(wide - 1)) == narrow  # a column short → step down
    assert load_logo(narrow - 1) == []  # nothing fits: no banner beats a torn one


def test_compose_startup_shows_footnote_under_logo() -> None:
    """A footnote (e.g. a copyright) is drawn muted, immediately under the logo and
    right-aligned to the logo's right edge, so the two read as one signed block."""
    screen = SelectScreen("pick", [Choice("a", 1)])
    screen.chrome = False
    screen.banner = ["A" * 40, "B" * 40]  # a wide wordmark to hang the note off
    screen.footnote = "note-xyz"
    lines = Text.from_ansi(frame.compose_startup(screen, 80, 20)).plain.split("\n")
    logo_rows = [i for i, ln in enumerate(lines) if set(ln.strip()) in ({"A"}, {"B"})]
    note_row = next(i for i, ln in enumerate(lines) if "note-xyz" in ln)
    assert lines[note_row].strip() == "note-xyz"  # its own line, nothing else on it
    assert note_row == logo_rows[-1] + 1  # immediately under the logo, no gap
    # Right edges align: the note ends at the same column the logo ends.
    assert len(lines[note_row].rstrip()) == len(lines[logo_rows[-1]].rstrip())


def test_compose_startup_box_is_horizontally_centered() -> None:
    """The content-sized box is centered, so its rows carry a leading left margin."""
    screen = SelectScreen("pick", [Choice("a", 1)])
    screen.chrome = False
    lines = Text.from_ansi(frame.compose_startup(screen, 80, 20)).plain.split("\n")
    box_lines = [ln for ln in lines if ln.strip()]
    assert box_lines and all(ln.startswith("  ") for ln in box_lines)  # centered inset


# --- glow --------------------------------------------------------------------


def _cell_color(line: str, idx: int) -> tuple[int, int, int]:
    """Resolve the foreground RGB of the character at ``idx`` in an ANSI line."""
    from meshterm.ui.tui.render import _console

    style = Text.from_ansi(line).get_style_at_offset(_console(80), idx)
    assert style.color is not None
    triplet = style.color.get_truecolor()
    return (triplet.red, triplet.green, triplet.blue)


_ACCENT = (129, 140, 248)  # the theme's accent border, #818cf8


def _find(plain: str, glyphs: frozenset[str]) -> int:
    """Index of the first box glyph from ``glyphs`` (the render console may substitute
    rounded corners with square ones on legacy Windows, so tests match the whole family)."""
    return next(i for i, ch in enumerate(plain) if ch in glyphs)


def test_corner_glow_brightens_top_left_and_fades_along_top_edge() -> None:
    """The ╭ corner is lifted toward white and the glow decays rightward to the base color."""
    lines = apply_corner_glow(render_lines(Panel(Text("x"), border_style="accent", width=60), 60))
    plain = Text.from_ansi(lines[0]).plain
    corner = sum(_cell_color(lines[0], _find(plain, glow._TOP_LEFT)))
    middle = sum(_cell_color(lines[0], len(plain) // 2))
    far = sum(_cell_color(lines[0], _find(plain, glow._TOP_RIGHT) - 1))
    assert far == sum(_ACCENT)  # the glow has fully melted into the border color
    assert corner > middle > far  # brightest at the corner, fading rightward


def test_corner_glow_fades_down_left_edge_only() -> None:
    """The left border fades downward; the right edge and bottom corner stay untouched."""
    body = Text("\n".join("row" for _ in range(8)))
    lines = apply_corner_glow(render_lines(Panel(body, border_style="accent", width=30), 30))
    upper = sum(_cell_color(lines[1], 0))
    lower = sum(_cell_color(lines[7], 0))
    assert upper > lower >= sum(_ACCENT)  # vertical fade toward the base color
    right = Text.from_ansi(lines[1]).plain.rindex("│")
    assert _cell_color(lines[1], right) == _ACCENT  # right edge keeps the plain border
    bottom = Text.from_ansi(lines[-1]).plain
    assert _cell_color(lines[-1], _find(bottom, glow._BOTTOM_LEFT)) == _ACCENT  # weight is 0 here


def test_corner_glow_leaves_titles_and_text_alone() -> None:
    """Only border glyphs are recoloured: the title keeps its style, the text its content."""
    panel = Panel(Text("body"), title="Hello", border_style="accent", width=40)
    raw = render_lines(panel, 40)
    lines = apply_corner_glow(raw)
    assert [Text.from_ansi(ln).plain for ln in lines] == [Text.from_ansi(ln).plain for ln in raw]
    idx = Text.from_ansi(raw[0]).plain.index("H")
    assert _cell_color(lines[0], idx) == _cell_color(raw[0], idx)  # title untouched


def test_corner_glow_lights_nested_panels_from_their_own_corners() -> None:
    """A tool panel nested inside a screen body glows too, blended from its own border color."""
    inner = Panel(Text("body"), border_style="muted", width=20)
    screen = ScrollScreen(Group(Text("above"), inner), title="outer")
    lines = frame.compose_base(Text("h"), screen, "hint", 60, 20).split("\n")
    row, plain = next(
        (i, p)
        for i, p in ((i, Text.from_ansi(ln).plain) for i, ln in enumerate(lines))
        # The inner box's top edge: a corner glyph on a row already inside the outer border.
        if any(ch in glow._TOP_LEFT for ch in p) and p.lstrip()[0] in glow._VERTICAL
    )
    muted = (148, 163, 184)  # #94a3b8
    corner = _cell_color(lines[row], _find(plain, glow._TOP_LEFT))
    assert corner != muted and all(c > b for c, b in zip(corner, muted))  # lifted toward white


# --- prompts -----------------------------------------------------------------


def test_text_screen_edits_and_validates() -> None:
    """Typing edits the buffer; a failing validator blocks submit and shows the error."""
    screen = TextScreen("name?", validate=lambda v: True if v == "ok" else "nope")
    for ch in "xy":
        screen.handle("text", ch)
    screen.future = _Fut()
    screen.handle("enter")  # 'xy' fails validation
    assert not screen.future.done()
    assert screen._error == "nope"
    screen.handle("backspace")
    screen.handle("backspace")
    for ch in "ok":
        screen.handle("text", ch)
    assert _run(screen, "enter") == "ok"


def test_text_screen_byte_limit_gauges_and_blocks_an_oversize_entry() -> None:
    """A byte-limited field shows the shared used/limit gauge and blocks Enter over the cap."""
    import re

    screen = TextScreen("msg?", byte_limit=10)
    for ch in "hello":
        screen.handle("text", ch)
    body = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(40)))
    assert "5/10" in body  # the gauge reads used/limit
    for ch in " world":  # 11 bytes total, over the 10-byte cap
        screen.handle("text", ch)
    screen.future = _Fut()
    screen.handle("enter")
    assert not screen.future.done()  # the over-limit entry is blocked
    assert "Too long by 1 byte" in screen._error
    # Trimming back within budget lets it submit.
    for _ in range(2):
        screen.handle("backspace")
    assert _run(screen, "enter") == "hello wor"


def test_line_editor_word_motion() -> None:
    """Ctrl+Left/Right hop by word — to the current word's start, else the previous/next."""
    from meshterm.ui.tui.prompt import _LineEditor

    editor = _LineEditor("the quick  brown fox")  # cursor at the end (len 20)
    editor.edit("ctrl_left")
    assert editor.cursor == 17  # start of "fox"
    editor.edit("ctrl_left")
    assert editor.cursor == 11  # skips the double space → start of "brown"
    editor.edit("ctrl_left")
    assert editor.cursor == 4  # start of "quick"
    editor.edit("ctrl_right")
    assert editor.cursor == 11  # forward over "quick" and the spaces → start of "brown"
    editor.cursor = 13  # mid-"brown"
    editor.edit("ctrl_left")
    assert editor.cursor == 11  # to the current word's start, not the previous word


def test_text_screen_password_masks() -> None:
    """A password field renders bullets, not the typed characters."""
    screen = TextScreen("pw?", password=True)
    for ch in "secret":
        screen.handle("text", ch)
    rendered = "\n".join(screen.render_body(40))
    assert "secret" not in rendered
    assert "•" in rendered


def test_line_editor_caps_length_and_truncates_paste() -> None:
    """A max_length editor swallows keys past the cap and truncates an over-long paste."""
    from meshterm.ui.tui.prompt import _LineEditor

    editor = _LineEditor("", max_length=6)
    for ch in "123456":
        assert editor.edit("text", ch) is True
    assert editor.edit("text", "9") is False  # at capacity — key swallowed, buffer unchanged
    assert editor.text == "123456"

    pasted = _LineEditor("", max_length=6)
    pasted.edit("text", "12345678")  # one over-long insert
    assert pasted.text == "123456"  # filled only the six available slots


def test_line_editor_paste_folds_controls_and_respects_max_length() -> None:
    """A ``paste`` action folds newlines/controls to spaces and inserts the run at the cursor."""
    from meshterm.ui.tui.prompt import _LineEditor

    editor = _LineEditor("ab")
    editor.cursor = 1
    assert editor.edit("paste", "X\nY") is True
    assert editor.text == "aX Yb"  # the newline became a space, inserted mid-buffer

    capped = _LineEditor("", max_length=3)
    capped.edit("paste", "hello")
    assert capped.text == "hel"  # a paste is trimmed to the remaining room, like a big insert

    assert _LineEditor("z").edit("paste", "") is False  # nothing to paste leaves the buffer


def test_pin_dialog_shows_six_slots_with_dots_for_blanks() -> None:
    """The PIN field is six fixed slots: bullets for typed digits, centre dots for blanks."""
    from meshterm.ui.tui.prompt import PinDialog

    dialog = PinDialog("MeshCore-Homestead")
    # Empty: six blank centre dots, no bullets yet.
    field = dialog._editor.render(slots=PinDialog.PIN_LENGTH).plain
    assert field.count("·") == 6
    assert "•" not in field

    # After three digits: three bullets, three remaining centre dots.
    for ch in "701":
        dialog.handle("text", ch)
    field = dialog._editor.render(slots=PinDialog.PIN_LENGTH).plain
    assert field.count("•") == 3
    assert field.count("·") == 3

    # A full six-digit PIN fills every slot; typing more is capped at six.
    for ch in "307999":
        dialog.handle("text", ch)
    assert dialog._editor.text == "123456"
    field = dialog._editor.render(slots=PinDialog.PIN_LENGTH).plain
    assert field.count("•") == 6
    assert "·" not in field


def test_confirm_toggle_and_default() -> None:
    """The confirm toggles with arrows/letters and returns the chosen bool."""
    screen = ConfirmScreen("sure?", default=True)
    assert _run(screen, "enter") is True
    screen = ConfirmScreen("sure?", default=True)
    screen.handle("left")  # toggle to No
    assert _run(screen, "enter") is False
    screen = ConfirmScreen("sure?", default=True)
    screen.handle("text", "n")
    assert _run(screen, "enter") is False


def test_button_dialog_enter_commits_highlighted() -> None:
    """Enter returns the highlighted button's value; the default sets the highlight."""
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=0)
    assert _run(screen, "enter") is True
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=1)
    assert _run(screen, "enter") is False


def test_button_dialog_arrows_move_highlight() -> None:
    """←/→ (and Tab) move the highlight between the buttons, wrapping at the ends."""
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=0)
    screen.handle("right")  # → No
    assert _run(screen, "enter") is False
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=0)
    screen.handle("left")  # wraps → No
    assert _run(screen, "enter") is False


def test_button_dialog_shortcut_keys_commit_instantly() -> None:
    """A mapped shortcut key commits its value straight away, bypassing the highlight."""
    screen = ButtonDialog(
        "quit?", [("Yes", True), ("No", False)], default=1, keys={"y": True, "n": False}
    )
    assert _run(screen, "text", "Y") is True  # case-insensitive, ignores the No default
    screen = ButtonDialog(
        "quit?", [("Yes", True), ("No", False)], default=0, keys={"y": True, "n": False}
    )
    assert _run(screen, "text", "n") is False


def test_button_dialog_escape_cancels() -> None:
    """Esc resolves with CANCEL so the caller can treat it as 'stay'."""
    from meshterm.ui.tui.screen import CANCEL

    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)])
    assert _run(screen, "escape") is CANCEL


def test_button_dialog_renders_a_styled_text_prompt_line_per_line() -> None:
    """A pre-styled multi-line Text prompt renders each line, keeping its content."""
    message = Text.from_markup("[ok]✓[/ok] clock set\n[ok]●[/ok] wrote backup.toml")
    screen = ButtonDialog(message, [("OK", "ok")])
    body = "\n".join(screen.render_body(60))
    plain = Text.from_ansi(body).plain
    assert "✓ clock set" in plain
    assert "● wrote backup.toml" in plain
    assert "OK" in plain


def test_button_dialog_sizes_to_the_widest_prompt_line() -> None:
    """dialog_width follows the longest line of a multi-line Text prompt."""
    wide = "a really quite long outcome line for sizing"
    message = Text(f"short\n{wide}")
    screen = ButtonDialog(message, [("OK", "ok")])
    assert screen.dialog_width == len(wide) + 12  # matches the margin the dialog adds


def test_autocomplete_suggests_and_tab_completes() -> None:
    """Suggestions match case-insensitively; Tab fills the highlighted one; Enter commits."""
    screen = AutocompleteScreen("target?", ["Alice", "Bob", "alfred"])
    for ch in "al":
        screen.handle("text", ch)
    assert screen._suggestions() == ["Alice", "alfred"]
    screen.handle("down")  # highlight 'alfred'
    screen.handle("tab")  # fill it
    assert screen._editor.text == "alfred"
    assert _run(screen, "enter") == "alfred"


def test_autocomplete_accepts_free_text() -> None:
    """Text with no matching suggestion still commits verbatim (e.g. a hex prefix)."""
    screen = AutocompleteScreen("target?", ["Alice"])
    for ch in "3d":
        screen.handle("text", ch)
    assert _run(screen, "enter") == "3d"


# --- device picker -----------------------------------------------------------


def test_device_picker_builds_aligned_columns(tmp_path) -> None:
    """The picker lays devices out in columns that line up across rows of differing widths."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    devices = [
        DiscoveredDevice(port="COM5", product="Wio SX1262", vid=0x2886),
        DiscoveredDevice(port="/dev/ttyUSB0", product="FT232R USB UART", vid=0x0403),
    ]
    captured: dict = {}

    class _Ui:
        async def select_startup(
            self, title, items, *, default=None, banner=None, footnote=None
        ):
            captured["items"] = items
            captured["banner"] = banner
            captured["footnote"] = footnote
            return None  # skip: never reaches the smoke test

    async def _never(_device):  # verify is unused when the user skips
        raise AssertionError("verify should not run when selection is skipped")

    store = DeviceStore(tmp_path / "devices.json")
    asyncio.run(prompt_device(_Ui(), devices, store, _never))
    # The banner (wordmark) is passed through so the splash can draw it.
    assert captured["banner"] and any("█" in row for row in captured["banner"])
    # A copyright footnote rides along for the splash to render beside the logo.
    assert captured["footnote"] == copyright_notice()
    # Each device row's port sits at the same column, proving the name column is padded.
    rows = [
        it.label.plain if hasattr(it.label, "plain") else it.label
        for it in captured["items"]
        if isinstance(it, Choice)
    ]
    # Two devices plus the trailing action rows (add a network device, then Quit).
    assert len(rows) == 4
    assert "Add a network device…" in rows[-2]
    assert rows[-2].strip().endswith("· experimental")  # the row is flagged experimental
    assert rows[-1].strip().endswith("Quit")
    device_rows = rows[:2]
    assert all(port in row for port, row in zip(("COM5", "/dev/ttyUSB0"), device_rows))
    assert device_rows[0].index("COM5") == device_rows[1].index("/dev/ttyUSB0")


def test_device_picker_names_and_sorts_known_devices(tmp_path) -> None:
    """A confirmed device shows its node name (in white), sorts to the top, and marks type."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import _BLE_ICON, _SERIAL_ICON, prompt_device

    # A previously-confirmed serial node, an unknown serial port, and a BLE companion.
    known = DiscoveredDevice(
        port="COM11", serial_number="SN1", description="USB Serial Device (COM11)"
    )
    unknown = DiscoveredDevice(port="COM3", product="Some Adapter", vid=0x1234)
    ble = DiscoveredDevice(
        transport="ble", address="AA:BB:CC:DD:EE:FF", name="MeshCore-Roam", product="MeshCore-Roam"
    )
    devices = [unknown, known, ble]  # discovery order: known is *not* first

    store = DeviceStore(tmp_path / "devices.json")
    store.remember(known, node_name="BaseStation")

    captured: dict = {}

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            captured["items"] = items
            return None  # skip past the smoke test

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    asyncio.run(prompt_device(_Ui(), devices, store, _never))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    device_rows = rows[:-1]  # drop the trailing Quit row

    # The confirmed device sorts to the very top and is shown by its mesh node name, not the
    # OS's generic "USB Serial Device" description.
    top = device_rows[0]
    assert "BaseStation" in top.plain
    assert "USB Serial Device" not in top.plain
    # …and that name is painted white ("device.known") so it stands out.
    assert any(span.style == "device.known" for span in top.spans)

    # The TYPE column marks the transport: a serial glyph for the wired node, the Bluetooth
    # rune for the companion advertised over BLE.
    assert _SERIAL_ICON in top.plain
    assert any(_BLE_ICON in row.plain for row in device_rows)


def test_device_picker_reinjects_remembered_tcp_device(tmp_path) -> None:
    """A remembered TCP companion reappears in the picker even though it can't be scanned for."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import tcp_device
    from meshterm.ui.device_picker import _TCP_ICON, prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    store.remember(tcp_device("192.168.1.50", 5000), node_name="WifiNode")

    captured: dict = {}

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            captured["items"] = items
            return None  # skip past the smoke test

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    # Nothing discovered this session, yet the remembered network device is rebuilt into the list.
    asyncio.run(prompt_device(_Ui(), [], store, _never))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    device_rows = [r for r in rows if hasattr(r, "plain") and "WifiNode" in r.plain]
    assert device_rows, "the remembered TCP device should be listed"
    top = device_rows[0]
    assert "192.168.1.50:5000" in top.plain  # its host:port sits in the address column
    assert _TCP_ICON in top.plain  # marked with the network TYPE glyph


def test_device_picker_lists_configured_tcp_profile(tmp_path) -> None:
    """A ``[profiles.*]`` TCP entry shows up in the picker under its alias, ready to select."""
    from meshterm.core.config import DeviceProfile
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.device_picker import _TCP_ICON, prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    profiles = {
        "bridge": DeviceProfile(
            name="bridge", transport="tcp", host="127.0.0.1", tcp_port=5000
        )
    }

    captured: dict = {}

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            captured["items"] = items
            return None  # skip past the smoke test

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    # Nothing scanned or remembered — the profile alone puts the endpoint in the list.
    asyncio.run(prompt_device(_Ui(), [], store, _never, profiles))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    device_rows = [r for r in rows if hasattr(r, "plain") and "bridge" in r.plain]
    assert device_rows, "the configured TCP profile should be listed"
    top = device_rows[0]
    assert "127.0.0.1:5000" in top.plain  # its host:port sits in the address column
    assert _TCP_ICON in top.plain  # marked with the network TYPE glyph


def test_device_picker_lists_configured_serial_profile(tmp_path) -> None:
    """A ``[profiles.*]`` serial entry (a soldered ``/dev/ttyS1``) shows up in the picker under
    its alias, ready to select — even though pyserial's scan never produces that platform port."""
    from meshterm.core.config import DeviceProfile
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.device_picker import prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    profiles = {"picocalc": DeviceProfile(name="picocalc", port="/dev/ttyS1", baudrate=115200)}

    captured: dict = {}

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            captured["items"] = items
            return None  # skip past the smoke test

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    # Nothing scanned or remembered — the serial profile alone puts the port in the list.
    asyncio.run(prompt_device(_Ui(), [], store, _never, profiles))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    device_rows = [r for r in rows if hasattr(r, "plain") and "picocalc" in r.plain]
    assert device_rows, "the configured serial profile should be listed"
    assert "/dev/ttyS1" in device_rows[0].plain  # its port sits in the address column


def test_device_picker_serial_profile_yields_to_scanned_port(tmp_path) -> None:
    """A serial profile whose port pyserial DOES enumerate is not double-listed — the scanned
    row (with real USB metadata) wins over the bare profile."""
    from meshterm.core.config import DeviceProfile
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    scanned = [DiscoveredDevice(port="/dev/ttyUSB0", product="XIAO", vid=0x2886, pid=0x8044)]
    profiles = {"radio": DeviceProfile(name="radio", port="/dev/ttyUSB0")}

    captured: dict = {}

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            captured["items"] = items
            return None

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    asyncio.run(prompt_device(_Ui(), scanned, store, _never, profiles))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    usb_rows = [r for r in rows if hasattr(r, "plain") and "/dev/ttyUSB0" in r.plain]
    assert len(usb_rows) == 1, "the scanned port must not be duplicated by the profile"


def test_device_picker_profile_yields_to_remembered_endpoint(tmp_path) -> None:
    """A profile at an already-remembered endpoint doesn't double-list — the richer row wins."""
    from meshterm.core.config import DeviceProfile
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import tcp_device
    from meshterm.ui.device_picker import prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    # Confirmed before, so it carries the real node name learned at connect time.
    store.remember(tcp_device("127.0.0.1", 5000), node_name="uConsole")
    profiles = {
        "bridge": DeviceProfile(
            name="bridge", transport="tcp", host="127.0.0.1", tcp_port=5000
        )
    }

    captured: dict = {}

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            captured["items"] = items
            return None

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    asyncio.run(prompt_device(_Ui(), [], store, _never, profiles))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    endpoint_rows = [r for r in rows if hasattr(r, "plain") and "127.0.0.1:5000" in r.plain]
    assert len(endpoint_rows) == 1, "the endpoint should appear exactly once"
    # The remembered node name wins over the bare profile alias.
    assert "uConsole" in endpoint_rows[0].plain
    assert "bridge" not in endpoint_rows[0].plain


def test_device_picker_adds_network_device(tmp_path) -> None:
    """The 'add a network device' row prompts for host:port and confirms the TCP companion."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.device_picker import _ADD_TCP, prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    probed: dict = {}

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            # Choose the "add a network device" action row.
            return next(it.value for it in items if isinstance(it, Choice) and it.value is _ADD_TCP)

        async def prompt_text_startup(self, title, *, prompt="", default="", validate=None,
                                      help_text="", banner=None, footnote=None):
            assert validate("192.168.1.50:5000") is True  # the validator accepts a good endpoint
            return "192.168.1.50:5000"

        async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
            return await coro

    async def verify(device, pin=None):
        probed["target"] = device.target
        return {"adv_name": "WifiNode"}  # a genuine companion answers

    chosen = asyncio.run(prompt_device(_Ui(), [], store, verify))
    assert chosen is not None and chosen.is_tcp and chosen.target == "192.168.1.50:5000"
    assert probed["target"] == "192.168.1.50:5000"
    # The confirmed network device is remembered forever, with the node name it reported.
    remembered = store.load()
    assert remembered is not None and remembered.is_tcp and remembered.node_name == "WifiNode"


def test_device_picker_removes_network_device_on_delete(tmp_path) -> None:
    """Delete on a network row confirms, forgets it, and it's gone from the re-drawn list."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import tcp_device
    from meshterm.ui.device_picker import _QUIT, prompt_device
    from meshterm.ui.tui import DeleteRequest

    store = DeviceStore(tmp_path / "devices.json")
    store.remember(tcp_device("192.168.1.50", 5000), node_name="WifiNode")

    seen_rows: list[list] = []
    confirmed_prompts: list = []

    class _Ui:
        def __init__(self) -> None:
            self._passes = 0

        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            names = [it.label.plain for it in items
                     if isinstance(it, Choice) and hasattr(it.label, "plain")]
            seen_rows.append(names)
            self._passes += 1
            if self._passes == 1:
                # First pass: the network row is present and marked deletable — press Delete.
                row = next(it for it in items if isinstance(it, Choice)
                           and getattr(it.value, "is_tcp", False))
                assert row.deletable
                return DeleteRequest(row.value)
            # Second pass (after the removal): leave the picker.
            return _QUIT

        async def confirm_startup(self, prompt, *, title="", confirm_label="Remove",
                                  banner=None, footnote=None, backdrop_items=None,
                                  backdrop_default=None):
            confirmed_prompts.append(prompt)
            # The confirm floats over the picker: it's handed the rows to redraw behind it,
            # with the row being removed pre-highlighted.
            assert backdrop_items is not None
            assert getattr(backdrop_default, "is_tcp", False)
            return True  # the user confirms the removal

    async def _never(_device, _pin=None):
        raise AssertionError("verify should not run when a row is deleted, not chosen")

    result = asyncio.run(prompt_device(_Ui(), [], store, _never))
    assert result is None  # quit on the second pass
    # The confirm named the device and its endpoint.
    assert confirmed_prompts and "WifiNode" in confirmed_prompts[0]
    assert "192.168.1.50:5000" in confirmed_prompts[0]
    # It was there on the first draw and gone on the second, and the store forgot it.
    assert any("WifiNode" in name for name in seen_rows[0])
    assert not any("WifiNode" in name for name in seen_rows[1])
    assert store.load() is None


def test_device_picker_keeps_network_device_when_removal_cancelled(tmp_path) -> None:
    """Cancelling the Delete confirm leaves the remembered network device untouched."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import tcp_device
    from meshterm.ui.device_picker import _QUIT, prompt_device
    from meshterm.ui.tui import DeleteRequest

    store = DeviceStore(tmp_path / "devices.json")
    store.remember(tcp_device("192.168.1.50", 5000), node_name="WifiNode")

    class _Ui:
        def __init__(self) -> None:
            self._passes = 0

        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            self._passes += 1
            if self._passes == 1:
                row = next(it for it in items if isinstance(it, Choice)
                           and getattr(it.value, "is_tcp", False))
                return DeleteRequest(row.value)
            return _QUIT

        async def confirm_startup(self, prompt, *, title="", confirm_label="Remove",
                                  banner=None, footnote=None, backdrop_items=None,
                                  backdrop_default=None):
            return False  # the user backs out (Cancel / Esc)

    async def _never(_device, _pin=None):
        raise AssertionError("verify should not run")

    asyncio.run(prompt_device(_Ui(), [], store, _never))
    # Nothing was forgotten — the device is still remembered.
    remembered = store.load()
    assert remembered is not None and remembered.node_name == "WifiNode"


def test_confirm_startup_floats_red_over_the_picker_backdrop() -> None:
    """The removal confirm floats as a red popup over a redrawn picker, not a full splash."""
    from meshterm.core.discovery import tcp_device

    session = TuiSession()
    device = tcp_device("192.168.1.50", 5000, name="WifiNode")
    items = [Choice("WifiNode (192.168.1.50:5000)", device, deletable=True)]

    async def main() -> None:
        task = asyncio.ensure_future(
            session.confirm_startup(
                "Remove WifiNode?",
                title="Remove network device",
                banner=["MESHTERM"],
                backdrop_items=items,
                backdrop_default=device,
            )
        )
        # Let confirm_startup push the backdrop list and float the confirm over it.
        for _ in range(3):
            await asyncio.sleep(0)

        base = session._base_screen()
        floats = session._float_layers()
        # The picker is redrawn as the chromeless base; the confirm floats over it (not a
        # full-screen splash that replaces the list).
        assert isinstance(base, SelectScreen) and base.chrome is False
        assert len(floats) == 1
        dialog = floats[0]
        assert isinstance(dialog, ButtonDialog)
        assert dialog.border_style == "err"  # the reserved data-loss red

        dialog.resolve(True)  # commit the removal
        result = await task
        assert result is True
        assert session._stack == []  # the backdrop is torn down with the dialog

    asyncio.run(main())


class _PickerUi:
    """A fake splash UI that always selects the first device, then dismisses messages."""

    def __init__(self) -> None:
        self.notes: list = []

    async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
        return next(it.value for it in items if isinstance(it, Choice))

    async def notify_startup(self, renderable, *, title="", banner=None, footnote=None):
        self.notes.append(renderable)

    async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
        return await coro


def test_device_picker_smoke_tests_and_reprompts(tmp_path) -> None:
    """A failed smoke test re-prompts; a passing one is remembered as confirmed."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    devices = [DiscoveredDevice(port="COM5", serial_number="SN1", product="Wio SX1262")]
    store = DeviceStore(tmp_path / "devices.json")
    ui = _PickerUi()

    # First probe fails (not MeshCore), second answers with self-info.
    results = [None, {"adv_name": "BaseStation"}]

    async def verify(_device, _pin=None):
        return results.pop(0)

    chosen = asyncio.run(prompt_device(ui, devices, store, verify))
    assert chosen is devices[0]
    assert len(ui.notes) == 1  # the "not a MeshCore device" message was shown once
    remembered = store.load()
    assert remembered is not None and remembered.node_name == "BaseStation"
    assert store.is_known(devices[0])


def test_device_picker_shows_copyright_on_every_splash(tmp_path) -> None:
    """The copyright rides along the logo on every splash — picker, spinner, and notices."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    devices = [DiscoveredDevice(port="COM5", serial_number="SN1", product="Wio SX1262")]
    store = DeviceStore(tmp_path / "devices.json")
    footnotes: list = []

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            footnotes.append(("select", footnote))
            return next(it.value for it in items if isinstance(it, Choice))

        async def notify_startup(self, renderable, *, title="", banner=None, footnote=None):
            footnotes.append(("notify", footnote))

        async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
            footnotes.append(("busy", footnote))
            return await coro

    # First probe fails (re-pick), second passes — exercising every post-selection screen.
    results = [None, {"adv_name": "BaseStation"}]

    async def verify(_device, _pin=None):
        return results.pop(0)

    asyncio.run(prompt_device(_Ui(), devices, store, verify))
    # Every splash draws the logo, so every splash carries the copyright beside it — the
    # opening picker, the smoke-test spinner, the failure notice, and the re-opened picker.
    assert len(footnotes) > 1
    assert all(footnote == copyright_notice() for _kind, footnote in footnotes)


def test_device_picker_prompts_and_retries_ble_pin(tmp_path) -> None:
    """A PIN-protected device opens the popup, re-asks on a wrong code, then connects."""
    from meshterm.core.connection import DeviceAuthenticationError
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    devices = [
        DiscoveredDevice(transport="ble", address="00:11:22:33:44:55", name="MeshCore-Pinned")
    ]
    store = DeviceStore(tmp_path / "devices.json")

    entered = iter(["000000", "654321"])  # a wrong code, then the right one
    errors: list = []

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            return next(it.value for it in items if isinstance(it, Choice))

        async def notify_startup(self, renderable, *, title="", banner=None, footnote=None):
            raise AssertionError("a successful PIN connect shows no failure notice")

        async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
            return await coro

        async def prompt_pin_startup(
            self, device_name, *, error="", help_text="", banner=None, footnote=None
        ):
            errors.append(error)
            return next(entered)

    async def verify(_device, pin=None):
        if pin != "654321":  # the first probe (no PIN) and the wrong code both get rejected
            raise DeviceAuthenticationError("needs a Bluetooth pairing PIN")
        return {"adv_name": "Pinned", "model": "Seeed Tracker T1000-E"}

    chosen = asyncio.run(prompt_device(_Ui(), devices, store, verify))
    assert chosen is devices[0]
    # Asked twice: first with no error, then with a "rejected" note after the wrong code.
    assert errors[0] == ""
    assert "rejected" in errors[1].lower()
    remembered = store.load()
    assert remembered is not None
    assert remembered.node_name == "Pinned"
    assert remembered.hardware_model == "Seeed Tracker T1000-E"  # learned once connected


def test_device_picker_pin_cancel_returns_to_list(tmp_path) -> None:
    """Esc on the PIN popup returns to the device list instead of connecting."""
    from meshterm.core.connection import DeviceAuthenticationError
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import _QUIT, prompt_device

    devices = [
        DiscoveredDevice(transport="ble", address="00:11:22:33:44:55", name="MeshCore-Pinned")
    ]
    store = DeviceStore(tmp_path / "devices.json")
    picks = iter([0, "quit"])  # pick the device once, then quit the re-opened list

    class _Ui:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            choices = [it for it in items if isinstance(it, Choice)]
            step = next(picks)
            if step == "quit":
                return next(it.value for it in choices if it.value is _QUIT)
            return choices[step].value

        async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
            return await coro

        async def prompt_pin_startup(
            self, device_name, *, error="", help_text="", banner=None, footnote=None
        ):
            return None  # the user cancels the PIN entry

    async def verify(_device, pin=None):
        raise DeviceAuthenticationError("needs a Bluetooth pairing PIN")

    result = asyncio.run(prompt_device(_Ui(), devices, store, verify))
    assert result is None  # cancelling the PIN then quitting leaves the picker empty-handed
    assert store.load() is None  # nothing was remembered


def test_device_picker_quit_row_returns_none(tmp_path) -> None:
    """Choosing the trailing Quit row leaves the picker (None) without a smoke test."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import _QUIT, prompt_device

    devices = [DiscoveredDevice(port="COM5", product="Wio SX1262")]

    class _QuitUi:
        async def select_startup(self, title, items, *, default=None, banner=None, footnote=None):
            # The last choice is the Quit row; picking it signals "exit".
            quit_choice = [it for it in items if isinstance(it, Choice) and it.value is _QUIT]
            assert quit_choice, "the picker offers a Quit row"
            return quit_choice[0].value

    async def _never(_device):
        raise AssertionError("verify must not run when the user quits")

    store = DeviceStore(tmp_path / "devices.json")
    assert asyncio.run(prompt_device(_QuitUi(), devices, store, _never)) is None


# --- busy splash --------------------------------------------------------------


def test_busy_screen_spins_over_its_message() -> None:
    """The busy splash shows an ASCII spinner beside its message and advances on tick."""
    from rich.text import Text as RichText

    from meshterm.ui.tui.screen import BusyScreen
    from meshterm.ui.tui.spinner import Spinner

    screen = BusyScreen("Talking to Wio on COM5…")

    def glyph() -> str:
        """The first (spinner) character of the rendered body, ANSI codes stripped."""
        return RichText.from_ansi("\n".join(screen.render_body(60))).plain.lstrip()[0]

    plain = RichText.from_ansi("\n".join(screen.render_body(60))).plain
    assert "Talking to Wio on COM5" in plain
    assert glyph() in Spinner.BRAILLE  # a spinner glyph leads the line

    # Ticking cycles through every frame and returns to the first.
    seen = {glyph()}
    for _ in range(len(Spinner.BRAILLE) - 1):
        screen.tick()
        seen.add(glyph())
    assert seen == set(Spinner.BRAILLE)


def test_spinner_cycles_and_resets() -> None:
    """The reusable Spinner advances through its frames, wraps, and resets."""
    from meshterm.ui.tui.spinner import Spinner

    spinner = Spinner("ab", style="warn")
    assert spinner.frame == "a"
    assert spinner.text().plain == "a" and spinner.text().style == "warn"
    spinner.tick()
    assert spinner.frame == "b"
    spinner.tick()  # wraps back to the first frame
    assert spinner.frame == "a"
    spinner.tick()
    spinner.reset()
    assert spinner.frame == "a"


# --- progress ----------------------------------------------------------------


def test_progress_handle_matches_rich_api() -> None:
    """add_task/advance/update mirror the Rich Progress subset the tools rely on."""
    screen = ProgressScreen("work")
    task = screen.add_task("tracing", total=5)
    screen.advance(task)
    screen.advance(task, 2)
    assert screen._tasks[task].completed == 3
    screen.update(task, description="tracing more", completed=5, total=5)
    assert screen._tasks[task].description == "tracing more"
    assert screen._tasks[task].completed == 5
    # Renders without error at a realistic width.
    assert screen.render_body(60)


def test_progress_chip_animates_between_advances() -> None:
    """The working chip spins on tick alone, so a task that rarely advances still reads alive.

    A trace advances just once, at the very end; the meter would sit motionless until then.
    Ticking the dialog must change what it renders even with the task's count untouched.
    """
    screen = ProgressScreen("trace")
    screen.add_task("tracing", total=1)  # will sit at 0/1 for the whole wait
    before = "\n".join(screen.render_body(60))
    screen.tick()
    after = "\n".join(screen.render_body(60))
    assert before != after  # the chip moved even though nothing advanced


def test_progress_completed_task_shows_check_and_indeterminate_has_no_track() -> None:
    """A finished task flips its chip to ✓; an unknown-total task draws no dead track."""
    from rich.text import Text as RichText

    screen = ProgressScreen("work")
    done = screen.add_task("tracing", total=2)
    screen.advance(done, 2)
    indet = screen.add_task("optimizing", total=None)
    plain = RichText.from_ansi("\n".join(screen.render_body(60))).plain
    assert "✓ tracing" in plain
    # The indeterminate row carries only the spinning chip + its count, never a track glyph.
    indet_line = next(line for line in plain.splitlines() if "optimizing" in line)
    assert "⠶" not in indet_line and "⣿" not in indet_line


# --- session stack -----------------------------------------------------------


async def test_session_runs_and_exits_when_main_returns() -> None:
    """The app starts, drives the main coroutine, and exits cleanly when it returns."""
    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        ran = {}

        async def main() -> None:
            ran["done"] = True

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert ran["done"] is True


async def test_session_select_dispatches_piped_keys() -> None:
    """A select resolves the value chosen via piped Down+Enter keystrokes, end to end."""
    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        captured = {}

        async def main() -> None:
            captured["value"] = await session.select(
                "pick", [Choice("a", 1), Choice("b", 2), Choice("c", 3)]
            )

        inp.send_text("\x1b[B\x1b[B\r")  # Down, Down, Enter -> third choice
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert captured["value"] == 3


async def test_session_busy_startup_animates_and_returns() -> None:
    """busy_startup awaits the task behind a chromeless spinner splash, then pops it."""
    from meshterm.ui.tui.screen import BusyScreen

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        captured = {}

        async def work() -> str:
            # While the task runs the busy splash is the chromeless base screen.
            assert isinstance(session.top, BusyScreen)
            assert session.top.chrome is False
            await asyncio.sleep(0.3)  # long enough for the spinner to tick at least once
            return "ok"

        async def main() -> None:
            captured["value"] = await session.busy_startup("checking…", work())

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert captured["value"] == "ok"
    assert session.top is None  # the splash was popped when the task finished


async def test_session_text_dispatches_typed_keys() -> None:
    """A text prompt captures typed characters and commits on Enter, end to end."""
    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        captured = {}

        async def main() -> None:
            captured["value"] = await session.text("name?")

        inp.send_text("hi\r")
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert captured["value"] == "hi"


def test_session_stack_and_float_selection() -> None:
    """The stack tracks top/base and only floats a deeper screen over its parent."""
    session = TuiSession()
    assert session.top is None
    base = ScrollScreen(Text("base"), title="base")
    session.push(base)
    assert session.top is base
    assert session._base_screen() is base
    assert not session._has_float()  # single screen: no float

    dialog = SelectScreen("pick", [Choice("a", 1)])
    session.push(dialog)
    assert session._has_float()  # deeper screen floats
    assert session._base_screen() is base  # base is the parent beneath the dialog
    session.pop(dialog)
    assert session.top is base
    assert not session._has_float()


def test_session_stacks_every_dialog_over_one_full_frame_background() -> None:
    """A dialog over a dialog: both float over the single background, neither full-frame.

    The old compositor drew only the top float and rendered the second-from-top as the
    full-frame base — so opening a confirm over a popup stretched that popup to fill the
    frame. Now the background is the deepest full-frame screen and every floating layer
    above it stays its own centered box.
    """
    session = TuiSession()
    channels = SelectScreen("Channels", [Choice("Ops", 1)])  # a tool's own floating list
    detail = SelectScreen("Ops (private)", [Choice("Clear", "clr")])  # its item popup
    confirm = ButtonDialog("Clear Ops?", [("Cancel", 0), ("Clear", 1)], border_style="err")
    for screen in (channels, detail, confirm):
        session.push(screen)

    # The bottom (all-floating) screen is the one full-frame background; the detail popup
    # and the confirm both float over it — the detail is no longer promoted to the base.
    assert session._base_screen() is channels
    assert session._float_layers() == [detail, confirm]

    session.pop(confirm)
    assert session._float_layers() == [detail]  # detail stays a float, not full-frame
    assert session._base_screen() is channels


def test_session_background_is_the_topmost_full_frame_screen() -> None:
    """A non-floating screen (a map, a scroll window) is the background under any dialogs."""
    session = TuiSession()
    full = ScrollScreen(Text("map"), title="map")  # floating=False
    dialog = ButtonDialog("go?", [("No", 0), ("Yes", 1)])
    session.push(full)
    session.push(dialog)
    assert session._base_screen() is full
    assert session._float_layers() == [dialog]


def test_dispatch_promotes_nav_actions_while_right_ctrl_is_held(monkeypatch) -> None:
    """A bare navigation key arriving while the physical right Ctrl is down becomes its
    Ctrl chord — the rescue for layouts (Canadian Multilingual Standard) that claim right
    Ctrl as a character modifier and strip the ctrl flag from the console's arrow event.
    Actions with no Ctrl sibling pass through untouched, as does everything once the key
    is released."""
    from meshterm.ui.tui import session as session_mod

    session = TuiSession()
    seen: list[str] = []

    class Probe(ScrollScreen):
        def handle(self, action: str, data: str = "") -> None:
            seen.append(action)

    session.push(Probe(Text("x")))

    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: True)
    session._dispatch("left")
    session._dispatch("home")
    # Enter has a sibling too — it is the one key a terminal can't spell chorded itself.
    session._dispatch("enter")
    session._dispatch("escape")  # no ctrl sibling: untouched even while held
    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: False)
    session._dispatch("left")
    assert seen == ["ctrl_left", "ctrl_home", "ctrl_enter", "escape", "left"]


def test_dispatch_promotes_letter_chords_while_right_ctrl_is_held(monkeypatch) -> None:
    """A bare letter typed while the physical right Ctrl is down becomes its Ctrl-letter
    chord — the same rescue as the nav keys, for the ^R/^P shortcuts a layout-claimed right
    Ctrl would otherwise strip to plain text. Only the mapped letters promote (case-folded,
    with the now-stale data dropped); other text — and everything once the key is released —
    stays text."""
    from meshterm.ui.tui import session as session_mod

    session = TuiSession()
    seen: list[tuple[str, str]] = []

    class Probe(ScrollScreen):
        def handle(self, action: str, data: str = "") -> None:
            seen.append((action, data))

    session.push(Probe(Text("x")))

    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: True)
    session._dispatch("text", "r")  # ^R retry
    session._dispatch("text", "P")  # ^P paths, case-folded
    session._dispatch("text", "x")  # unmapped letter: stays text even while held
    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: False)
    session._dispatch("text", "r")  # released: plain text again
    assert seen == [("retry", ""), ("paths", ""), ("text", "x"), ("text", "r")]


def test_right_ctrl_rescue_covers_the_sessions_own_chords(monkeypatch) -> None:
    """The rescue is app-wide, not screen-actions-only: ^V and ^C are answered by the session
    itself, and a layout-claimed right Ctrl must reach them too — right Ctrl-V pastes the
    clipboard into a compose line rather than typing a ``v``, right Ctrl-C quits. Neither
    pseudo-action is ever forwarded to a screen."""
    from meshterm.ui.tui import session as session_mod

    session = TuiSession()
    seen: list[tuple[str, str]] = []

    class Probe(ScrollScreen):
        def handle(self, action: str, data: str = "") -> None:
            seen.append((action, data))

    class FakeApp:
        def __init__(self) -> None:
            self.exited = False

        def exit(self) -> None:
            self.exited = True

        def invalidate(self) -> None:
            pass

    session.push(Probe(Text("x")))
    session._app = FakeApp()
    monkeypatch.setattr(session_mod, "_read_clipboard", lambda: "pasted")

    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: True)
    session._dispatch("text", "v")  # ^V: clipboard reaches the screen as a paste
    assert seen == [("paste", "pasted")]
    session._dispatch("text", "c")  # ^C: quits, and nothing lands on the screen
    assert seen == [("paste", "pasted")]
    assert session._app.exited is True

    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: False)
    session._dispatch("text", "v")  # released: a plain typed character again
    assert seen[-1] == ("text", "v")


def test_every_ctrl_letter_chord_is_bound_on_both_ctrl_keys() -> None:
    """The chord table drives the prompt_toolkit bindings, so a chord can never be bound for
    the left Ctrl without its right-Ctrl rescue (the drift the two used to be able to develop
    when the letter map was maintained by hand)."""
    from prompt_toolkit.keys import Keys

    from meshterm.ui.tui.session import _CTRL_LETTER_CHORDS, _KEY_ACTIONS

    for letter, action in _CTRL_LETTER_CHORDS.items():
        key = getattr(Keys, f"Control{letter.upper()}")
        assert _KEY_ACTIONS[key] == action
    # Enter/Tab/Backspace are spelled c-m/c-i/c-h: claiming those letters would rebind them.
    assert not {"m", "i", "h"} & set(_CTRL_LETTER_CHORDS)
    assert _KEY_ACTIONS[Keys.Enter] == "enter"


def test_wide_glyph_detection_flags_emoji_not_marks() -> None:
    """The desync only ever comes from a width-2 glyph the terminal may draw narrower — an
    emoji. Node-type marks, status marks and chart braille are width-1 everywhere, so they
    must not trip the check (that they seemed to was the earlier misdiagnosis)."""
    from meshterm.ui.tui.session import _has_wide_glyph

    assert _has_wide_glyph("👋")
    assert _has_wide_glyph("Bob 👋 waved")
    assert _has_wide_glyph("clock 🕒 sync")
    assert _has_wide_glyph("⚡ explore")  # a width-2 icon still counts
    assert not _has_wide_glyph("plain ascii row")
    assert not _has_wide_glyph("★ ▲ ● ■ ◉ ○")  # node-type marks: width 1
    assert not _has_wide_glyph("⠿⣿⡇ chart")  # braille: width 1
    assert not _has_wide_glyph("✓ ✗ ⚠ … done")  # status marks: width 1


def _repaint_harness():
    """A session wired to a fake pt app, plus its remembered 3-row frame of ``A`` cells."""
    import types

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout.screen import Char
    from prompt_toolkit.layout.screen import Screen as PtScreen

    remembered = PtScreen()
    for row in range(3):
        for x in range(10):
            remembered.data_buffer[row][x] = Char("A")
    session = TuiSession()
    session._app = types.SimpleNamespace(
        renderer=types.SimpleNamespace(_last_screen=remembered),
        output=types.SimpleNamespace(get_size=lambda: Size(rows=10, columns=60)),
        invalidate=lambda: None,
    )
    return session, remembered


def _row_text(screen, row: int) -> str:
    """The remembered frame's row as plain characters (the scrub's sentinel shows through)."""
    return "".join(screen.data_buffer[row][x].char for x in range(10))


def test_a_wide_glyph_frame_upgrades_to_a_full_repaint() -> None:
    """prompt_toolkit paints differentially with a *relative* cursor — sound only while every
    glyph is one cell. A width-2 glyph the terminal draws in one cell (an emoji in a chat line)
    leaves the row's cursor model off; a later paint that skips the unchanged emoji then strands
    stale cells to its right. With no remembered frame to compare against, a composed frame
    carrying such a glyph drops pt's cached frame, upgrading the next paint to a full
    erase_down + redraw. A frame of only width-1 glyphs keeps the fast differential paint —
    this holds wherever the glyph is, floating dialog or not."""
    session, _remembered = _repaint_harness()
    session._emit("Bob 👋 says hi")
    assert session._app.renderer._last_screen is None

    # A frame of only width-1 glyphs — plain text, node marks, chart braille — keeps the
    # efficient differential paint.
    session, remembered = _repaint_harness()
    session._emit("★ you  ▲ repeater  ● node  ⠿ chart")
    assert session._app.renderer._last_screen is remembered
    session._emit("★ you  ▲ repeater  ● node  ⠿ chart · moved")  # changed, still all width-1
    assert session._app.renderer._last_screen is remembered
    assert _row_text(remembered, 0) == "A" * 10  # nothing scrubbed either


def test_only_the_rows_that_changed_are_repainted() -> None:
    """A ticking header repaints the header, not the screen under it.

    The requirement a wide glyph imposes is that its *row* be rewritten whole, from column 0;
    pt steps down a row with a carriage return, so the misalignment can never reach the rows
    below. The background composes one line per terminal row, so the rows that changed are
    exactly what needs rewriting — and an idle frame changes none of them, which is what the
    1 Hz refresh was flickering over.
    """
    session, remembered = _repaint_harness()
    frame_1 = "🎯 Farthest node\nrow one\nrow two"
    session._emit(frame_1)
    assert session._app.renderer._last_screen is None  # nothing to compare against yet

    session._app.renderer._last_screen = remembered
    session._emit(frame_1)  # the timer tick: same frame, nothing touched at all
    assert session._app.renderer._last_screen is remembered
    assert [_row_text(remembered, y) for y in range(3)] == ["A" * 10] * 3

    session._emit("🎯 Farthest node\nrow one changed\nrow two")
    assert session._app.renderer._last_screen is remembered  # no erase, no full redraw
    assert _row_text(remembered, 1) == "￿" * 10  # the one changed row, marked whole
    assert _row_text(remembered, 0) == "A" * 10  # …and the rows around it left alone
    assert _row_text(remembered, 2) == "A" * 10

    # A frame whose height moved has no row mapping to trust — repaint everything.
    session._emit("🎯 Farthest node\nrow one changed")
    assert session._app.renderer._last_screen is None


def test_a_changed_layer_repaints_over_a_still_wide_glyph_frame() -> None:
    """Any layer changing upgrades the paint while a wide glyph is drawn *anywhere*.

    A plain dialog moving over a base row that carries an emoji is rewritten from a model of
    that row the terminal disagrees with, so it is the frame as a whole — not the layer that
    happened to change — that decides. A layer *leaving* counts as a change too: a closing
    dialog just stops rendering, and the cells it gives back to the base would otherwise be
    rewritten piecemeal.
    """
    import types

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout.screen import Screen as PtScreen

    session = TuiSession()
    remembered = PtScreen()
    session._app = types.SimpleNamespace(
        renderer=types.SimpleNamespace(_last_screen=remembered),
        output=types.SimpleNamespace(get_size=lambda: Size(rows=10, columns=60)),
        invalidate=lambda: None,
    )
    session._emit("🎯 the board behind", "base")  # a wide glyph on the background
    session._app.renderer._last_screen = remembered
    session._emit("plain dialog, frame 1", "float0")  # no emoji of its own…
    assert session._app.renderer._last_screen is None  # …but the frame carries one

    session._app.renderer._last_screen = remembered
    session._emit("plain dialog, frame 1", "float0")  # unchanged again → left alone
    assert session._app.renderer._last_screen is remembered

    # The dialog closes: one screen on the stack means no float layer this paint, so
    # reconciling drops it — and, with the emoji-bearing base still drawn, the cells it hands
    # back are repainted whole rather than piecemeal.
    session.push(Screen())
    session._app.renderer._last_screen = remembered
    session._reconcile_layers()
    assert "float0" not in session._layers
    assert session._app.renderer._last_screen is None

    # With nothing wide left drawn, a layer leaving costs no repaint at all.
    session._layers.clear()
    session._emit("plain base", "base")
    session._emit("plain dialog", "float0")
    session._app.renderer._last_screen = remembered
    session._reconcile_layers()
    assert session._app.renderer._last_screen is remembered


def test_floating_text_prompt_is_a_popup_over_a_blank_base() -> None:
    """``text(floating=True)`` floats as a centered popup even on an empty stack.

    A mid-flow modal — a remote-admin password, between the node picker and the admin menu —
    must float like the button dialogs, so a blank base is slipped beneath it rather than
    letting the prompt fill the frame the way a tool's primary entry screen does.
    """
    session = TuiSession()

    async def main() -> None:
        task = asyncio.ensure_future(
            session.text("Password", password=True, floating=True)
        )
        for _ in range(5):
            await asyncio.sleep(0)
            if session._has_float():
                break
        floats = session._float_layers()
        assert len(floats) == 1 and isinstance(floats[0], TextScreen)
        assert session._base_screen() is not floats[0]  # a blank base sits beneath it

        floats[0].resolve("hunter2")
        assert await task == "hunter2"
        assert session._stack == []  # the blank base is torn down with the prompt

    asyncio.run(main())


def test_default_text_prompt_is_the_full_frame_base() -> None:
    """A default ``text`` prompt on an empty stack *is* the frame — a tool's primary entry.

    The Trace target's typed fallback stands in for the select picker, so it fills the frame
    rather than floating over a blank base (the floating popup is opt-in, see above).
    """
    session = TuiSession()

    async def main() -> None:
        task = asyncio.ensure_future(session.text("Target node"))
        for _ in range(5):
            await asyncio.sleep(0)
            if session.top is not None:
                break
        assert isinstance(session.top, TextScreen)
        assert not session._has_float()  # no float — the prompt is the background
        assert session._base_screen() is session.top

        session.top.resolve("YUL")
        assert await task == "YUL"

    asyncio.run(main())


# --- busy skeleton card ------------------------------------------------------


def test_busy_overlay_renders_title_chip_caption_and_scanner_row() -> None:
    """A titled, captioned card shows its heading, the working chip + caption, and the LED bar."""
    import re

    from meshterm.ui.tui.overlay import BusyOverlay, _SCAN_CELLS, _SCAN_GLYPH

    overlay = BusyOverlay("reading from Waymarker…", title="Nodes", fade=0.0)  # full bright at once
    ansi = overlay.render()
    assert "Nodes" in ansi  # the heading naming the screen being fetched
    assert "reading from Waymarker" in ansi  # the caption beside the chip
    assert overlay.spinner.frame in ansi  # the one-cell working chip
    # The scanning bar stands in for the content to come: one braille lamp per cell, each styled
    # on its own, so strip ANSI before counting the run of lamps.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi)
    assert _SCAN_GLYPH * _SCAN_CELLS in plain


def test_busy_overlay_chip_ticks_with_the_animation() -> None:
    """The card's working chip is the reusable one-cell Spinner and advances on tick."""
    from meshterm.ui.tui.overlay import BusyOverlay
    from meshterm.ui.tui.spinner import Spinner

    overlay = BusyOverlay()
    assert isinstance(overlay.spinner, Spinner)
    first = overlay.spinner.frame
    overlay.tick()
    assert overlay.spinner.frame != first


def test_busy_overlay_scanner_recolours_on_tick_without_reshaping() -> None:
    """The bar isn't static: the light moves, so ticking recolours it — but its shape holds.

    Regression guard for the frozen-looking card — the chip spun but the bar sat dead-still.
    The braille layout must stay put while the colouring (the moving light) shifts each tick.
    """
    import re

    from meshterm.ui.tui.overlay import BusyOverlay, _SCAN_GLYPH

    overlay = BusyOverlay("reading…", title="Nodes", fade=0.0)  # full bright, no fade to wait out

    def lamp_rows(ansi: str) -> list[str]:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi)
        return [line for line in plain.splitlines() if _SCAN_GLYPH in line]

    before = overlay.render()
    for _ in range(3):
        overlay.tick()
    after = overlay.render()
    assert after != before  # the light moved: the ANSI colouring differs
    assert lamp_rows(after) == lamp_rows(before)  # …but the lamps never shift shape


def _scanner_head(overlay) -> int:  # type: ignore[no-untyped-def]
    """The index of the brightest lamp — where the scanning light's head currently sits."""
    glow = overlay._glow
    return max(range(len(glow)), key=lambda i: glow[i])


def test_busy_overlay_scanner_leaves_a_persistence_of_vision_trail() -> None:
    """Lamps the head has passed keep glowing, fading — a comet tail, not a lone bright lamp."""
    from meshterm.ui.tui.overlay import BusyOverlay

    overlay = BusyOverlay("reading…", title="Nodes", fade=0.0)
    for _ in range(5):  # sweep in off the left edge so a head and a tail both exist
        overlay.tick()

    head = _scanner_head(overlay)
    glow = overlay._glow
    assert glow[head] > 0.0  # a lit head
    trail = [g for i, g in enumerate(glow) if i < head and g > 0.0]  # lamps behind the head
    assert trail  # the head dragged a trail rather than leaving black behind it
    assert max(trail) < glow[head]  # and the trail is dimmer than the head it follows


def test_busy_overlay_scanner_bounces_off_both_ends() -> None:
    """The head sweeps to the right edge and back to the left — the Knight-Rider ping-pong."""
    from meshterm.ui.tui.overlay import BusyOverlay, _SCAN_CELLS

    overlay = BusyOverlay(fade=0.0)
    heads = [_scanner_head(overlay)]
    for _ in range(80):  # long enough for at least one full there-and-back sweep
        overlay.tick()
        heads.append(_scanner_head(overlay))

    assert max(heads) >= _SCAN_CELLS - 2  # reached the right end
    assert min(heads) <= 1  # …and came back to the left end


def test_busy_overlay_holds_black_then_fades_in() -> None:
    """Brightness is 0 through the hold, then climbs to full colour over the fade window."""
    from meshterm.ui.tui.overlay import BusyOverlay

    overlay = BusyOverlay(hold=0.1, fade=0.2)
    assert overlay.brightness == 0.0  # nothing paints during the hold
    overlay.started_at -= 0.1  # to the very end of the hold
    assert overlay.brightness < 0.2  # only now beginning to glow up from black
    overlay.started_at -= 0.2  # past the full fade window
    assert overlay.brightness == 1.0  # fully lit


def test_dim_color_scales_hex_toward_black() -> None:
    """The fade dimmer scales the hex channels and preserves attribute words like ``bold``."""
    from meshterm.ui.tui.overlay import dim_color

    assert dim_color("#38bdf8", 1.0) == "#38bdf8"  # untouched at full brightness
    assert dim_color("#ffffff", 0.0) == "#000000"  # black at zero
    assert dim_color("bold #ffffff", 0.5) == "bold #808080"  # keeps 'bold', halves the colour


def test_overlay_fade_restarts_when_re_exposed_after_a_prompt() -> None:
    """Popping back to an empty stack replays the black-hold + fade, not a full-bright snap."""
    from meshterm.ui.tui.overlay import BusyOverlay

    session = TuiSession()
    overlay = BusyOverlay()
    session._overlay = overlay
    overlay.started_at -= 10  # pretend the intro already finished
    assert overlay.brightness == 1.0

    screen = ScrollScreen(Text("prompt"))
    session.push(screen)  # a prompt covers the card
    session.pop(screen)  # dismissed → card re-exposed on the now-empty stack
    assert overlay.brightness == 0.0  # the fade restarted from black


async def test_session_busy_overlay_shows_between_screens_and_clears() -> None:
    """The overlay floats while a block runs on an empty stack, and is dropped afterwards."""
    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        seen = {}

        async def main() -> None:
            async with session.busy_overlay("working…"):
                await asyncio.sleep(0.05)  # still within the initial hold
                seen["hidden_during_hold"] = not session._overlay_visible()
                await asyncio.sleep(0.3)  # past the 200ms hold: the ring has faded in
                seen["active"] = session._overlay is not None
                seen["visible_empty_stack"] = session._overlay_visible()
                seen["rendered"] = bool(session._render_overlay().value.strip())
                # With a screen on the stack the ring stays hidden so it can't bury a prompt.
                session.push(ScrollScreen(Text("prompt"), title="p"))
                seen["hidden_over_screen"] = not session._overlay_visible()
                session.pop()

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert seen["hidden_during_hold"] is True
    assert seen["active"] is True
    assert seen["visible_empty_stack"] is True
    assert seen["rendered"] is True
    assert seen["hidden_over_screen"] is True
    assert session._overlay is None  # cleared on exit


# --- horizontal scroll (opt-in) -------------------------------------------------------


def _hscroll_screen(width_of_rows: int = 60) -> "SelectScreen":
    from meshterm.ui.tui.select import Choice, SelectScreen, Separator

    return SelectScreen(
        "long",
        [
            Separator("HEAD-" + "h" * width_of_rows),
            Choice("row-one-" + "x" * width_of_rows + "-tail", 1),
            Choice("short", 2),
        ],
        hscroll=True,
        filterable=True,
    )


def _row_plains(screen, width: int) -> list[str]:
    import re

    return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in screen.render_body(width)]


def test_select_hscroll_shifts_only_the_highlighted_row() -> None:
    """→ slides the highlighted row under its pinned pointer; other rows and headers hold."""
    screen = _hscroll_screen()  # the long "row-one" is highlighted by default
    before = _row_plains(screen, 40)
    assert any("row-one-" in ln for ln in before)
    screen.handle("right")
    shifted = _row_plains(screen, 40)
    # The highlighted row's head scrolled off, under the still-pinned pointer…
    assert any(ln.startswith("❯ ") for ln in shifted)
    assert not any("row-one-" in ln for ln in shifted)
    # …but the section header did not move, and the short row is untouched.
    assert any(ln.strip().startswith("HEAD-") for ln in shifted)
    assert any("short" in ln for ln in shifted)
    screen.handle("left")
    assert any("row-one-" in ln for ln in _row_plains(screen, 40))


def test_select_hscroll_clamps_at_the_highlighted_rows_tail() -> None:
    """→ stops once the *highlighted* row's own end is in view, not the widest row's."""
    screen = _hscroll_screen()
    for _ in range(50):
        screen.handle("right")
    plains = _row_plains(screen, 40)  # the render clamps the shift
    assert any("-tail" in ln for ln in plains)  # the highlighted row's end is visible
    row_len = len("row-one-" + "x" * 60 + "-tail")
    # Clamped to the first whole step that brings the tail inside the lane (the width less
    # the pointer, less the cell a scrolled row spends on its left cut mark) — stopping on
    # the exact flush-right cell would leave a right mark promising a remainder.
    step = screen._HSCROLL_STEP
    assert screen._hshift == -(-(row_len - (40 - 2 - 1)) // step) * step


def test_select_hscroll_resets_when_the_highlight_moves() -> None:
    """The shift is per-row: moving the highlight (or editing the filter) drops it to the start."""
    screen = _hscroll_screen()
    screen.handle("right")
    assert screen._hshift > 0
    screen.handle("down")  # moving to another row abandons that row's scroll
    assert screen._hshift == 0
    screen.handle("right")
    screen.handle("text", "r")  # a filter edit resets it too
    assert screen._hshift == 0


def test_select_hscroll_only_acts_on_an_overflowing_row() -> None:
    """←→ and its footer atom appear only while the highlighted row overflows the width."""
    screen = _hscroll_screen()
    screen.render_body(40)  # the long row-one is highlighted and overflows 40 cells
    assert "←→ scroll" in screen.footer_hint
    screen.handle("down")  # the short row fits — nothing to scroll
    screen.render_body(40)
    assert "←→ scroll" not in screen.footer_hint
    screen.handle("right")
    screen.render_body(40)
    assert screen._hshift == 0  # a row that fits can't shift


def test_select_hscroll_from_pins_the_rows_head_and_slides_only_its_run() -> None:
    """A row that declares a head block keeps it drawn while ←→ scroll everything past it."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    lanes = "#1 Aug 09  "
    screen = SelectScreen(
        "long",
        [Choice(lanes + "run-" + "y" * 60 + "-end", 1, hscroll_from=len(lanes))],
        hscroll=True,
    )
    screen.handle("right")
    row = _row_plains(screen, 40)[0]
    assert row.startswith("❯ " + lanes)  # the lanes never move…
    assert "run-" not in row  # …while the run behind them has slid off to the left
    for _ in range(50):
        screen.handle("right")
    end = _row_plains(screen, 40)[0]
    assert end.startswith("❯ " + lanes) and "-end" in end  # the tail is reachable


def test_select_hscroll_marks_both_edges_the_run_continues_past() -> None:
    """A scrolled row cracks/ellipsizes at whichever side its run runs on."""
    from meshterm.ui.pathline import _ELLIPSIS
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen(
        "long", [Choice("z" * 200, 1)], hscroll=True
    )
    screen.handle("right")
    row = _row_plains(screen, 40)[0]
    # Plain prose has no chip fill to shear, so both marks fall back to the ellipsis.
    assert row.startswith("❯ " + _ELLIPSIS) and row.endswith(_ELLIPSIS)


def test_select_without_hscroll_ignores_left_right() -> None:
    """The flag defaults off: ←/→ stay inert and rows render exactly as before."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen("plain", [Choice("row", 1)])
    before = screen.render_body(40)
    screen.handle("right")
    screen.handle("left")
    assert screen.render_body(40) == before and screen._hshift == 0


# --- row detail lines (opt-in) -------------------------------------------------------


def test_select_choice_detail_hangs_under_its_row() -> None:
    """A Choice.detail draws as a second, indented line right under its title."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen(
        "pick", [Choice("first row", 1, detail="weakest -6.0 dB  ·  3×"), Choice("second", 2)]
    )
    lines = _row_plains(screen, 40)
    idx = next(i for i, ln in enumerate(lines) if "first row" in ln)
    assert lines[idx + 1].startswith("  weakest -6.0 dB")
    assert "second" in lines[idx + 2]


def test_select_choice_without_detail_draws_one_line() -> None:
    """A Choice with no detail (the default, or one resolving empty) stays a single line."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen("pick", [Choice("first row", 1), Choice("second", 2, detail="")])
    lines = _row_plains(screen, 40)
    assert lines[1].strip() == "second"


def test_select_cursor_tracks_the_highlight_past_a_detail_line() -> None:
    """The cursor lands on the title line even when an earlier row grew a detail line."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen(
        "pick",
        [Choice("first row", 1, detail="has detail"), Choice("second", 2)],
        default=2,
    )
    lines = _row_plains(screen, 40)
    assert screen.cursor_line() == next(i for i, ln in enumerate(lines) if "second" in ln)


def test_select_hscroll_leaves_the_detail_line_unshifted() -> None:
    """←→ slides the highlighted row's title only — its detail line never scrolls."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    long_title = "row-one-" + "x" * 60 + "-tail"
    screen = SelectScreen(
        "pick", [Choice(long_title, 1, detail="weakest -6.0 dB")], hscroll=True
    )
    screen.handle("right")
    lines = _row_plains(screen, 40)
    assert "weakest -6.0 dB" in lines[1]


# --- the fast path's frame source ----------------------------------------------------


def _framed_session():
    """A session with just enough of an app behind it to compose a frame at 53x26."""
    import types

    from prompt_toolkit.data_structures import Size

    session = TuiSession()
    session._app = types.SimpleNamespace(
        renderer=types.SimpleNamespace(_last_screen=None),
        output=types.SimpleNamespace(get_size=lambda: Size(rows=26, columns=53)),
        invalidate=lambda: None,
    )
    return session


def test_a_dialog_frame_is_composed_here_not_handed_to_prompt_toolkit() -> None:
    """A float used to send the whole frame down prompt_toolkit's renderer.

    That cost its full grid rebuild and diff on every keystroke — in every confirm, picker
    and viewer in the app. The float's placement is reproducible (an unanchored, unsized
    float is centred), so the frame source returns the finished picture with the box merged
    in, and the row diff applies to dialogs like everything else.
    """
    session = _framed_session()
    session.push(ScrollScreen(Text("the list beneath"), title="Nodes", floating=False))
    session.push(ButtonDialog("Remove this contact?", [("Cancel", 0), ("Remove", 1)]))

    text = session._plain_frame()
    assert text is not None, "a dialog must not fall back to prompt_toolkit"
    rows = text.split("\n")
    assert len(rows) == 26
    body = "\n".join(rows)
    assert "Remove this contact?" in _plain(body)
    assert "the list beneath" in _plain(body), "the backdrop must show around the box"


def test_the_busy_overlay_is_still_prompt_toolkits_to_place() -> None:
    """The one float we don't place: a content-sized window the float container measures."""
    from meshterm.ui.tui.overlay import BusyOverlay

    session = _framed_session()
    assert session._plain_frame() is None  # an empty stack has no background either
    overlay = BusyOverlay(title="Starting up")
    overlay.started_at -= overlay.hold + overlay.fade  # past the hold: it is on screen
    session._overlay = overlay
    assert session._overlay_visible()
    assert session._plain_frame() is None


def test_the_rows_a_dialog_does_not_reach_come_back_unchanged() -> None:
    """What makes compositing worth doing: the diff still skips the untouched rows."""
    session = _framed_session()
    session.push(ScrollScreen(Text("\n".join(f"line {i}" for i in range(40))),
                              title="Nodes", floating=False))
    first = session._plain_frame().split("\n")
    session.push(ButtonDialog("Sure?", [("Cancel", 0), ("Yes", 1)]))
    second = session._plain_frame().split("\n")

    unchanged = sum(1 for a, b in zip(first, second) if a == b)
    assert unchanged >= 10, "the box should only rewrite the rows it covers"


# --- the body is drawn one viewport at a time ----------------------------------------


def test_a_long_list_only_rasterizes_the_rows_the_viewport_shows() -> None:
    """141 contacts laid out 150 rows tall still only shows twenty of them.

    Rendering the rest was pure waste on every keystroke *and* on the once-a-second tick.
    The line count has to stay exact, though — the scroll clamp, the ``↑↓ more`` markers and
    the sticky-header offsets are all measured against it.
    """
    from meshterm.ui.tui.screen import LazyLines

    drawn: list[int] = []

    def title(i: int):
        def build():
            drawn.append(i)
            return f"contact {i:03d}"

        return build

    screen = SelectScreen("Contacts", [Choice(title(i), i) for i in range(150)])
    lines = screen.render_body(53)

    assert isinstance(lines, LazyLines)
    assert len(lines) == 150  # the count is exact and cost nothing
    assert drawn == [], "no row is rasterized until something reads it"

    window = lines[0:20]
    assert len(window) == 20
    assert sorted(drawn) == list(range(20))
    assert "contact 000" in _plain(window[0])

    lines[5]  # a re-read is memoized, never a second render
    assert sorted(drawn) == list(range(20))


def test_a_lazy_body_slices_frames_and_scrolls_exactly_as_a_list_did() -> None:
    """The substitution has to be invisible to the frame: same height, same clip flags.

    Deferring a row's *drawing* must never defer how many rows there are — the scroll
    clamp, the ``↑↓ more`` subtitle and the pinned heading are all measured against that
    count, and every one of them would go wrong if the tail were merely unbuilt.
    """
    items = [section_heading("Group")]
    items += [Choice(f"row {i}", i) for i in range(60)]
    screen = SelectScreen("Long", items, footer_hint="Esc back")
    screen.floating = False

    out = frame.compose_base(Text("hdr"), screen, "Esc back", 53, 26)
    assert len(out.split("\n")) == 26
    plain = _plain(out)
    assert "row 0" in plain and "↓ more" in plain
    assert screen._scroll_total == 61  # the whole body is still measured

    for _ in range(45):  # walk the highlight past the fold
        screen.handle("down")
    plain = _plain(frame.compose_base(Text("hdr"), screen, "Esc back", 53, 26))
    assert "row 45" in plain, "the highlight must still be kept in view"
    assert "row 0 " not in plain, "the top of the list should have scrolled away"
    assert "── Group ──" in plain, "the section heading pins once it scrolls off"


def test_an_off_screen_rows_live_title_is_left_alone() -> None:
    """A row's callable title is resolved only while the row is actually on screen."""
    calls: list[str] = []
    items = [
        Choice(lambda: (calls.append("top"), "top row")[1], 0),
        *[Choice(f"filler {i}", i) for i in range(1, 40)],
        Choice(lambda: (calls.append("bottom"), "bottom row")[1], 40),
    ]
    screen = SelectScreen("Live", items)
    screen.floating = False
    lines = screen.render_body(53)
    lines[0:10]
    assert calls == ["top"]
