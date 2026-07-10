"""The selectable-list screen: the reusable replacement for ``questionary.select``.

A :class:`SelectScreen` shows grouped, arrow-navigable choices with type-to-filter, bounded
to the viewport and scrolled to keep the highlighted row visible. Choice values are
arbitrary objects, so the same screen drives the main menu (tool names), the device picker
(device objects), and the config editor (setting keys).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from rich.cells import cell_len
from rich.text import Text

from .render import render_lines, render_to_ansi
from .screen import Screen


@dataclass
class Choice:
    """One selectable row.

    Attributes:
        title: Text shown for the row — a plain string or a Rich :class:`~rich.text.Text`
            (for a coloured segment such as an unread badge). Either may instead be a
            zero-argument callable resolved fresh on every repaint, so a row can track state
            that changes while the list is open.
        value: Value returned when the row is chosen.
    """

    title: Union[str, Text, Callable[[], Union[str, Text]]]
    value: Any

    @property
    def label(self) -> Union[str, Text]:
        """The row's current text, resolving a callable title on each read."""
        return self.title() if callable(self.title) else self.title


def _plain(label: Union[str, Text]) -> str:
    """The plain-text form of a row label, for filtering (a :class:`Text` keeps its ``plain``)."""
    return label.plain if isinstance(label, Text) else label


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

    Resolves with the chosen :class:`Choice` value, or :data:`~meshterm.ui.tui.screen.CANCEL`
    if the user presses Esc.
    """

    def __init__(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        footer_hint: str = "↑↓ move · type to filter · Enter select · Esc back",
        filterable: bool = True,
        wrap: bool = True,
    ) -> None:
        """Build a select screen.

        Args:
            title: Short heading shown in the border.
            items: A list of :class:`Choice` and :class:`Separator` in display order.
            prompt: An optional instruction shown inside the box, above the list — so a
                floating select reads like the other dialogs (a prompt above its controls).
            default: A choice value to pre-highlight, if present.
            footer_hint: Footer key hint.
            filterable: Whether typing narrows the list. Off for short, fixed lists (e.g.
                the startup device picker) where type-to-filter would only get in the way.
            wrap: Whether the highlight wraps around the ends (Down from the last row jumps
                to the first, and vice versa). Off for grouped lists where wrapping across the
                section headings reads as a jarring jump rather than continuing to scroll.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._prompt = prompt
        self._filterable = filterable
        self._wrap = wrap
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

        Without a filter, groups and headings show as authored. With a filter, only the
        matching choices are shown — but every separator stays, so the list keeps its
        section landmarks (and column headers) while it narrows.
        """
        if not self._filter:
            return self._items
        needle = self._filter.lower()
        return [
            it
            for it in self._items
            if isinstance(it, Separator) or needle in _plain(it.label).lower()
        ]

    def _choices(self, rows: Optional[list] = None) -> list:
        """Return just the selectable choices among ``rows`` (or the current rows)."""
        rows = self._rows() if rows is None else rows
        return [it for it in rows if isinstance(it, Choice)]

    def _section_starts(self) -> list[int]:
        """Choice indices that begin a section — the first choice after each run of separators.

        Drives Ctrl+PageUp/PageDown section jumps. Computed over the *displayed* rows, so
        the jumps keep working while a filter narrows the list (the headings stay — see
        :meth:`_rows` — and empty sections simply yield no stop).
        """
        starts: list[int] = []
        idx = 0
        fresh = True  # the next choice opens a section (top of the list, or just past a heading)
        for item in self._rows():
            if isinstance(item, Separator):
                fresh = True
            elif isinstance(item, Choice):
                if fresh:
                    starts.append(idx)
                    fresh = False
                idx += 1
        return starts

    def _jump_section(self, direction: int) -> None:
        """Move the highlight to the next section (``+1``) or the current/previous one (``-1``).

        Mirrors the scroll-based section jump of read-only screens, but in choice space: down
        lands on the first choice of the following section; up lands on the first choice of the
        section we're in, or the previous section's when already at a section start.
        """
        starts = self._section_starts()
        if not starts:
            return
        if direction > 0:
            nxt = next((s for s in starts if s > self._index), None)
            if nxt is not None:
                self._index = nxt
        else:
            at_or_before = [s for s in starts if s <= self._index]
            if not at_or_before:
                self._index = 0
            elif at_or_before[-1] < self._index:
                self._index = at_or_before[-1]
            else:
                self._index = at_or_before[-2] if len(at_or_before) >= 2 else 0

    # --- rendering -----------------------------------------------------------

    @property
    def dialog_width(self) -> int:
        """Natural outer width so a floating select hugs its widest row, not the terminal.

        The widest of the prompt, title, footer, and every row (with room for the pointer),
        so a short menu is a tidy popup rather than a full-width banner. The compositor still
        caps this to the space available, and this is only read for a *floating* select — the
        full-screen base menu is laid out by ``compose_base`` and ignores it.
        """
        widths = [cell_len(self.title), cell_len(self.footer_hint), cell_len(self._prompt)]
        for item in self._items:
            label = item.title if isinstance(item, Separator) else _plain(item.label)
            widths.append(cell_len(label) + 2)  # + the "❯ " / "  " pointer column
        return max(widths, default=20) + 8

    def render_body(self, width: int) -> list[str]:
        """Render the optional prompt then each row as one ANSI line, the choice marked."""
        rows = self._rows()
        choices = self._choices(rows)
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0
        selected = choices[self._index] if choices else None

        lines: list[str] = []
        # A prompt (when set) sits above the list, offsetting every row below it; the cursor
        # line and sticky-header indices below are shifted by exactly this many lines.
        prefix = 0
        if self._prompt:
            plines = render_lines(Text(self._prompt), width)
            lines.extend(plines)
            lines.append("")
            prefix = len(plines) + 1
        # Record each section heading as a sticky-header candidate, so one that scrolls off is
        # re-pinned to the top row by the base Screen.sticky_header. Separators survive
        # filtering (see _rows), so the pinning keeps working while the list narrows.
        self._sticky_headers = []
        if self._filter:
            lines.append(render_to_ansi(Text(f"/{self._filter}", style="warn"), width))
        for item in rows:
            if isinstance(item, Separator):
                sep = render_to_ansi(Text(item.title, style="muted"), width)
                self._sticky_headers.append((len(lines), sep))
                lines.append(sep)
                continue
            is_sel = item is selected
            pointer = "❯ " if is_sel else "  "
            style = "brand" if is_sel else ""
            label = item.label
            # A Text label carries its own spans (e.g. a red badge); keep them and lay the
            # row's base style underneath, so the highlight tints the row while the badge
            # keeps its colour. A plain string is styled uniformly as before.
            text = Text(pointer, style=style)
            text.append_text(label if isinstance(label, Text) else Text(label))
            text.style = style
            text.no_wrap = True
            text.overflow = "ellipsis"
            text.truncate(width)
            lines.append(render_to_ansi(text, width))
        if not choices:
            lines.append(render_to_ansi(Text("(no matches)", style="muted"), width))
        # Remember where the highlighted row landed so the session can keep it in view,
        # shifted past any prompt lines drawn above the list.
        base_cursor = _cursor_line(rows, selected, bool(self._filter))
        self._cursor = None if base_cursor is None else base_cursor + prefix
        return lines

    def cursor_line(self) -> Optional[int]:
        """Return the body line index of the highlighted row."""
        return getattr(self, "_cursor", None)

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, edit the filter, or commit/cancel the selection."""
        choices = self._choices()
        if action == "up":
            if choices:
                self._index = (self._index - 1) % len(choices) if self._wrap else max(
                    0, self._index - 1
                )
        elif action == "down":
            if choices:
                self._index = (self._index + 1) % len(choices) if self._wrap else min(
                    len(choices) - 1, self._index + 1
                )
        elif action == "pageup":
            self._index = max(0, self._index - self._page_step)
        elif action == "pagedown":
            self._index = min(len(choices) - 1, self._index + self._page_step) if choices else 0
        elif action in ("home", "ctrl_home"):
            self._index = 0
        elif action in ("end", "ctrl_end"):
            self._index = max(0, len(choices) - 1)
        elif action == "ctrl_pagedown":
            self._jump_section(1)
        elif action == "ctrl_pageup":
            self._jump_section(-1)
        elif action == "enter":
            if choices:
                self.resolve(choices[self._index].value)
        elif action == "escape":
            super().handle("escape")
        elif action == "backspace" and self._filterable:
            self._filter = self._filter[:-1]
            self._index = 0
        elif action == "text" and self._filterable and data.isprintable():
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
