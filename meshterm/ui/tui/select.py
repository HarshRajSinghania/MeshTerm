"""The selectable-list screen: the reusable replacement for ``questionary.select``.

A :class:`SelectScreen` shows grouped, arrow-navigable choices with type-to-filter, bounded
to the viewport and scrolled to keep the highlighted row visible. Choice values are
arbitrary objects, so the same screen drives the main menu (tool names), the device picker
(device objects), and the config editor (setting keys).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from rich.cells import cell_len
from rich.text import Text

from .render import crop_cells, query_line, render_lines, render_to_ansi
from .screen import Screen


def _wants_width(fn: Callable) -> bool:
    """Whether a callable :attr:`Choice.title` takes the render width.

    A title callable with at least one *required* positional parameter is the width-aware
    form; one with none (including defaults-only signatures) stays the zero-argument
    live-repaint form. Decided from the signature once at construction — never by trying
    the call, so a ``TypeError`` raised *inside* a title can't be mistaken for arity.
    """
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):  # a builtin without an introspectable signature
        return False
    return any(
        p.default is p.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        for p in parameters
    )


@dataclass
class Choice:
    """One selectable row.

    Attributes:
        title: Text shown for the row — a plain string or a Rich :class:`~rich.text.Text`
            (for a coloured segment such as an unread badge). Either may instead be a
            zero-argument callable resolved fresh on every repaint, so a row can track state
            that changes while the list is open — or a callable taking the **render width**
            (one required positional argument), the :class:`Separator` power extended to
            rows: a route-bearing row can then middle-elide itself to the terminal
            (``PathLine.ellipsized``) instead of being amputated at the right edge.
        value: Value returned when the row is chosen.
        deletable: Whether pressing Delete on this row asks to remove it. When set, Delete
            resolves the list with a :class:`DeleteRequest` wrapping this row's value instead
            of choosing it, so the caller can run a remove flow and re-open the list. Off by
            default, so an ordinary list ignores Delete.
        detail: An optional second line, drawn hanging under ``title`` at the pointer's
            indent — context that doesn't belong in the selectable line itself (a route's
            weakest SNR/sample count/provenance tag, say). Never scrolls or wraps; it
            ellipsizes on its own if too wide. ``None`` (the default) draws nothing, so an
            ordinary row stays exactly one line. May be a zero-argument callable like
            ``title``.
    """

    title: Union[str, Text, Callable[[], Union[str, Text]], Callable[[int], Union[str, Text]]]
    value: Any
    deletable: bool = False
    detail: Union[str, Text, Callable[[], Union[str, Text]], None] = None

    def __post_init__(self) -> None:
        # The callable form's arity, read once — see _wants_width.
        self._title_wants_width = callable(self.title) and _wants_width(self.title)

    def text(self, width: int) -> Union[str, Text]:
        """The row's content at ``width`` cells, resolving either callable form."""
        if not callable(self.title):
            return self.title
        return self.title(width) if self._title_wants_width else self.title()

    @property
    def label(self) -> Union[str, Text]:
        """The row's *natural* (unbounded) text — what filtering and measuring read."""
        return self.text(_UNBOUNDED)

    @property
    def detail_label(self) -> Optional[Union[str, Text]]:
        """The row's current detail line, resolving a callable on each read."""
        return self.detail() if callable(self.detail) else self.detail


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


def _insert_atom(base: str, atom: str, index: int = 1) -> str:
    """Insert ``atom`` as the ``index``-th ` · `-separated atom of a footer hint.

    Surfaces a conditional *navigation* atom (the ←→ per-row scroll) right after the
    leading move atom, keeping the hint's navigation-then-actions-then-``Esc`` shape — where
    :func:`_splice_hint` instead places an *action* atom just before the trailing ``Esc``.
    """
    parts = base.split(" · ")
    parts.insert(min(index, len(parts)), atom)
    return " · ".join(parts)


