"""Shared select-menu chrome: the pieces every list screen builds the same way.

MeshTerm's list screens (the config editors, the channel manager, Watchtower, Courier,
the pickers) all end in the same furniture: label-and-description rows padded into
aligned lanes, a blank line, then the exit group — a plain ``Back`` row, or the
``✓ Apply / ✗ Back — discard`` pair once changes are staged. Before this module each
screen hand-rolled its own copy, and they drifted (``← Back``, ``← Close``, missing
blank lines, ``✖`` for ``✗``). These helpers are now the one way to build that
furniture, so the app's exit language and row alignment stay uniform by construction:

* :func:`back_rows` — the standard exit group: one blank separator, then ``Back``.
* :func:`exit_rows` — the staged-changes variant: ``✓ Apply n staged changes`` above an
  err-tinted ``✗ Back — discard staged changes`` (or the plain group when clean).
* :func:`menu_rows` — label + muted description :class:`Choice` rows in two aligned
  lanes, padded in display cells so double-width emoji can't skew the description
  column (the Device actions presentation).
* :func:`lane_row` — one SETTING / VALUE / DESCRIPTION row for the editor-style lists.
* :func:`column_header` / :class:`Lane` — the header line over a lane-aligned list, which
  abbreviates its labels to fit a narrow terminal instead of wrapping or losing one, and
  :func:`lane_header`, the ready-made header for :func:`lane_row`'s lanes.
* :func:`changes_phrase` — ``"1 staged change"`` / ``"3 staged changes"``.
* :func:`confirm_discard` — the shared are-you-sure dialog for leaving staged changes.

House rules the helpers encode (see the UX standards in ``CLAUDE.md``): the exit row is
always the bare word ``Back`` — no arrow, no icon — with exactly one blank separator
line above it; ``✓``/``✗`` (U+2713/U+2717) are THE status marks, styled ``ok``/``err``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence, Union

from rich.cells import cell_len
from rich.text import Text

from .tui import Choice, Separator

if TYPE_CHECKING:
    from ..context import AppContext


def back_rows(value: Any = None) -> list:
    """The standard exit group closing a select list: a blank line, then ``Back``.

    Args:
        value: The value the Back row resolves with (``None`` reads as cancel for
            most callers; the config editors pass their own sentinel).

    Returns:
        ``[Separator(" "), Choice("Back", value)]`` — append to the end of the items.
    """
    return [Separator(" "), Choice(title="Back", value=value)]


def exit_rows(staged: int, *, apply_value: Any, back_value: Any) -> list:
    """The editor exit group: Apply joins Back once there is something staged.

    With nothing staged this is exactly :func:`back_rows`. With changes staged, an
    ok-marked ``✓ Apply …`` row sits above an err-marked ``✗ Back — discard staged
    changes`` so the consequence of leaving is spelled out (the config editor's
    long-standing presentation, now shared).

    Args:
        staged: How many changes are staged (0 = the plain Back group).
        apply_value: The value the Apply row resolves with.
        back_value: The value the Back row resolves with.

    Returns:
        The rows to append: a blank separator, then the one- or two-row exit group.
    """
    if not staged:
        return back_rows(back_value)
    return [
        Separator(" "),
        Choice(
            title=Text.assemble(("✓ ", "ok"), f"Apply {changes_phrase(staged)}"),
            value=apply_value,
        ),
        Choice(
            title=Text.assemble(("✗ ", "err"), "Back — discard staged changes"),
            value=back_value,
        ),
    ]


def menu_rows(rows: Iterable[tuple[Union[str, Text], str, Any]]) -> list:
    """Label + description :class:`Choice` rows in two aligned lanes.

    The menu-style presentation shared by Device actions, the channel detail, and the
    repeater actions: no header line (these are commands, not tabular data), the
    description column starting two cells past the widest label, padding computed in
    display cells so a double-width emoji can't skew it.

    Args:
        rows: ``(label, description, value)`` triples. A :class:`Text` label keeps its
            own styling (an err-tinted destructive row, a live unread badge).

    Returns:
        One :class:`Choice` per row, lanes aligned across them all.
    """
    prepared = [
        (label if isinstance(label, Text) else Text(label), help_text, value)
        for label, help_text, value in rows
    ]
    width = max((cell_len(label.plain) for label, _, _ in prepared), default=0)
    items: list = []
    for label, help_text, value in prepared:
        row = Text()
        row.append_text(label)
        row.append(" " * (width - cell_len(label.plain) + 2))
        row.append(help_text, style="muted")
        items.append(Choice(title=row, value=value))
    return items


def lane_row(label: str, value: Text, help_text: str, label_w: int, value_w: int) -> Text:
    """Lay one row out in the SETTING / VALUE / DESCRIPTION lanes (cell-padded).

    The editor-list presentation (device configuration, repeater admin): padding is
    computed in display cells so a wide glyph in a value can't skew the lanes, and the
    description stays muted under the select highlight (which only tints the row's
    base style).
    """
    row = Text(label)
    row.append(" " * (label_w - cell_len(label) + 2))
    row.append_text(value)
    row.append(" " * (value_w - cell_len(value.plain) + 2))
    row.append(help_text, style="muted")
    return row


@dataclass(frozen=True)
class Lane:
    """One lane of a column header: its label, any shorter forms, and its width.

    Attributes:
        label: The lane's label — a plain string, or its forms longest-first
            (``("DESCRIPTION", "DESC")``) for a lane that can give cells back on a narrow
            terminal. Only a lane the line actually overruns is ever shortened.
        width: Cells the label is padded to — the lane's own width *plus* the gap before
            the next lane, so a label wider than its column absorbs that gap instead of
            shifting every lane after it right (``UNREAD`` over a narrower badge lane).
            Zero (the default) for a trailing lane, which just runs to the edge.
    """

    label: Union[str, Sequence[str]]
    width: int = 0

    @property
    def forms(self) -> tuple[str, ...]:
        """The lane's labels, longest first (a bare string is its own only form)."""
        return (self.label,) if isinstance(self.label, str) else tuple(self.label)


