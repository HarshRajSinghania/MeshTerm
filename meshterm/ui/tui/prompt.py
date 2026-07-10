"""Input screens: text, confirm, and autocomplete — the ``questionary`` replacements.

These reimplement the small slice of ``questionary`` the app uses (a validated line editor,
a yes/no toggle, and a free-text field with suggestions) as uniform TUI screens, so every
prompt shares the framework's look, layering, resize, and Esc-to-cancel behavior.
"""

from __future__ import annotations

from typing import Callable, Optional

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.text import Text

from .render import render_lines
from .screen import Screen
from .spinner import Spinner

#: A validator returns ``True`` when the input is acceptable, or an error message to show.
Validator = Callable[[str], "bool | str"]


def _center(content: Text, width: int) -> Text:
    """Center ``content`` in ``width`` with plain padding on both sides.

    Used instead of ``Text(justify="center")`` for lines that hold a reverse-video button
    chip: Rich strips a styled span's *trailing* whitespace when it sits directly against
    justify padding, which would shave the right edge off the chip (``"  Quit"`` instead of
    ``"  Quit  "``) and leave the label hugging the left of the fill. Padding manually keeps
    a real segment after the chip, so its trailing cells survive and the fill stays symmetric.
    """
    pad = max(0, width - content.cell_len)
    left = pad // 2
    line = Text(" " * left)
    line.append_text(content)
    line.append(" " * (pad - left))
    return line


class _LineEditor:
    """A minimal single-line text editor (insert, delete, and cursor movement).

    Shared by :class:`TextScreen` and :class:`AutocompleteScreen`. Tracks the text and the
    cursor position; rendering shows the cursor as a reverse-video cell.
    """

    def __init__(self, initial: str = "", *, max_length: Optional[int] = None) -> None:
        """Start the editor with ``initial`` text and the cursor at its end.

        Args:
            initial: The starting buffer contents.
            max_length: Optional hard cap on the number of characters; further insertions are
                dropped (or a paste truncated to fit). ``None`` leaves the length unbounded.
        """
        self.text = initial
        self.cursor = len(initial)
        self._max_length = max_length

    def edit(self, action: str, data: str = "") -> bool:
        """Apply an editing action, returning ``True`` if it changed the buffer/cursor.

        Args:
            action: The normalized key action.
            data: The character to insert when ``action`` is ``text``.

        Returns:
            ``True`` if the action was an editing action handled here.
        """
        if action == "text" and data.isprintable():
            if self._max_length is not None:
                room = self._max_length - len(self.text)
                if room <= 0:
                    return False  # at capacity — swallow the key without changing the buffer
                data = data[:room]  # a multi-char paste fills only the remaining slots
            self.text = self.text[: self.cursor] + data + self.text[self.cursor :]
            self.cursor += len(data)
        elif action == "backspace" and self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1
        elif action == "delete" and self.cursor < len(self.text):
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
        elif action == "left":
            self.cursor = max(0, self.cursor - 1)
        elif action == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif action == "home":
            self.cursor = 0
        elif action == "end":
            self.cursor = len(self.text)
        elif action == "ctrl_left":
            self.cursor = self._word_left(self.cursor)
        elif action == "ctrl_right":
            self.cursor = self._word_right(self.cursor)
        else:
            return False
        return True

    def _word_left(self, pos: int) -> int:
        """Index of the start of the word at/left of ``pos`` (the previous word if already there).

        Skips any whitespace immediately left of the cursor, then the run of word characters, so
        from mid-word it lands on that word's first character and from a word start it steps back
        to the previous word — the usual Ctrl+Left behavior of a text editor.
        """
        i = pos
        while i > 0 and self.text[i - 1].isspace():
            i -= 1
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        return i

    def _word_right(self, pos: int) -> int:
        """Index of the start of the next word after ``pos`` (or the line end if none remains).

        Skips the current run of word characters, then the whitespace after it, landing on the
        first character of the following word — the usual Ctrl+Right behavior.
        """
        n = len(self.text)
        i = pos
        while i < n and not self.text[i].isspace():
            i += 1
        while i < n and self.text[i].isspace():
            i += 1
        return i

    def render(
        self, mask: bool = False, *, overflow_at: Optional[int] = None, slots: Optional[int] = None
    ) -> Text:
        """Render the current line with a reverse-video cursor cell.

        Args:
            mask: When ``True``, replace each character with a bullet (password entry).
            overflow_at: Character index at which the text spills past a byte budget; that
                character and everything after it are shown in the error style so the user
                can see exactly what to trim. ``None`` (the default) styles the line plainly.
            slots: When set, render exactly this many fixed positions (a masked PIN field):
                typed positions show a bullet, still-blank ones a muted centre dot. Takes
                precedence over ``mask``/``overflow_at``.
        """
        if slots is not None:
            return self._render_slots(slots)
        shown = "•" * len(self.text) if mask else self.text
        text = Text("› ", style="accent")
        for i, ch in enumerate(shown):
            over = overflow_at is not None and i >= overflow_at
            if i == self.cursor:
                text.append(ch, style="err.reverse" if over else "reverse")
            else:
                text.append(ch, style="err" if over else None)
        if self.cursor >= len(shown):  # cursor past the last character → trailing block
            text.append(" ", style="reverse")
        return text

    def _render_slots(self, slots: int) -> Text:
        """Render a fixed-width masked field: filled bullets and centred dots for blanks.

        Used for PIN entry — each of ``slots`` positions shows a bullet (``•``) once typed and
        a muted centre dot (``·``) while still blank, spaced apart so the field reads as a row
        of PIN boxes rather than a growing line. The cursor position is drawn reverse-video.
        """
        filled = len(self.text)
        text = Text("› ", style="accent")
        for i in range(slots):
            if i:
                text.append(" ")
            glyph = "•" if i < filled else "·"
            if i == self.cursor:
                text.append(glyph, style="reverse")  # the active slot
            elif i < filled:
                text.append(glyph)  # an entered digit
            else:
                text.append(glyph, style="muted")  # a blank still to fill
        return text