@dataclass
class Separator:
    """A non-selectable row between choices.

    Attributes:
        title: The row's text — a plain string drawn uniformly in :attr:`style`, or a Rich
            :class:`~rich.text.Text` carrying its own spans (for a column header that lights
            just its active sort column, say), rendered as-authored with ``style`` ignored.
            It may instead be a callable taking the **render width** and returning either
            — unlike :attr:`Choice.title`'s zero-argument callable — so a column header can
            size its labels to the terminal it is being drawn on (see
            :func:`~meshterm.ui.menus.column_header`).
        style: Theme style a *string* title is drawn in. Section headings pass ``"accent"``
            so they read as highlighted landmarks; the default ``"muted"`` fits the
            structural rows (blank spacers, column-header lines, inline notes). A ``Text``
            title styles itself, so this is unused for one.
        pinned: Whether this row stays on screen for the *whole* list once scrolled past,
            above any section heading pinned under it — for a column header, whose lane
            names mean the same in every section (see
            :meth:`~meshterm.ui.tui.screen.Screen.sticky_rows`). Off by default: an
            ordinary separator only pins while its own section is on screen. At most one
            row per list should set it; the last one recorded wins.
        heading: Whether this row is a *section landmark*: it re-pins to the top row while
            its own section is scrolled through, and it delimits the Ctrl+PageUp/PageDown
            section jumps. Off by default, because most separators label nothing below them
            — a blank spacer, an empty-state note, a discipline's wrapped description — and
            pinning one of those overhead would say nothing while costing a content row (and
            would shadow the heading it sits under). Set through
            :func:`~meshterm.ui.menus.section_heading`, or by hand on a column header that
            *is* its block's only landmark.
    """

    title: Union[str, Text, Callable[[int], Union[str, Text]]]
    style: str = "muted"
    pinned: bool = False
    heading: bool = False

    def text(self, width: int) -> Union[str, Text]:
        """The row's content at ``width`` cells, resolving a width-aware title."""
        return self.title(width) if callable(self.title) else self.title


Item = "Choice | Separator"

#: The width handed to a width-aware :class:`Separator` or :class:`Choice` when a
#: *natural* width is being measured rather than a real terminal one
#: (:attr:`SelectScreen.dialog_width`, :attr:`Choice.label`) — wide enough that a
#: self-fitting column header or self-eliding row returns its fullest form.
_UNBOUNDED = 10_000

#: The navigation actions that move the highlight to another row, so an ``hscroll`` list
#: drops the current row's horizontal shift (each row scrolls on its own — see
#: :meth:`SelectScreen.handle`). The filter edits reset it in their own branches.
_HSHIFT_RESET_ACTIONS = frozenset({
    "up", "down", "pageup", "pagedown", "home", "ctrl_home", "end", "ctrl_end",
    "ctrl_pageup", "ctrl_pagedown",
})


