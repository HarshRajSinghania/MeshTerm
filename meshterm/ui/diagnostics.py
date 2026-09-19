# SPDX-License-Identifier: Apache-2.0
"""The Diagnostics page: the bug-report block, on a frame with nothing else on it.

**A bare frame, and the reason is mechanical rather than aesthetic.** The one thing this
page is for is getting its text out of the terminal and into an issue, and a terminal is
selected by dragging: a panel border is characters sitting on the *same lines* as the
content, so every line of a line-wise selection arrives with a box-drawing glyph welded to
each end. Same for a header, a footer hint and an F-key lane, which arrive as text that
looks like part of the report to whoever reads the paste. So this page draws its rows on a
:attr:`~meshterm.ui.tui.screen.Screen.bare` frame, the one the share screen uses for the
same kind of reason — there, a camera wants no contrast arguing with the code; here, a
clipboard wants no furniture pretending to be data. Esc leaves, as everywhere.

The page's **one** line of its own is the save hint, and that is a trade rather than an
exception: a bare frame has nowhere to advertise a key, so the alternative was a page that
could write the file and never said so. One trailing line after a blank row costs a paste
far less than a border costs it on every line, and a drag that follows the block stops
above it anyway (see :class:`DiagnosticsPage`).

The rows come from the tool's :mod:`~meshterm.ui.report`, projected through the very lanes
the command line prints (:func:`~meshterm.ui.renderers.facts_pairs`), so what is copied off
this screen and what ``meshterm diagnostics`` writes to a pipe are the same facts in the
same words. Only the *layout* differs, and in one respect: the scripted face crops a value
too wide for its lane, which is right for a record somebody will split on whitespace and
wrong for a page whose entire job is to be complete — so here a long value hangs under its
own block instead (the app-wide rule for a wrapped labelled row).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from .tui.screen import ScrollScreen

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext
    from .report import Facts, Listing, Report


#: The key that writes the block to a file, and the only affordance this page advertises.
SAVE_KEY = "s"


class DiagnosticsPage(ScrollScreen):
    """The bug-report block, full-screen and unadorned, with one thing it can do.

    Bare, so a mouse drag takes the facts and only the facts; scrollable, because the
    block outgrows a 26-row console and a truncated bug report is worse than a scrolled
    one.

    **The save line is the one concession, and it is a deliberate one.** A bare frame has
    no footer and no F-key lane, so a page that could save and never said so would be a
    key nobody could find — on the PicoCalc especially, where the lane is normally the only
    place an affordance is advertised at all. So the page carries its own last line. It
    costs a paste one trailing line where a border would have cost it a character on
    *every* line, and a blank row above it means a drag that follows the block stops
    before it of its own accord.

    Saving answers **on that same line** rather than in a dialog: the reader pressed a key
    to be told where a file went, and a box over the page would cover the thing they were
    about to copy. It is also what lets a failure — a read-only home, a full disk — be said
    without inventing an error screen for it.
    """

    bare = True

    def __init__(self, report: Report, *, save_path: Path) -> None:
        """Lay the report out as the page's body.

        Args:
            report: The tool's report — the same blocks the CLI renders.
            save_path: Where :data:`SAVE_KEY` writes the block.
        """
        super().__init__(Text(""), title="Diagnostics", floating=False)
        self._report = report
        self._save_path = save_path
        self._saved: Path | None = None
        self._error: str | None = None
        self._refresh()

    def handle(self, action: str, data: str = "") -> None:
        """Save on :data:`SAVE_KEY`; scroll or leave on everything else.

        The key arrives as bare text, which a read-only page can afford precisely because
        it has no filter and no field for a letter to mean anything else in.
        """
        if action == "text" and data.lower() == SAVE_KEY:
            self._save()
            return
        super().handle(action, data)

    def _save(self) -> None:
        """Write the block, and say on the page's own last line what happened."""
        from ..tools.diagnostics import write_report

        try:
            self._saved, self._error = write_report(self._report, self._save_path), None
        except OSError as exc:
            # A home that is read-only, full, or not there. The reader can still copy the
            # block off the screen, which is the whole reason that path exists as well.
            self._saved, self._error = None, exc.strerror or str(exc)
        self._refresh()

    def _refresh(self) -> None:
        """Rebuild the body: the block, a blank row, then the page's one line of its own."""
        self.replace_content(Group(render_report(self._report), Text(""), self._status()))

    def _status(self) -> Text:
        """The last line: what the key does, or what it did."""
        if self._error is not None:
            return Text(f"could not save to {self._save_path}: {self._error}", style="err")
        if self._saved is not None:
            return Text(f"saved to {self._saved}", style="ok")
        return Text(f"{SAVE_KEY}  save this to a file", style="muted")


async def open_diagnostics_page(ctx: AppContext, report: Report, *, save_path: Path) -> None:
    """Open the Diagnostics page full-screen and hold it until the reader backs out.

    Args:
        ctx: The shared application context (must be running the interactive TUI).
        report: The block to show.
        save_path: Where the page's save key writes.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the Diagnostics page is only available in the menu")
    await ctx.ui.session.run_screen(DiagnosticsPage(report, save_path=save_path))


def render_report(report: Report) -> RenderableType:
    """Lay a report out as the page's body: each block in order, a blank row between.

    A block that would draw nothing — a listing with no rows — is left out along with its
    separator, exactly as the scripted face leaves it out, so the two never disagree about
    whether a section exists.

    Args:
        report: The blocks to draw.

    Returns:
        The page body.
    """
    from .report import Facts

    parts: list[RenderableType] = []
    for block in report:
        drawn = _facts(block) if isinstance(block, Facts) else _listing(block)
        if drawn is None:
            continue
        if parts:
            parts.append(Text(""))
        parts.append(drawn)
    return Group(*parts)


def _facts(block: Facts) -> RenderableType | None:
    """One facts block: key on the left, value on the right, wrapping under itself."""
    from .renderers import facts_pairs

    rows = facts_pairs(block)
    if not rows:
        return None
    table = _grid()
    table.add_column(no_wrap=True)
    # The one column allowed to wrap, and the reason this page is not just the scripted
    # renderer at a different width: a config directory or a connection error cropped to
    # the frame is a bug report missing the half that mattered.
    table.add_column(overflow="fold")
    for key, value in rows:
        table.add_row(Text(key, style="muted"), Text(value))
    return table


def _listing(block: Listing) -> RenderableType | None:
    """One listing: its header row, then its records. Nothing where there are no records."""
    if not block.rows:
        return None
    lanes = block.lanes()
    table = _grid()
    for _column, lane in lanes:
        table.add_column(justify=lane.align, no_wrap=True)
    table.add_row(*(Text(lane.header, style="muted") for _column, lane in lanes))
    for row in block.rows:
        table.add_row(*(Text(lane.render(row.get(column.key))) for column, lane in lanes))
    return table


def _grid() -> Table:
    """A frameless table that starts in column zero.

    No box, no edge padding and no leading pad: every cell of chrome on a row is a
    character the clipboard carries, and an indent before the first key is one the reader
    of the issue has to strip off every line.
    """
    from .script import GUTTER

    return Table(
        box=None,
        show_header=False,
        show_edge=False,
        pad_edge=False,
        expand=False,
        padding=(0, GUTTER, 0, 0),
    )


__all__ = ["DiagnosticsPage", "open_diagnostics_page", "render_report"]