class TextScreen(Screen):
    """A validated single-line text prompt. Resolves with the string, or CANCEL on Esc."""

    def __init__(
        self,
        title: str,
        *,
        default: str = "",
        validate: Optional[Validator] = None,
        help_text: str = "",
        password: bool = False,
        footer_hint: str = "Enter accept · Esc cancel",
    ) -> None:
        """Build a text prompt.

        Args:
            title: The question shown above the field.
            default: Prefilled text.
            validate: Optional validator run on Enter; a returned string is shown as an
                error and blocks submission.
            help_text: Optional muted hint shown under the field.
            password: When ``True``, mask the entered text.
            footer_hint: Footer key hint.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._editor = _LineEditor(default)
        self._validate = validate
        self._help = help_text
        self._password = password
        self._error = ""

    def render_body(self, width: int) -> list[str]:
        """Render the field, any help text, and the current validation error."""
        parts: list[RenderableType] = [self._editor.render(mask=self._password)]
        if self._help:
            parts.append(Text(self._help, style="muted"))
        if self._error:
            parts.append(Text(self._error, style="err"))
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Edit the buffer, submit on Enter (if valid), or cancel on Esc."""
        if action == "enter":
            value = self._editor.text
            if self._validate is not None:
                result = self._validate(value)
                if result is not True:
                    self._error = str(result)
                    return
            self.resolve(value)
        elif action == "escape":
            super().handle("escape")
        else:
            if self._editor.edit(action, data):
                self._error = ""


