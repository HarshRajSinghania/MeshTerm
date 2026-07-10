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
from rich.table import Table
from rich.text import Text

from meshterm.ui.tui import frame
from meshterm.ui.tui.progress import ProgressScreen
from meshterm.ui.tui.prompt import (
    AutocompleteScreen,
    ButtonDialog,
    ConfirmScreen,
    TextScreen,
)
from meshterm.ui.tui.render import render_lines, render_to_ansi
from meshterm.ui.tui.screen import CANCEL, Screen, ScrollScreen
from meshterm.ui.tui.select import Choice, SelectScreen, Separator
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


def test_select_filter_narrows_and_hides_separators() -> None:
    """Typing filters to matching choices only; separators drop out while filtering."""
    screen = _menu()
    screen.handle("text", "a")  # matches alpha, beta(?), gamma -> those containing 'a'
    rows = screen._rows()
    assert all(isinstance(r, Choice) for r in rows)
    assert {r.title for r in rows} == {"alpha", "beta", "gamma"}
    screen.handle("text", "l")  # now 'al' -> only alpha
    assert [r.title for r in screen._rows()] == ["alpha"]
    assert _run(screen, "enter") == 1


def test_select_non_filterable_ignores_typing() -> None:
    """With filtering off, typed keys neither narrow the list nor add a filter line."""
    screen = SelectScreen("pick", [Choice("alpha", 1), Choice("beta", 2)], filterable=False)
    screen.handle("text", "a")
    screen.handle("backspace")
    assert screen._filter == ""
    assert len(screen._rows()) == 2  # nothing was filtered out


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
    items: list = [Separator("── Channels ──")]
    items += [Choice(f"chan{i}", ("c", i)) for i in range(6)]
    items += [Separator("── Direct ──")]
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


def test_select_pinned_heading_keeps_the_last_row_reachable() -> None:
    """Even with a heading pinned, the bottom choice stays fully visible (not clipped)."""
    screen = _grouped_menu()
    screen.handle("end")  # highlight the final choice
    lines = screen.render_body(40)
    visible, _above, below = frame._visible_slice(screen, lines, 6)
    assert Text.from_ansi(visible[0]).plain.strip() == "── Direct ──"  # heading pinned
    assert any("peer7" in Text.from_ansi(row).plain for row in visible)  # last row shown
    assert below is False  # and we know we're at the bottom


def test_screen_sticky_header_picks_the_governing_recorded_header() -> None:
    """The base Screen.sticky_header logic is generic over any recorded headers list.

    Both the select list and the chat transcript reuse it by populating ``_sticky_headers``;
    this exercises the shared rule directly: pin the last header at or above the offset, unless
    it *is* the top row or none sits above it.
    """
    screen = Screen()
    screen._sticky_headers = [(0, "A"), (5, "B"), (12, "C")]
    assert screen.sticky_header(0) is None    # header A is itself the top row
    assert screen.sticky_header(3) == "A"     # scrolled past A, before B → A governs
    assert screen.sticky_header(5) is None     # header B is now the top row
    assert screen.sticky_header(20) == "C"    # below every header → the last one pins
    assert Screen().sticky_header(9) is None  # no recorded headers → nothing to pin


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
    screen._sticky_headers = [(0, "A"), (20, "B"), (60, "C")]
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


def test_compose_startup_shows_footnote_below_box() -> None:
    """A footnote (e.g. a copyright) is drawn, muted and centered, below the box."""
    screen = SelectScreen("pick", [Choice("a", 1)])
    screen.chrome = False
    screen.footnote = "© 2026 Johnputer"
    lines = Text.from_ansi(frame.compose_startup(screen, 80, 20)).plain.split("\n")
    note = next(ln for ln in lines if "Homestead" in ln)
    assert note.strip() == "© 2026 Johnputer"  # its own line
    assert note.startswith("   ")  # centered, not flush-left


def test_compose_startup_box_is_horizontally_centered() -> None:
    """The content-sized box is centered, so its rows carry a leading left margin."""
    screen = SelectScreen("pick", [Choice("a", 1)])
    screen.chrome = False
    lines = Text.from_ansi(frame.compose_startup(screen, 80, 20)).plain.split("\n")
    box_lines = [ln for ln in lines if ln.strip()]
    assert box_lines and all(ln.startswith("  ") for ln in box_lines)  # centered inset


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
    # A copyright footnote rides along for the splash to render below the box.
    assert "Homestead" in captured["footnote"]
    # Each device row's port sits at the same column, proving the name column is padded.
    rows = [
        it.label.plain if hasattr(it.label, "plain") else it.label
        for it in captured["items"]
        if isinstance(it, Choice)
    ]
    assert len(rows) == 3  # two devices plus a trailing Quit row (like the main menu)
    assert rows[-1].strip() == "quit"
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


def test_device_picker_drops_copyright_after_first_selection(tmp_path) -> None:
    """The copyright shows on the opening splash, then never again — even on a re-pick."""
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
    # Only the very first splash carries the copyright; the busy spinner, the failure
    # notice, and the re-opened picker all drop it.
    assert footnotes[0][0] == "select" and "Homestead" in footnotes[0][1]
    assert all(footnote is None for _kind, footnote in footnotes[1:])


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
