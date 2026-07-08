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
from meshterm.ui.tui.prompt import AutocompleteScreen, ConfirmScreen, TextScreen
from meshterm.ui.tui.render import render_lines, render_to_ansi
from meshterm.ui.tui.screen import CANCEL, ScrollScreen
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


# --- scroll ------------------------------------------------------------------


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


def test_text_screen_password_masks() -> None:
    """A password field renders bullets, not the typed characters."""
    screen = TextScreen("pw?", password=True)
    for ch in "secret":
        screen.handle("text", ch)
    rendered = "\n".join(screen.render_body(40))
    assert "secret" not in rendered
    assert "•" in rendered


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


def test_device_picker_builds_aligned_columns() -> None:
    """The picker lays devices out in columns that line up across rows of differing widths."""
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
            return None

    asyncio.run(prompt_device(_Ui(), devices, None))
    # The banner (wordmark) is passed through so the splash can draw it.
    assert captured["banner"] and any("█" in row for row in captured["banner"])
    # A copyright footnote rides along for the splash to render below the box.
    assert "Homestead" in captured["footnote"]
    # Each device row's port sits at the same column, proving the name column is padded.
    rows = [it.label.plain for it in captured["items"] if isinstance(it, Choice)]
    assert len(rows) == 2
    assert all(port in row for port, row in zip(("COM5", "/dev/ttyUSB0"), rows))
    assert rows[0].index("COM5") == rows[1].index("/dev/ttyUSB0")


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