class PinDialog(Screen):
    """A startup popup that collects a Bluetooth pairing PIN, re-asking on a rejected code.

    Shown by the device picker when a chosen companion answers the scan but refuses the GATT
    connection until it is bonded. It presents a masked field centered in its own bordered box
    under the wordmark — the same chromeless-splash presentation as the picker and its spinner
    — so it reads as one more step of the startup flow rather than a context switch.

    It only *collects* a PIN; verifying it means actually opening the BLE connection, which the
    picker does. So a rejected code isn't detected here — the picker catches the authentication
    failure and re-opens this dialog with ``error`` set, which is why the field starts empty each
    time. Resolves with the entered PIN, or :data:`CANCEL` on Esc (the user gave up — the picker
    returns them to the device list).
    """

    footer_hint = "Enter connect · Esc cancel"

    #: MeshCore pairing PINs are a fixed six digits, so the field is capped at six characters
    #: and drawn as six slots (typed digits as bullets, blanks as centre dots).
    PIN_LENGTH = 6

    def __init__(self, device_name: str, *, error: str = "", help_text: str = "") -> None:
        """Build the PIN dialog.

        Args:
            device_name: The companion's display name, woven into the prompt.
            error: A message shown in the error style — set by the picker on a re-ask after a
                rejected PIN; empty on the first ask.
            help_text: A muted hint under the field (e.g. where to read the code).
        """
        super().__init__()
        self.title = "Bluetooth PIN required"
        self._device = device_name
        self._error = error
        self._help = help_text
        self._editor = _LineEditor("", max_length=self.PIN_LENGTH)

    def render_body(self, width: int) -> list[str]:
        """Render the prompt, the six-slot PIN field, any hint, and a rejected-PIN error."""
        prompt = Text()
        prompt.append(self._device, style="brand")
        prompt.append(" needs a pairing PIN to connect.")
        field = self._editor.render(slots=self.PIN_LENGTH)
        parts: list[RenderableType] = [prompt, Text(""), field]
        if self._help:
            parts.append(Text(self._help, style="muted"))
        if self._error:
            parts.append(Text(self._error, style="err"))
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Edit the field, submit a non-empty PIN on Enter, or cancel on Esc."""
        if action == "enter":
            pin = self._editor.text.strip()
            if not pin:  # an empty PIN can't be right — nudge rather than pointlessly retry
                self._error = "Enter the PIN, or press Esc to cancel."
                return
            self.resolve(pin)
        elif action == "escape":
            super().handle("escape")
        elif self._editor.edit(action, data):
            self._error = ""


class ConfirmScreen(Screen):
    """A yes/no prompt. Resolves with a bool, or CANCEL on Esc."""

    def __init__(
        self,
        title: str,
        *,
        default: bool = True,
        footer_hint: str = "←→/Y/N choose · Enter accept · Esc cancel",
    ) -> None:
        """Build a confirm prompt.

        Args:
            title: The yes/no question.
            default: The initially highlighted answer.
            footer_hint: Footer key hint.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._value = default

    def render_body(self, width: int) -> list[str]:
        """Render the Yes / No options with the current choice highlighted."""
        text = Text()
        text.append("  Yes  ", style="selected" if self._value else "muted")
        text.append("   ")
        text.append("  No  ", style="muted" if self._value else "selected")
        return render_lines(text, width)

    def handle(self, action: str, data: str = "") -> None:
        """Toggle the choice, submit on Enter, or cancel on Esc."""
        if action in ("left", "right", "tab"):
            self._value = not self._value
        elif action == "text" and data.lower() in ("y", "n"):
            self._value = data.lower() == "y"
        elif action == "enter":
            self.resolve(self._value)
        elif action == "escape":
            super().handle("escape")


