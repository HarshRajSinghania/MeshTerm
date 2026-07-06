"""The selectable-list screen: the reusable replacement for ``questionary.select``.

A :class:`SelectScreen` shows grouped, arrow-navigable choices with type-to-filter, bounded
to the viewport and scrolled to keep the highlighted row visible. Choice values are
arbitrary objects, so the same screen drives the main menu (tool names), the device picker
(device objects), and the config editor (setting keys).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from rich.text import Text

from .render import render_to_ansi
from .screen import Screen


@dataclass
class Choice:
    """One selectable row.

    Attributes:
        title: Text shown for the row.
        value: Value returned when the row is chosen.
    """

    title: str
    value: Any


@dataclass
class Separator:
    """A non-selectable group heading.

    Attributes:
        title: The heading text (rendered muted).
    """

    title: str


Item = "Choice | Separator"


class SelectScreen(Screen):
    """A grouped, filterable, single-choice list.

    Resolves with the chosen :class:`Choice` value, or :data:`~meshtools.ui.tui.screen.CANCEL`
    if the user presses Esc.
    """

    def __init__(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        footer_hint: str = "↑↓ move · type to filter · Enter select · Esc back",
    ) -> None:
        """Build a select screen.

        Args:
            title: Heading shown above the list.
            items: A list of :class:`Choice` and :class:`Separator` in display order.
            default: A choice value to pre-highlight, if present.
            footer_hint: Footer key hint.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._items = items
        self._filter = ""
        # Index into the currently-selectable (filtered) choices.
        self._index = 0
        if default is not None:
            selectable = [it for it in items if isinstance(it, Choice)]
            for i, choice in enumerate(selectable):
                if choice.value == default:
                    self._index = i
                    break

    # --- filtering -----------------------------------------------------------

    def _rows(self) -> list:
        """Return the items to display given the active filter.

        Without a filter, groups and headings show as authored. With a filter, headings
        are dropped and only matching choices are shown, flattened.
        """
        if not self._filter:
            return self._items
        needle = self._filter.lower()
        return [
            it for it in self._items if isinstance(it, Choice) and needle in it.title.lower()
        ]

    def _choices(self, rows: Optional[list] = None) -> list:
        """Return just the selectable choices among ``rows`` (or the current rows)."""
        rows = self._rows() if rows is None else rows
        return [it for it in rows if isinstance(it, Choice)]

    # --- rendering -----------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render each row to a single ANSI line, the highlighted choice marked."""
        rows = self._rows()
        choices = self._choices(rows)
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0
        selected = choices[self._index] if choices else None

        lines: list[str] = []
        if self._filter:
            lines.append(render_to_ansi(Text(f"/{self._filter}", style="warn"), width))
        for item in rows:
            if isinstance(item, Separator):
                lines.append(render_to_ansi(Text(item.title, style="muted"), width))
                continue
            is_sel = item is selected
            pointer = "❯ " if is_sel else "  "
            style = "brand" if is_sel else ""
            text = Text(pointer + item.title, style=style, no_wrap=True, overflow="ellipsis")
            text.truncate(width)
            lines.append(render_to_ansi(text, width))
        if not choices:
            lines.append(render_to_ansi(Text("(no matches)", style="muted"), width))
        # Remember where the highlighted row landed so the session can keep it in view.
        self._cursor = _cursor_line(rows, selected, bool(self._filter))
        return lines

    def cursor_line(self) -> Optional[int]:
        """Return the body line index of the highlighted row."""
        return getattr(self, "_cursor", None)

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, edit the filter, or commit/cancel the selection."""
        choices = self._choices()
        if action == "up":
            self._index = (self._index - 1) % len(choices) if choices else 0
        elif action == "down":
            self._index = (self._index + 1) % len(choices) if choices else 0
        elif action == "home":
            self._index = 0
        elif action == "end":
            self._index = max(0, len(choices) - 1)
        elif action == "enter":
            if choices:
                self.resolve(choices[self._index].value)
        elif action == "escape":
            super().handle("escape")
        elif action == "backspace":
            self._filter = self._filter[:-1]
            self._index = 0
        elif action == "text" and data.isprintable():
            self._filter += data
            self._index = 0


class ReorderScreen(Screen):
    """A list whose rows the user rearranges in place with the arrow keys.

    Move the cursor with ↑/↓; press Enter to *grab* the highlighted row, then ↑/↓ carry it
    up and down the list; press Enter again to *drop* it. The screen stays open until Esc,
    at which point it resolves with the final order as a list of the original row indices
    (so ``[2, 0, 1]`` means "the row that started third is now first").
    """

    def __init__(self, title: str, labels: list[str]) -> None:
        """Build a reorder screen.

        Args:
            title: Heading shown above the list.
            labels: The row labels, in their current order.
        """
        super().__init__()
        self.title = title
        self._labels = list(labels)
        # order[position] == the label's original index; a moved row carries its index along.
        self._order = list(range(len(labels)))
        self._index = 0
        self._grabbed = False

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Key hint, phrased for whether a row is currently grabbed."""
        if self._grabbed:
            return "↑↓ move row · Enter drop · Esc done"
        return "↑↓ choose · Enter grab · Esc done"

    def render_body(self, width: int) -> list[str]:
        """Render each row, marking the cursor (and, when grabbed, the moving row)."""
        lines: list[str] = []
        for pos, orig in enumerate(self._order):
            is_cursor = pos == self._index
            if is_cursor and self._grabbed:
                pointer, style = "▸ ", "brand"
            elif is_cursor:
                pointer, style = "❯ ", "brand"
            else:
                pointer, style = "  ", ""
            text = Text(pointer + self._labels[orig], style=style, no_wrap=True,
                        overflow="ellipsis")
            text.truncate(width)
            lines.append(render_to_ansi(text, width))
        self._cursor = self._index
        return lines

    def cursor_line(self) -> Optional[int]:
        """Return the body line index of the cursor row, so the session keeps it in view."""
        return getattr(self, "_cursor", None)

    def handle(self, action: str, data: str = "") -> None:
        """Move the cursor, carry a grabbed row, toggle grab, or finish on Esc."""
        n = len(self._order)
        if action == "up":
            if self._grabbed and self._index > 0:
                self._order[self._index - 1], self._order[self._index] = (
                    self._order[self._index], self._order[self._index - 1])
                self._index -= 1
            elif not self._grabbed and n:
                self._index = (self._index - 1) % n
        elif action == "down":
            if self._grabbed and self._index < n - 1:
                self._order[self._index + 1], self._order[self._index] = (
                    self._order[self._index], self._order[self._index + 1])
                self._index += 1
            elif not self._grabbed and n:
                self._index = (self._index + 1) % n
        elif action == "enter":
            self._grabbed = not self._grabbed
        elif action == "escape":
            self.resolve(list(self._order))


def _cursor_line(rows: list, selected: Optional[Choice], filtered: bool) -> Optional[int]:
    """Compute the rendered body line index of the selected row.

    Args:
        rows: The displayed rows (choices and separators).
        selected: The currently highlighted choice, if any.
        filtered: Whether a filter line precedes the rows (offsetting every row by one).

    Returns:
        The zero-based line index of the highlighted row, or ``None`` if nothing is
        selected.
    """
    if selected is None:
        return None
    offset = 1 if filtered else 0
    for i, item in enumerate(rows):
        if item is selected:
            return i + offset
    return None
