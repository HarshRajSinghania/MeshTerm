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
        deletable: Whether pressing Delete on this row asks to remove it. When set, Delete
            resolves the list with a :class:`DeleteRequest` wrapping this row's value instead
            of choosing it, so the caller can run a remove flow and re-open the list. Off by
            default, so an ordinary list ignores Delete.
    """

    title: Union[str, Text, Callable[[], Union[str, Text]]]
    value: Any
    deletable: bool = False

    @property
    def label(self) -> Union[str, Text]:
        """The row's current text, resolving a callable title on each read."""
        return self.title() if callable(self.title) else self.title


@dataclass
class DeleteRequest:
    """A request, raised from a select list, to remove the highlighted row.

    Pressing Delete on a :class:`Choice` marked ``deletable`` resolves the select with this
    wrapper rather than the row's value itself, so the caller can tell "the user wants to
    remove this" apart from "the user chose this" — typically running a confirm-then-forget
    flow and re-opening the list.

    Attributes:
        value: The :attr:`Choice.value` of the row the user asked to remove.
    """

    value: Any


def _plain(label: Union[str, Text]) -> str:
    """The plain-text form of a row label, for filtering (a :class:`Text` keeps its ``plain``)."""
    return label.plain if isinstance(label, Text) else label


def _splice_hint(base: str, segment: str) -> str:
    """Insert ``segment`` into a footer hint just before its trailing ``Esc`` clause.

    Keeps the hint grammar's "Esc last" rule when a per-row hint (e.g. ``Del remove`` on a
    deletable row) is added while the cursor sits on it: the segment lands as its own ``·``
    atom right before the final ``· Esc …``, rather than after it. A hint with no ``Esc``
    clause simply gains the segment at the end.
    """
    marker = " · Esc"
    idx = base.rfind(marker)
    if idx == -1:
        return f"{base} · {segment}"
    return f"{base[:idx]} · {segment}{base[idx:]}"