class ButtonDialog(Screen):
    """A centered dialog: a prompt above a row of side-by-side buttons.

    A reusable choose-one prompt. The buttons sit in a row; ←/→ (or Tab) move the highlight,
    Enter commits the highlighted button, and Esc cancels. Optional single-key shortcuts
    commit a button instantly (e.g. ``y``/``n``, self-hinted by ``Yes``/``No`` labels). The
    highlight and border colours are parametrised so a caller can theme it (a destructive
    action in red, say). Resolves with the chosen button's value, or CANCEL on Esc.
    """

    def __init__(
        self,
        prompt: str,
        buttons: list[tuple[str, object]],
        *,
        title: str = "",
        default: int = 0,
        keys: Optional[dict[str, object]] = None,
        footer_hint: str = "←→ choose · Enter select · Esc cancel",
        prompt_style: str = "",
        button_style: str = "selected",
        button_idle_style: str = "muted",
        border_style: str = "accent",
    ) -> None:
        """Build a button dialog.

        Args:
            prompt: The question shown above the buttons.
            buttons: ``(label, value)`` pairs laid out left to right.
            title: Optional dialog heading.
            default: Index of the initially highlighted button.
            keys: Optional map of a lowercase shortcut character to the value it commits
                immediately (bypassing the highlight), e.g. ``{"y": True, "n": False}``.
            footer_hint: Footer key hint (hints Enter and Esc, as the other dialogs do).
            prompt_style: Rich style for the question (e.g. ``"warn"`` for a cautionary
                action); empty for the default foreground.
            button_style: Rich style for the highlighted button.
            button_idle_style: Rich style for the un-highlighted buttons.
            border_style: Rich style for the dialog border (read by the frame compositor).
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self.border_style = border_style
        self._prompt = prompt
        self._buttons = buttons
        self._index = default if 0 <= default < len(buttons) else 0
        self._keys = {k.lower(): v for k, v in (keys or {}).items()}
        self._prompt_style = prompt_style
        self._button_style = button_style
        self._button_idle_style = button_idle_style

    @property
    def _button_row_width(self) -> int:
        """Display width of the button row (``  Label  `` cells joined by 4-space gaps)."""
        cells = sum(cell_len(label) + 4 for label, _ in self._buttons)
        gaps = 4 * max(0, len(self._buttons) - 1)
        return cells + gaps

    @property
    def dialog_width(self) -> int:
        """Natural outer width so the frame sizes the box to its content, not the terminal.

        The widest of the prompt, button row, title, and footer hint, plus a comfortable
        margin and the border — so a short confirm reads as a tidy box rather than a banner
        stretched across the screen. The compositor still caps this to the terminal.
        """
        inner = max(
            cell_len(self._prompt),
            self._button_row_width,
            cell_len(self.title),
            cell_len(self.footer_hint),
        )
        return inner + 12  # panel padding + border, plus horizontal breathing room

    def render_body(self, width: int) -> list[str]:
        """Render the prompt centered above a centered row of buttons."""
        row = Text()
        for i, (label, _value) in enumerate(self._buttons):
            if i:
                row.append("    ")
            style = self._button_style if i == self._index else self._button_idle_style
            row.append(f"  {label}  ", style=style)
        prompt = Text(self._prompt, style=self._prompt_style)
        return render_lines(Group(_center(prompt, width), Text(""), _center(row, width)), width)

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, commit on Enter or a shortcut key, or cancel on Esc."""
        if action in ("left", "right", "tab") and self._buttons:
            step = -1 if action == "left" else 1
            self._index = (self._index + step) % len(self._buttons)
        elif action == "text" and data.lower() in self._keys:
            self.resolve(self._keys[data.lower()])
        elif action == "enter" and self._buttons:
            self.resolve(self._buttons[self._index][1])
        elif action == "escape":
            super().handle("escape")