class SelectScreen(Screen):
    """A grouped, filterable, single-choice list.

    Resolves with the chosen :class:`Choice` value, or :data:`~meshterm.ui.tui.screen.CANCEL`
    if the user presses Esc.
    """

    #: Cells one ←/→ press shifts an h-scrolling list by (see the ``hscroll`` flag).
    _HSCROLL_STEP = 8

    @property
    def fkey_lane(self):
        """The shared pager, the section jumps on F1/F2, and row deletion on F3.

        Ctrl+PageUp/PageDown step the highlight heading by heading — and on the PicoCalc
        they are the one binding in the app that is *physically unreachable*: paging has
        no key there to hold Ctrl over. So a grouped list promotes them onto the lane,
        keeping the shared handedness — a pair rises toward its outer key, so on this
        left-edge pair *up* takes F1 (see :data:`~meshterm.ui.tui.fkeys.DEFAULT_LANE`).

        Their two gates say different things, per the lane's empty-versus-dim rule. A list
        that has no headings *at all* leaves both slots blank: sections aren't a thing
        here. A grouped list whose live filter has narrowed the rows down to a single
        surviving section keeps its labels and dims them — the sections are still a thing,
        they just have nowhere to jump right now.

        ``Delete`` is the same claim as the footer's per-row delete atom, on a platform
        that draws no footer: only a list that hands out :attr:`Choice.deletable` rows at
        all shows the slot, and it lights exactly on the rows the key would act on. The
        chip says ``Delete`` on every such list rather than echoing each one's own hint
        phrasing, because six cells hold the key's verb and not the object it removes —
        which the highlighted row is already naming.
        """
        from .fkeys import FPair, default_lane

        # One pass over the displayed rows for the whole lane: this runs every paint on
        # the platform that draws it, and `_rows()` re-filters the item list each call.
        rows = self._rows()
        lane = list(default_lane(nav=len(self._choices(rows)) > 1))
        if len(self._all_section_starts()) >= 2:
            live = len(self._section_starts(rows)) > 1
            lane[0] = FPair("Sect ↑", "ctrl_pageup", enabled=live)
            lane[1] = FPair("Sect ↓", "ctrl_pagedown", enabled=live)
        if self._delete_hint:
            choices = self._choices(rows)
            current = choices[max(0, min(self._index, len(choices) - 1))] if choices else None
            lane[2] = FPair("Delete", "delete",
                            enabled=current is not None and current.deletable)
        return lane

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
        hscroll: bool = False,
        hscroll_hint: str = "←→ scroll",
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
            hscroll: Whether ←/→ horizontally scroll the *highlighted* row so an over-long
                line can be read to its end (the Watchtower's alert log). Off by default —
                rows simply ellipsize at the right edge and ←/→ stay inert. Only a row that
                actually overflows the width scrolls; a short row (and every separator or
                column header) stays put, and the shift resets to the start whenever the
                highlight moves to another row or the filter is edited — each row scrolls
                on its own, independently of the rest of the screen.
            hscroll_hint: The footer atom surfaced (as the second ` · ` atom, right after
                the move atom) while ``hscroll`` is on and the highlighted row overflows —
                so ←→ advertises itself exactly when it would do something. Ignored when
                ``hscroll`` is off.
        """
        super().__init__()
        self.title = title
        self._hscroll = hscroll
        self._hscroll_hint = hscroll_hint
        self._hshift = 0
        self._last_width = 0  # the last render width, for the footer's overflow probe
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
        needle = self._filter.strip().lower()
        if not needle:
            return self._items
        return [
            it
            for it in self._items
            if isinstance(it, Separator) or needle in _plain(it.label).lower()
        ]

    def _choices(self, rows: Optional[list] = None) -> list:
        """Return just the selectable choices among ``rows`` (or the current rows)."""
        rows = self._rows() if rows is None else rows
        return [it for it in rows if isinstance(it, Choice)]

    def _section_starts(self, rows: Optional[list] = None) -> list[int]:
        """Choice indices that begin a section — the first choice after each run of separators.

        Drives Ctrl+PageUp/PageDown section jumps. Computed over the *displayed* rows by
        default, so the jumps keep working while a filter narrows the list (the headings
        stay — see :meth:`_rows` — and empty sections simply yield no stop).

        Args:
            rows: The rows to read the sections off, or ``None`` for the displayed ones.
                :meth:`_all_section_starts` passes the unfiltered items instead, to ask
                the structural question rather than the live one.
        """
        starts: list[int] = []
        idx = 0
        fresh = True  # the next choice opens a section (top of the list, or just past a heading)
        for item in self._rows() if rows is None else rows:
            if isinstance(item, Separator):
                fresh = True
            elif isinstance(item, Choice):
                if fresh:
                    starts.append(idx)
                    fresh = False
                idx += 1
        return starts

    def _all_section_starts(self) -> list[int]:
        """The section starts of the *unfiltered* list — whether this list has sections at all.

        The F-key lane needs both questions answered separately (see :attr:`fkey_lane`):
        a flat list never offers a section jump, while a grouped one whose filter has
        collapsed it to one section offers it again the moment the filter is edited.
        """
        return self._section_starts(self._items)

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
        """The footer key hint, gaining dynamic atoms only where their keys would act.

        Two conditional atoms fold in exactly where they apply, so the bottom border never
        advertises a key that would do nothing:

        * the :attr:`_delete_hint` atom, spliced just before the trailing ``Esc`` clause
          (see :func:`_splice_hint`) while the highlighted row is :attr:`Choice.deletable`;
        * the :attr:`_hscroll_hint` atom (``hscroll`` lists only), inserted right after the
          move atom (see :func:`_insert_atom`) while the highlighted row overflows the width
          — a short row scrolls nowhere, so ←→ stays hidden on it.
        """
        base = self._footer_base
        current = self._current_choice()
        if self._delete_hint and current is not None and current.deletable:
            base = _splice_hint(base, self._delete_hint)
        if self._hscroll and self._hscroll_hint and self._selected_overflows():
            base = _insert_atom(base, self._hscroll_hint)
        return base

    def _selected_overflows(self) -> bool:
        """Whether the highlighted row's label is too wide for the last render width.

        The gate for both the ←→ scroll and its footer atom: a row that fits has nothing to
        scroll. Measured against the row's content area (the width less the 2-cell pointer),
        using the width the last :meth:`render_body` saw (``0`` before the first paint, so
        nothing reads as overflowing until a real width is known).
        """
        if not self._hscroll or self._last_width <= 0:
            return False
        current = self._current_choice()
        if current is None:
            return False
        return cell_len(_plain(current.label)) > max(1, self._last_width - 2)

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
        surfacing that hint never widens the box mid-navigation. A width-aware separator is
        measured at its fullest too (see :data:`_UNBOUNDED`): the box asks for room for the
        whole header, and the header only abbreviates once the *terminal* caps the box.
        """
        footer = _splice_hint(self._footer_base, self._delete_hint) if self._delete_hint \
            else self._footer_base
        widths = [cell_len(self.title), cell_len(footer), cell_len(self._prompt)]
        for item in self._items:
            label = item.text(_UNBOUNDED)  # both row kinds measure at their fullest form
            widths.append(cell_len(_plain(label)) + 2)  # + the "❯ " / "  " pointer column
            if isinstance(item, Choice) and item.detail_label is not None:
                widths.append(cell_len(_plain(item.detail_label)) + 2)  # same hanging indent
        return max(widths, default=20) + 8

    def render_body(self, width: int) -> list[str]:
        """Render the optional prompt then each row as one ANSI line, the choice marked."""
        rows = self._rows()
        choices = self._choices(rows)
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0
        selected = choices[self._index] if choices else None

        # Horizontal scroll rides only the *highlighted* row, and only as far as its own
        # tail: → stops once that row's end is in view, and a short (or unselected) row
        # can't shift at all. Measured fresh each paint — a callable title may have changed
        # width — against the row's content area (width less the 2-cell pointer).
        self._last_width = width
        if self._hscroll and self._hshift:
            avail = max(1, width - 2)
            sel_len = cell_len(_plain(selected.label)) if selected is not None else 0
            self._hshift = max(0, min(self._hshift, sel_len - avail))

        lines: list[str] = []
        # A prompt (when set) sits above the list, offsetting every row below it; the cursor
        # line and sticky-header indices below are shifted by exactly this many lines.
        prefix = 0
        if self._prompt:
            plines = render_lines(Text(self._prompt), width)
            lines.extend(plines)
            lines.append("")
            prefix = len(plines) + 1
        # Record each section heading (a Separator that says it is one) as a sticky block, so
        # one that scrolls off is re-pinned to the top rows by the base Screen.sticky_block —
        # and a pinned separator (a column header) as the whole-list header pinned above it.
        # A heading's block runs on through the separators that *immediately* follow it, before
        # the section's first row: prose written there is the section's preamble (the Trophy
        # case's "what this discipline scores"), so it belongs overhead with the heading rather
        # than scrolling away from the rows it explains. Everything else — blank spacers,
        # empty-state notes under a section that has rows, a stray line between choices — is
        # the landmark of nothing and stays out of the running, so the rows pinned overhead
        # are always the section that governs. Separators survive filtering (see _rows), so
        # the pinning keeps working while the list narrows.
        self._sticky_headers = []
        self._pinned_header = None
        block: Optional[list[str]] = None  # the heading block still taking rows, if any
        if self._filter:
            lines.append(query_line(self._filter, width))
        # A row's detail line (see Choice.detail) can make it two lines tall, so the cursor
        # is tracked inline as rows are drawn rather than derived from the row index.
        cursor_at: Optional[int] = None
        for item in rows:
            if isinstance(item, Separator):
                # A Text title carries its own spans (a two-colour column header); a plain
                # string is drawn uniformly in the separator's style. Separators never
                # h-scroll — the shift rides the highlighted choice row alone.
                title = item.text(width)
                content = title if isinstance(title, Text) else Text(title, style=item.style)
                if item.pinned:
                    # A pinned header is drawn *outside* the body slice and is exactly one
                    # row tall, so it crops like a row rather than wrapping — a column
                    # header too wide for the terminal ellipsizes instead of stealing a
                    # second reserved row from the content.
                    drawn = [render_to_ansi(content, width, no_wrap=True)]
                    # A whole-list header stands outside the section run too: it pins above
                    # the section headings rather than taking a turn among them, and so is
                    # no section boundary for the Ctrl+PageUp/PageDown jumps either.
                    self._pinned_header = (len(lines), drawn[0])
                else:
                    # A prose separator (a note above a startup list) may wrap; each row it
                    # takes is its own body line, so the viewport still counts them all — and
                    # every one of them joins the block, wrapped continuations included.
                    drawn = render_lines(content, width)
                    if item.heading:
                        block = list(drawn)
                        self._sticky_headers.append((len(lines), block))
                    elif block is not None:
                        block.extend(drawn)  # the heading's preamble, pinned with it
                lines.extend(drawn)
                continue
            block = None  # a row closes the heading's block; prose past here is its own
            is_sel = item is selected
            if is_sel:
                cursor_at = len(lines)
            pointer = "❯ " if is_sel else "  "
            style = "brand" if is_sel else ""
            # A width-aware title fits itself to the row's content area (the width less
            # the 2-cell pointer). The exception is the highlighted row of an ``hscroll``
            # list, which keeps its natural form: ←→ slide the full line, and a row that
            # pre-elided itself would have nothing left to slide over.
            if self._hscroll and is_sel:
                label = item.label
            else:
                label = item.text(max(1, width - 2))
            # A Text label carries its own spans (e.g. a red badge); keep them and lay the
            # row's base style underneath, so the highlight tints the row while the badge
            # keeps its colour. A plain string is styled uniformly as before.
            text = Text(pointer, style=style)
            label_text = label if isinstance(label, Text) else Text(label)
            if self._hscroll and self._hshift and is_sel:
                # Only the highlighted row slides, and only its label — the 2-cell pointer
                # stays pinned. Every other row (and separator) renders unshifted.
                label_text = crop_cells(label_text, self._hshift, max(1, width - 2))
            text.append_text(label_text)
            text.style = style
            text.no_wrap = True
            text.overflow = "ellipsis"
            text.truncate(width)
            lines.append(render_to_ansi(text, width))
            detail = item.detail_label if isinstance(item, Choice) else None
            if detail is not None and _plain(detail):
                # Hangs under the row at the pointer's own indent — never scrolls or
                # wraps, just ellipsizes on its own if it's too wide to fit.
                detail_text = detail if isinstance(detail, Text) else Text(detail)
                line = Text("  ")
                line.append_text(detail_text)
                line.no_wrap = True
                line.overflow = "ellipsis"
                line.truncate(width)
                lines.append(render_to_ansi(line, width))
        if not choices:
            lines.append(render_to_ansi(Text("no matches", style="muted"), width))
        # Remember where the highlighted row landed so the session can keep it in view,
        # shifted past any prompt lines drawn above the list.
        self._cursor = None if cursor_at is None else cursor_at + prefix
        return lines

    def cursor_line(self) -> Optional[int]:
        """Return the body line index of the highlighted row."""
        return getattr(self, "_cursor", None)

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, edit the filter, or commit/cancel the selection."""
        choices = self._choices()
        if self._hscroll and action in _HSHIFT_RESET_ACTIONS:
            self._hshift = 0  # moving off a row abandons its scroll — each row scrolls alone
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
            # The jump goes to the very top of the *page*, not just the first choice: the
            # rows above it (a heading, its preamble, a lead-in note) scroll back into
            # view too, rather than staying folded behind their pinned stand-ins.
            self._index = 0
            self.scroll_to_top()
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
        elif action == "left" and self._hscroll:
            self._hshift = max(0, self._hshift - self._HSCROLL_STEP)
        elif action == "right" and self._hscroll:
            self._hshift += self._HSCROLL_STEP  # clamped to the highlighted row's tail at render
        elif action == "escape":
            super().handle("escape")
        elif action == "backspace" and self._filterable:
            self._filter = self._filter[:-1]
            self._index = 0
            self._hshift = 0
        elif action == "text" and self._filterable and data.isprintable():
            # A leading space is ignored (the filter never begins with whitespace); a
            # trailing one is dropped when matching (see _rows), so spaces count only
            # mid-query — inside a multi-word name like "Homestead R&D".
            if not data.isspace() or self._filter:
                self._filter += data
                self._index = 0
                self._hshift = 0


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

    @property
    def fkey_lane(self):
        """No lane: the whole screen is ↑↓ and Enter, three keys already on the keyboard."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE

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
