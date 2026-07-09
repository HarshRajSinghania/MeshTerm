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

#: A validator returns ``True`` when the input is acceptable, or an error message to show.
Validator = Callable[[str], "bool | str"]


class _LineEditor:
    """A minimal single-line text editor (insert, delete, and cursor movement).

    Shared by :class:`TextScreen` and :class:`AutocompleteScreen`. Tracks the text and the
    cursor position; rendering shows the cursor as a reverse-video cell.
    """

    def __init__(self, initial: str = "") -> None:
        """Start the editor with ``initial`` text and the cursor at its end."""
        self.text = initial
        self.cursor = len(initial)

    def edit(self, action: str, data: str = "") -> bool:
        """Apply an editing action, returning ``True`` if it changed the buffer/cursor.

        Args:
            action: The normalized key action.
            data: The character to insert when ``action`` is ``text``.

        Returns:
            ``True`` if the action was an editing action handled here.
        """
        if action == "text" and data.isprintable():
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

    def render(self, mask: bool = False, *, overflow_at: Optional[int] = None) -> Text:
        """Render the current line with a reverse-video cursor cell.

        Args:
            mask: When ``True``, replace each character with a bullet (password entry).
            overflow_at: Character index at which the text spills past a byte budget; that
                character and everything after it are shown in the error style so the user
                can see exactly what to trim. ``None`` (the default) styles the line plainly.
        """
        shown = "•" * len(self.text) if mask else self.text
        text = Text("› ", style="accent")
        for i, ch in enumerate(shown):
            over = overflow_at is not None and i >= overflow_at
            if i == self.cursor:
                text.append(ch, style="reverse err" if over else "reverse")
            else:
                text.append(ch, style="err" if over else None)
        if self.cursor >= len(shown):  # cursor past the last character → trailing block
            text.append(" ", style="reverse")
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
        text.append("  Yes  ", style="reverse brand" if self._value else "muted")
        text.append("   ")
        text.append("  No  ", style="muted" if self._value else "reverse brand")
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
        button_style: str = "reverse brand",
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
        row = Text(justify="center")
        for i, (label, _value) in enumerate(self._buttons):
            if i:
                row.append("    ")
            style = self._button_style if i == self._index else self._button_idle_style
            row.append(f"  {label}  ", style=style)
        prompt = Text(self._prompt, style=self._prompt_style, justify="center")
        return render_lines(Group(prompt, Text(""), row), width)

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