class ReconnectDialog(Screen):
    """A centered dialog shown when the companion link drops mid-session.

    An animated spinner and message sit above a single ``Abort`` button. Unlike the other
    dialogs there is nothing to *choose*: the app watches for the device to return and
    dismisses this screen itself (resolving its future) the moment the reconnect succeeds, so
    the user's only action is to give up and abort. Only Enter resolves ``"quit"``; Esc is
    deliberately inert, so an idle habit of pressing Esc can't drop the session while a replug
    may be a second away. The button uses the shared ``selected`` highlight style (the theme's
    cyan) — the same fill as the quit-confirmation dialog — so every popup dialog reads alike.
    The animation is the reusable :class:`~meshterm.ui.tui.spinner.Spinner`, advanced by
    :meth:`tick` from the session's animation timer.
    """

    def __init__(
        self,
        message: str,
        *,
        title: str = "Device disconnected",
        footer_hint: str = "Enter abort",
        prompt_style: str = "warn",
        border_style: str = "warn",
    ) -> None:
        """Build the reconnect dialog.

        Args:
            message: The line shown beside the spinner (e.g. "Waiting for your device…").
            title: Dialog heading.
            footer_hint: Footer key hint (only Abort is offered).
            prompt_style: Rich style for the message (``"warn"`` for the cautionary tone).
            border_style: Rich style for the dialog border (read by the frame compositor).
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self.border_style = border_style
        self._message = message
        self._prompt_style = prompt_style
        self._spinner = Spinner()

    @property
    def dialog_width(self) -> int:
        """Natural outer width so the frame sizes the box to its content (see ButtonDialog)."""
        inner = max(
            cell_len(self._message),
            cell_len(self.title),
            cell_len(self.footer_hint),
            len("  Abort  "),
        )
        return inner + 12  # panel padding + border, plus horizontal breathing room

    def set_message(self, message: str) -> None:
        """Replace the line shown beside the spinner (e.g. to note a stalled retry)."""
        self._message = message

    def tick(self) -> None:
        """Advance the spinner to its next frame (driven by the session's animation timer)."""
        self._spinner.tick()

    def render_body(self, width: int) -> list[str]:
        """Render the spinner and message centered above a single Abort button."""
        line = self._spinner.text()
        line.append("  ")
        line.append(self._message, style=self._prompt_style)
        row = Text()
        row.append("  Abort  ", style="selected")
        return render_lines(Group(_center(line, width), Text(""), _center(row, width)), width)

    def handle(self, action: str, data: str = "") -> None:
        """Abort only on Enter; ignore everything else, Esc included (the app auto-dismisses)."""
        if action == "enter":
            self.resolve("quit")


class AutocompleteScreen(Screen):
    """A free-text field with a live suggestion list (``questionary.autocomplete``).

    The user may type any value; matching suggestions appear below and can be highlighted
    with the arrows and accepted into the field with Tab. Enter commits the typed text
    (after validation). Resolves with the string, or CANCEL on Esc.
    """

    #: Cap the suggestion list so it never dominates the dialog.
    MAX_SUGGESTIONS = 8

    def __init__(
        self,
        title: str,
        choices: list[str],
        *,
        default: str = "",
        validate: Optional[Validator] = None,
        footer_hint: str = "type · ↑↓ Tab complete · Enter accept · Esc cancel",
    ) -> None:
        """Build an autocomplete prompt.

        Args:
            title: The question shown above the field.
            choices: Suggestion strings to match against.
            default: Prefilled text.
            validate: Optional validator run on Enter.
            footer_hint: Footer key hint.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._editor = _LineEditor(default)
        self._choices = choices
        self._validate = validate
        self._error = ""
        self._sugg = 0

    def _suggestions(self) -> list[str]:
        """Return suggestions matching the current text, capped for display."""
        needle = self._editor.text.lower()
        if not needle:
            matches = list(self._choices)
        else:
            matches = [c for c in self._choices if needle in c.lower()]
        return matches[: self.MAX_SUGGESTIONS]

    def render_body(self, width: int) -> list[str]:
        """Render the field, the matching suggestions, and any error."""
        parts: list[RenderableType] = [self._editor.render()]
        suggestions = self._suggestions()
        self._sugg = max(0, min(self._sugg, len(suggestions) - 1)) if suggestions else 0
        for i, sug in enumerate(suggestions):
            is_sel = i == self._sugg
            row = Text(("❯ " if is_sel else "  ") + sug, style="brand" if is_sel else "muted")
            row.truncate(width)
            parts.append(row)
        if self._error:
            parts.append(Text(self._error, style="err"))
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Edit the field, move/accept suggestions, submit on Enter, or cancel on Esc."""
        suggestions = self._suggestions()
        if action == "up":
            self._sugg = (self._sugg - 1) % len(suggestions) if suggestions else 0
        elif action == "down":
            self._sugg = (self._sugg + 1) % len(suggestions) if suggestions else 0
        elif action == "tab":
            if suggestions:
                self._editor = _LineEditor(suggestions[self._sugg])
                self._error = ""
        elif action == "enter":
            value = self._editor.text
            if self._validate is not None:
                result = self._validate(value)
                if result is not True:
                    self._error = str(result)
                    return
            self.resolve(value)
        elif action == "escape":
            super().handle("escape")
        else:
            if self._editor.edit(action, data):
                self._error = ""
                self._sugg = 0
