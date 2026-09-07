"""Tests for the TUI output surface's window-or-popup presentation.

:meth:`TuiUi.present` upgrades a short, text-only result ("✓ flood advertisement sent")
to a centered OK dialog instead of a full scrollable window. These cover the collapse
gate (:func:`_collapse_to_message`), the routing between the two presentations, and the
severity-to-border mapping the message dialog applies (:func:`_message_border`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.table import Table
from rich.text import Text

from meshterm.ui.surface import _DIALOG_MAX_CELLS, TuiUi, _collapse_to_message
from meshterm.ui.tui.session import TuiSession, _message_border

# -- the collapse gate ------------------------------------------------------------


def test_collapse_accepts_a_short_note_and_keeps_its_styling() -> None:
    """A one-line success note qualifies, with its markup spans intact."""
    note = Text.from_markup("[ok]✓[/ok] flood advertisement sent")
    message = _collapse_to_message([note])
    assert message is not None
    assert message.plain == "✓ flood advertisement sent"
    assert any(str(span.style) == "ok" for span in message.spans)


def test_collapse_joins_a_few_notes_into_one_message() -> None:
    """Two or three separate notes collapse into one multi-line dialog message."""
    notes = [Text("✓ device clock set"), Text("● wrote backup.toml")]
    message = _collapse_to_message(notes)
    assert message is not None
    assert message.plain == "✓ device clock set\n● wrote backup.toml"


def test_collapse_rejects_tall_output() -> None:
    """More lines than fit a tidy popup fall back to the result window."""
    assert _collapse_to_message([Text(f"line {i}") for i in range(4)]) is None


def test_collapse_rejects_wide_lines() -> None:
    """A line too wide for the popup (a long path, say) falls back to the window."""
    assert _collapse_to_message([Text("x" * (_DIALOG_MAX_CELLS + 1))]) is None


def test_collapse_rejects_non_text_renderables() -> None:
    """Any table/panel in the buffer sends the whole result to the window."""
    assert _collapse_to_message([Text("note"), Table()]) is None
    assert _collapse_to_message([]) is None


# -- present() routing --------------------------------------------------------------


class _RecordingSession:
    """Stands in for :class:`TuiSession`, recording which presentation was used."""

    def __init__(self) -> None:
        self.dialogs: list[tuple[Text, str]] = []
        self.scrolls: list[tuple[Any, str]] = []

    async def message_dialog(self, message: Text, *, title: str = "") -> None:
        self.dialogs.append((message, title))

    async def scroll(self, body: Any, *, title: str = "", footer_hint: str = "") -> None:
        self.scrolls.append((body, title))


async def test_present_floats_a_short_note_as_a_dialog() -> None:
    """A single outcome note pops as an OK dialog, not a full result window."""
    session = _RecordingSession()
    ui = TuiUi(session)  # type: ignore[arg-type]
    ui.note("[ok]✓[/ok] zero-hop advertisement sent")
    await ui.present(title="Advert")
    assert session.scrolls == []
    (message, title), = session.dialogs
    assert message.plain == "✓ zero-hop advertisement sent"
    assert title == "Advert"


async def test_present_keeps_big_output_in_the_result_window() -> None:
    """Tables (and any oversized output) still open the scrollable window."""
    session = _RecordingSession()
    ui = TuiUi(session)  # type: ignore[arg-type]
    ui.note("heading")
    ui.show(Table(title="nodes"))
    await ui.present(title="Nodes")
    assert session.dialogs == []
    assert [title for _, title in session.scrolls] == ["Nodes"]


async def test_present_clears_the_buffer_either_way() -> None:
    """After presenting, a second present has nothing to show (no double popup)."""
    session = _RecordingSession()
    ui = TuiUi(session)  # type: ignore[arg-type]
    ui.note("✓ done")
    await ui.present(title="Once")
    await ui.present(title="Twice")
    assert len(session.dialogs) == 1 and session.scrolls == []


# -- the popup end-to-end through a real session ------------------------------------


async def test_message_dialog_floats_over_a_blank_backdrop_end_to_end() -> None:
    """On an empty stack the popup pushes a blank base beneath itself, then pops both.

    This is the main-menu tool path: the menu is popped while a tool runs, so without
    the backdrop the lone dialog would be drawn as the base (full-frame). Driving a
    real session with piped keys proves Enter (OK) dismisses it and the stack unwinds.
    """
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())

        async def main() -> None:
            inp.send_text("\r")  # Enter commits the OK button
            await session.message_dialog(
                Text.from_markup("[ok]✓[/ok] flood advertisement sent"), title="Advert"
            )
            assert session._stack == []  # dialog and its backdrop both popped

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_confirm_floats_over_an_existing_popup_end_to_end() -> None:
    """A confirm floats over an existing popup, end to end.

    Opened over a floating detail popup, it renders and resolves: the render loop runs
    the multi-layer float pool for real (base + detail + confirm), which is the case
    the old single-float compositor stretched the detail to full-frame.
    """
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from meshterm.ui.tui.screen import ScrollScreen
    from meshterm.ui.tui.select import Choice, SelectScreen

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())

        async def main() -> None:
            base = ScrollScreen(Text("channels"), title="Channels")  # full-frame background
            detail = SelectScreen("Ops", [Choice("Clear", "clr")])  # a floating popup
            session.push(base)
            session.push(detail)
            # Two layers float over the one background while the confirm is up.
            assert session._base_screen() is base
            inp.send_text("\r")  # Enter commits the confirm's default (Delete)
            confirmed = await session.button_dialog(
                "Clear Ops?", [("Cancel", 0), ("Clear", 1)], default=1, border_style="err"
            )
            assert confirmed == 1
            assert session._float_layers() == [detail]  # confirm gone, detail still afloat
            session.pop(detail)
            session.pop(base)

        await asyncio.wait_for(session.run(main()), timeout=5)


# -- the message dialog's border tone -----------------------------------------------


def test_message_border_echoes_the_strongest_tone() -> None:
    """Err outranks warn; warn outranks the neutral accent; ok stays neutral."""
    err = Text.from_markup("[err]✗ Trace failed:[/err] timeout")
    warn = Text.from_markup("[warn]device rebooting[/warn]")
    ok = Text.from_markup("[ok]✓[/ok] private key imported")
    both = Text.from_markup("[warn]careful[/warn] [err]broken[/err]")
    assert _message_border(err) == "err"
    assert _message_border(warn) == "warn"
    assert _message_border(ok) == "accent"
    assert _message_border(both) == "err"
    assert _message_border("plain string") == "accent"


# -- the button dialog's caution tiers ----------------------------------------------


class _ButtonRecordingSession:
    """Records the styling kwargs :meth:`TuiUi.dialog` hands the button dialog."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def button_dialog(self, prompt: str, buttons: list, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return buttons[-1][1]


async def _dialog_styles(**kw: Any) -> dict[str, Any]:
    session = _ButtonRecordingSession()
    ui = TuiUi(session)  # type: ignore[arg-type]
    await ui.dialog("Delete this record?", [("Cancel", False), ("Delete", True)], **kw)
    return session.kwargs


async def test_dialog_destructive_tier_borders_red() -> None:
    """A data-loss dialog draws in the reserved error red — prompt and border."""
    styles = await _dialog_styles(destructive=True)
    assert styles["border_style"] == "err"
    assert styles["prompt_style"] == "err"


async def test_dialog_danger_tier_stays_amber() -> None:
    """A merely-disruptive dialog keeps the amber caution tone, never the deletion red."""
    styles = await _dialog_styles(danger=True)
    assert styles["border_style"] == "warn"
    assert styles["prompt_style"] == "warn"


async def test_dialog_plain_is_neutral_and_destructive_outranks_danger() -> None:
    """No flag is the neutral accent frame; destructive wins over danger when both are set."""
    plain = await _dialog_styles()
    assert plain["border_style"] == "accent" and plain["prompt_style"] == ""
    both = await _dialog_styles(danger=True, destructive=True)
    assert both["border_style"] == "err" and both["prompt_style"] == "err"