def column_header(lanes: Sequence[Lane], width: int, *, indent: int = 2) -> str:
    """The column-header line over a lane-aligned list, fitted to ``width``.

    THE header builder for every list that pads its rows into columns (the two setting
    editors, the chat picker). Lanes are laid out left to right at their own widths, after
    ``indent`` cells clearing the pointer column, so each label lands exactly over the lane
    it names.

    A header is one row and stays one row. Where the line overruns ``width`` the lanes fall
    back to their shorter labels — from the right, since the fixed lanes are padded to their
    rows' content and only a trailing lane can actually give a cell back — and a line that
    still won't fit is ellipsized. It never wraps: a pinned header (see
    :attr:`~meshterm.ui.tui.select.Separator.pinned`) is drawn outside the body slice, where
    a second row would cost the content one.

    Args:
        lanes: The lanes in display order.
        width: Cells the line has to fit into — the screen's render width.
        indent: Leading pad in cells: the select screen's 2-cell pointer column, plus any
            glyph lane the rows draw before their first value.

    Returns:
        The header line, at most ``width`` cells wide.
    """
    picked = [0] * len(lanes)

    def line() -> str:
        out = " " * indent
        for lane, form in zip(lanes, picked):
            label = lane.forms[form]
            out += label + " " * max(0, lane.width - cell_len(label))
        return out

    at = len(lanes) - 1
    while at >= 0 and cell_len(line()) > width:
        if picked[at] + 1 < len(lanes[at].forms):
            picked[at] += 1  # this lane has something shorter to offer — take it
        else:
            at -= 1  # spent; ask the lane to its left
    text = line()
    return fit_cells(text, width) if cell_len(text) > width else text


def lane_header(label_w: int, value_w: int, width: int) -> str:
    """The SETTING / VALUE / DESCRIPTION header over :func:`lane_row`'s lanes.

    The row builder's header twin, shared by the two editor lists (device configuration
    and repeater admin) so they head identical lanes identically. ``DESCRIPTION`` shortens
    to ``DESC`` where the two value lanes leave it no room — long setting labels and a
    staged ``current → new`` value can push the full word past a 72-column terminal.
    """
    return column_header(
        [
            Lane("SETTING", label_w + 2),
            Lane("VALUE", value_w + 2),
            Lane(("DESCRIPTION", "DESC")),
        ],
        width,
    )


def changes_phrase(count: int) -> str:
    """``"1 staged change"`` / ``"3 staged changes"`` for dialogs and menu rows."""
    return f"{count} staged change{'' if count == 1 else 's'}"


async def confirm_discard(
    ctx: "AppContext", staged: int, *, verb: str = "applying"
) -> bool:
    """Ask before dropping staged changes on the way out; ``True`` means discard.

    The shared unsaved-changes dialog: the safe way out (keep editing) sits left, the
    committing Discard right and default, per the app's dialog convention.

    Args:
        ctx: Shared application context (for the UI surface).
        staged: How many changes would be dropped.
        verb: What applying them would have done — ``"applying"`` for the local
            editor, ``"sending"`` for a remote one — woven into the question.

    Returns:
        ``True`` if the user chose to discard; ``False`` to keep editing.
    """
    choice = await ctx.ui.dialog(
        f"Discard {changes_phrase(staged)} without {verb} them?",
        [("Keep editing", "keep"), ("Discard", "discard")],
        title="Unsaved changes",
        default=1,
        danger=True,
    )
    return choice == "discard"


def section_heading(label: str) -> Separator:
    """A ``── Label ──`` accent section heading for a grouped select list.

    The one form every grouped list's headings take (the main menu's categories, the
    config editor's setting groups, Watchtower's Alerts/Watched nodes, Courier's
    Outbox/Finished), so sections read the same on every screen.

    It is also what makes a heading a *landmark*: the row is marked
    :attr:`~meshterm.ui.tui.select.Separator.heading`, so it re-pins to the top row once its
    section scrolls under it and the Ctrl+PageUp/PageDown jumps step by it. Building the row
    by hand is how a section loses that — go through here.
    """
    return Separator(f"── {label} ──", style="accent", heading=True)


def fit_cells(text: str, width: int, *, align: str = "left") -> str:
    """Fit ``text`` into exactly ``width`` display cells, ellipsizing overflow.

    The lane-fitting helper every fixed-width name/label column uses: padding and
    truncation are measured in display cells (wide glyphs count 2), so a name carrying
    an emoji or CJK character can't skew the lanes the way ``str.ljust`` would.

    Args:
        text: The text to fit.
        width: The exact cell width to return.
        align: ``"left"`` (pad on the right) or ``"right"`` (pad on the left).

    Returns:
        A string measuring exactly ``width`` cells.
    """
    if cell_len(text) > width:
        kept = ""
        for ch in text:
            if cell_len(kept + ch) > width - 1:
                break
            kept += ch
        text = kept + "…"
    pad = " " * max(0, width - cell_len(text))
    return pad + text if align == "right" else text + pad


__all__ = [
    "Lane",
    "back_rows",
    "changes_phrase",
    "column_header",
    "confirm_discard",
    "exit_rows",
    "fit_cells",
    "lane_header",
    "lane_row",
    "menu_rows",
    "section_heading",
]