@dataclass
class Separator:
    """A non-selectable row between choices.

    Attributes:
        title: The row's text — a plain string drawn uniformly in :attr:`style`, or a Rich
            :class:`~rich.text.Text` carrying its own spans (for a column header that lights
            just its active sort column, say), rendered as-authored with ``style`` ignored.
        style: Theme style a *string* title is drawn in. Section headings pass ``"accent"``
            so they read as highlighted landmarks; the default ``"muted"`` fits the
            structural rows (blank spacers, column-header lines, inline notes). A ``Text``
            title styles itself, so this is unused for one.
    """

    title: Union[str, Text]
    style: str = "muted"


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
        footer_hint: Optional[str] = None,
        delete_hint: str = "",
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
            footer_hint: Footer key hint; defaults to one that mentions type-to-filter only
                when ``filterable`` (a fixed list shouldn't advertise a filter it ignores).
            delete_hint: A key-hint atom (e.g. ``"Del remove"``) surfaced in the footer only
                while the highlighted row is :attr:`Choice.deletable` — so the removal key
                advertises itself exactly when it would act, and stays hidden on rows it can't
                touch. Empty (the default) leaves the footer fixed.
            filterable: Whether typing narrows the list. Off for short, fixed lists (e.g.
                the startup device picker) where type-to-filter would only get in the way.
            wrap: Whether the highlight wraps around the ends (Down from the last row jumps
                to the first, and vice versa). Off for grouped lists where wrapping across the
                section headings reads as a jarring jump rather than continuing to scroll.
        """
        super().__init__()
        self.title = title
        if footer_hint is None:
            footer_hint = (
                "↑↓ move · type to filter · Enter select · Esc back"
                if filterable
                else "↑↓ move · Enter select · Esc back"
            )
        self._footer_base = footer_hint
        self._delete_hint = delete_hint
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

    def _current_choice(self) -> Optional[Choice]:
        """The choice the highlight currently sits on, or ``None`` when the list is empty."""
        choices = self._choices()
        if not choices:
            return None
        return choices[max(0, min(self._index, len(choices) - 1))]

    @property
    def footer_hint(self) -> str:
        """The footer key hint, gaining the :attr:`_delete_hint` atom on a deletable row.

        Fixed to the base hint unless a ``delete_hint`` was supplied *and* the highlighted
        row opted into removal — then the atom is spliced in just before the trailing ``Esc``
        clause (see :func:`_splice_hint`), so the removal key shows up on the bottom border
        precisely while the cursor is on a row it can act on.
        """
        base = self._footer_base
        current = self._current_choice()
        if self._delete_hint and current is not None and current.deletable:
            return _splice_hint(base, self._delete_hint)
        return base

    @property
    def sizing_footer_hint(self) -> str:
        """The fullest the footer can get, for stable box sizing (see :attr:`footer_hint`).

        The delete-hint atom is always folded in here, so a compositor that sizes a box to
        the footer width (the startup splash's :func:`~meshterm.ui.tui.frame.compose_startup`)
        reserves room for it up front — the box then never widens the moment the highlight
        lands on a deletable row.
        """
        if self._delete_hint:
            return _splice_hint(self._footer_base, self._delete_hint)
        return self._footer_base

    # --- rendering -----------------------------------------------------------

    @property
    def dialog_width(self) -> int:
        """Natural outer width so a floating select hugs its widest row, not the terminal.

        The widest of the prompt, title, footer, and every row (with room for the pointer),
        so a short menu is a tidy popup rather than a full-width banner. The compositor still
        caps this to the space available, and this is only read for a *floating* select — the
        full-screen base menu is laid out by ``compose_base`` and ignores it. The footer is
        measured at its fullest — with the delete-hint atom folded in — so a deletable row
        surfacing that hint never widens the box mid-navigation.
        """
        footer = _splice_hint(self._footer_base, self._delete_hint) if self._delete_hint \
            else self._footer_base
        widths = [cell_len(self.title), cell_len(footer), cell_len(self._prompt)]
        for item in self._items:
            label = item.title if isinstance(item, Separator) else item.label
            widths.append(cell_len(_plain(label)) + 2)  # + the "❯ " / "  " pointer column
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
                # A Text title carries its own spans (a two-colour column header); a plain
                # string is drawn uniformly in the separator's style.
                title = item.title
                heading = title if isinstance(title, Text) else Text(title, style=item.style)
                sep = render_to_ansi(heading, width)
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
            lines.append(render_to_ansi(Text("no matches", style="muted"), width))
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
        elif action == "delete":
            # Delete asks to remove the highlighted row, but only where the row opted in
            # (e.g. a remembered network device in the picker); elsewhere it's inert.
            if choices and choices[self._index].deletable:
                self.resolve(DeleteRequest(choices[self._index].value))
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

    Move the cursor with ↑/↓; press Enter on a row to *grab* it, then ↑/↓ carry it up and
    down the list; press Enter again to *drop* it. Below the list sit the action rows,
    following the config editor's pattern: once the order has actually changed, an
    ok-tinted *Apply* joins an err-tinted *Back — discard*; while it is untouched there is
    only a plain *Back*. Enter on Apply commits, resolving with the final order as a list
    of the original row indices (so ``[2, 0, 1]`` means "the row that started third is now
    first"); Enter on Back — like Esc anywhere — resolves :data:`CANCEL` so the caller
    keeps the original order.
    """

    #: Action-row sentinels (kept distinct from list positions, which are ints).
    _APPLY = "apply"
    _BACK = "back"

    #: Footer hints for both grab states — dialog_width sizes to the longer one, so the
    #: box never resizes when a grab starts.
    _HINT_IDLE = "↑↓ move · Enter grab / select · Esc cancel"
    _HINT_GRABBED = "↑↓ move row · Enter drop · Esc cancel"

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
        return self._HINT_GRABBED if self._grabbed else self._HINT_IDLE

    @property
    def dialog_width(self) -> int:
        """Natural outer width so a floating reorder hugs its widest row (see SelectScreen).

        Sized for the fullest the box can get — both footer hints and the dirty-state
        action rows — so it never widens mid-interaction when a grab starts or Apply
        appears. The compositor still caps this to the terminal.
        """
        widths = [cell_len(self.title), cell_len(self._HINT_IDLE), cell_len(self._HINT_GRABBED)]
        rows = self._labels + [label.plain for _key, label in self._dirty_actions()]
        widths += [cell_len(row) + 2 for row in rows]  # + the "❯ " / "  " pointer column
        return max(widths, default=20) + 8

    def _dirty(self) -> bool:
        """Whether the rows have actually left their original order."""
        return self._order != list(range(len(self._order)))

    @staticmethod
    def _dirty_actions() -> list[tuple[str, Text]]:
        """The full exit group shown once the order has changed (also sizes dialog_width)."""
        return [
            (ReorderScreen._APPLY, Text.assemble(("✓ ", "ok"), "Apply new order")),
            (ReorderScreen._BACK, Text.assemble(("✗ ", "err"), "Back — discard changes")),
        ]

    def _actions(self) -> list[tuple[str, Text]]:
        """The action rows below the list, matching the config editor's exit group:
        Apply joins Back only once there is a change to apply, and Back then spells
        out the consequence of leaving.
        """
        if self._dirty():
            return self._dirty_actions()
        return [(self._BACK, Text("Back"))]

    def render_body(self, width: int) -> list[str]:
        """Render the rows, a blank spacer, then the action group, marking the cursor."""
        n = len(self._order)
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
        lines.append("")
        for i, (_key, label) in enumerate(self._actions()):
            if n + i == self._index:
                # The cursor row goes full-brand like the list rows above it,
                # trading the ✓/✗ tint for the highlight.
                text = Text("❯ " + label.plain, style="brand", no_wrap=True)
            else:
                text = Text("  ", no_wrap=True)
                text.append_text(label)
            text.truncate(width, overflow="ellipsis")
            lines.append(render_to_ansi(text, width))
        # The spacer line offsets every action row by one on screen.
        self._cursor = self._index if self._index < n else self._index + 1
        return lines

    def cursor_line(self) -> Optional[int]:
        """Return the body line index of the cursor row, so the session keeps it in view."""
        return getattr(self, "_cursor", None)

    def handle(self, action: str, data: str = "") -> None:
        """Move the cursor, carry a grabbed row, grab/drop, run an action, or cancel on Esc."""
        n = len(self._order)
        total = n + len(self._actions())
        if action == "up":
            if self._grabbed and self._index > 0:
                self._order[self._index - 1], self._order[self._index] = (
                    self._order[self._index], self._order[self._index - 1])
                self._index -= 1
            elif not self._grabbed and total:
                self._index = (self._index - 1) % total
        elif action == "down":
            if self._grabbed and self._index < n - 1:
                self._order[self._index + 1], self._order[self._index] = (
                    self._order[self._index], self._order[self._index + 1])
                self._index += 1
            elif not self._grabbed and total:
                self._index = (self._index + 1) % total
        elif action == "enter":
            if self._index < n:
                self._grabbed = not self._grabbed  # Enter grabs a row, Enter again drops it
            elif self._actions()[self._index - n][0] == self._APPLY:
                self.resolve(list(self._order))
            else:
                super().handle("escape")  # Back resolves CANCEL, same as Esc
        elif action == "escape":
            super().handle("escape")


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
