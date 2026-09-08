"""The two faces of one answer: a renderer turns a report into bytes.

A tool states its answer as a :mod:`~meshterm.ui.report`; this module is where that
becomes output, and the CLI boundary picks which one runs (see
:func:`~meshterm.cli.main_callback`). Nothing above here knows what a gutter or a comma is,
and nothing below here knows what a contact is. That separation is the whole point: a
third format later — YAML, CSV, whatever somebody needs — is a new class in this file and
not an edit to twenty tools.

**Plain** (:class:`PlainRenderer`) is for a person at a prompt: aligned columns, ages, a
route drawn with arrows, and a ``sysctl -a`` block for a set of facts about one thing. It
is written in the vocabulary of :mod:`meshterm.ui.script`, which is where the reasons for
each of its rules live.

**JSON** (:class:`JsonRenderer`) is for a program, often on another machine and often
later. It prints **the answer itself** — an array for a listing, an object for a set of
facts — compact, one line, UTF-8, no ASCII escaping, keys in the order the report declares
them. There is no wrapper: ``meshterm contacts --json | jq '.[].node.name'`` reads what it
looks like it reads, and the report a caller needs is still ``$?``, exactly as it is
without the flag. So a failure prints **nothing at all** on stdout — the ``meshterm: what
went wrong`` line stays on stderr where every utility puts it, and stdout holding a valid
document is then a reliable signal that the command worked.

Two rules cover a report of more than one block, and between them they produce the
documents ``trace`` and ``tx-optimize`` are specified with. A **single-block** report *is*
its block's document. A **multi-block** report is one object: each :class:`Listing` under
its own key, and each :class:`Facts` merged in at the top level — a facts block is the set
of facts about the one thing the command answered, so it is the document rather than a
compartment inside it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from enum import Enum
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.text import Text

from . import script
from .report import BARE, SILENT, Block, Facts, Lane, Listing, Report

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext


class OutputFormat(str, Enum):
    """The formats a scripted run can print its answer in.

    An enum rather than the ``--json`` boolean it replaces, because the boolean was
    already being asked questions it could not answer — ``tx-optimize`` read it as "may I
    prompt?" — and because a third member here costs one renderer and no tool changes at
    all. ``--json`` stays the documented spelling and is sugar for this.
    """

    PLAIN = "plain"
    JSON = "json"


class Renderer:
    """Turns a report into bytes. One subclass per format."""

    def render(self, report: Report | None) -> None:
        """Print a completed report, or nothing at all when there is none."""
        raise NotImplementedError

    @contextmanager
    def stream(self, listing: Listing) -> Iterator[Any]:
        """Open a live listing whose rows arrive one at a time.

        The narrow second seam, and it exists because a stream has no end to return from:
        ``monitor`` and ``chat listen`` run until a window closes or somebody interrupts
        them, so their answer cannot be a value handed back at the end. Two commands use
        it. Nothing else should — a command that can build a whole report should, because
        a report is the thing every format already knows how to read.

        Args:
            listing: The shape of the records to come — its columns, its lane order, and
                (for the plain face) the width each lane is pinned to. Its ``rows`` are
                ignored; rows arrive through the yielded callable.

        Yields:
            A callable taking one typed row mapping.
        """
        raise NotImplementedError


class PlainRenderer(Renderer):
    """The face a person reads: aligned text on the scripted console.

    Attributes:
        console: The plain console (see :func:`meshterm.ui.script.console`).
    """

    def __init__(self, console: Console) -> None:
        """Bind the renderer to the console its output goes to."""
        self.console = console

    def render(self, report: Report | None) -> None:
        """Draw each block in order, one blank line between them.

        The separator is counted off what was *drawn* rather than off the block index: a
        block can render to nothing (a listing with no rows, an acknowledgement whose plain
        answer is its exit status), and a blank line above the first visible block would be
        a line of output the command did not have.
        """
        drawn_any = False
        for block in report or ():
            drawn = self._block(block)
            if drawn is None:
                continue
            if drawn_any:
                self.console.print(script.blank())
            self.console.print(drawn)
            drawn_any = True

    def _block(self, block: Block) -> Any:
        """The renderable for one block, or ``None`` where it prints nothing."""
        if isinstance(block, Listing):
            return self._listing(block) if block.rows else None
        if isinstance(block, Facts):
            return self._facts(block)
        raise TypeError(f"unrenderable block: {block!r}")  # pragma: no cover - closed set

    def _listing(self, block: Listing) -> Any:
        """A listing as its header line and its padded records."""
        lanes = block.lanes()
        table = script.columns(
            *(lane.header for _, lane in lanes),
            right=[lane.header for _, lane in lanes if lane.align == "right"],
        )
        # `config show` is an array to a parser and a `sysctl -a` block to a reader, so it
        # keeps the columns and drops the heading row. Same table either way — the padding,
        # the no-wrap and the crop are what make a record splittable, and none of them is
        # the header's doing.
        table.show_header = block.headed
        for row in block.rows:
            table.add_row(*(lane.render(row.get(column.key)) for column, lane in lanes))
        return table

    def _facts(self, block: Facts) -> Any:
        """A facts block as a key/value listing, one bare value, or nothing."""
        if block.shape == SILENT:
            return None
        if block.shape == BARE:
            column = next(c for c in block.fields if c.key == block.bare)
            # As a Text, not a markup string: this is a private key, a share URL or a
            # remote node's own reply, and Rich would read a bracket in one as a style tag.
            return Text(column.lanes[0].render(block.values.get(column.key)))
        rows: list[tuple[str, str]] = []
        for column in block.fields:
            value = block.values.get(column.key)
            if block.omit_absent and (value is None or value == ""):
                continue
            for lane in column.lanes:
                rows.append((lane.header, lane.render(value)))
        return script.pairs(rows)

    @contextmanager
    def stream(self, listing: Listing) -> Iterator[Any]:
        """Print the header line, then one pinned-lane record per row as it arrives."""
        lanes = listing.lanes()
        pinned = script.stream(
            *((lane.header, _lane_width(lane)) for _, lane in lanes),
            right=[lane.header for _, lane in lanes if lane.align == "right"],
        )
        self.console.print(pinned.header, highlight=False)

        def row(record: Mapping[str, Any]) -> None:
            cells = [lane.render(record.get(column.key)) for column, lane in lanes]
            self.console.print(pinned.record(*cells), highlight=False)

        yield row


class JsonRenderer(Renderer):
    """The face a program reads: one compact document, or one per streamed record.

    Attributes:
        console: The plain console, used only for its stream — the document is written
            through :func:`json.dumps` rather than by Rich, so nothing can pad, wrap,
            highlight or otherwise adorn it.
    """

    def __init__(self, console: Console) -> None:
        """Bind the renderer to the console its output goes to."""
        self.console = console

    def render(self, report: Report | None) -> None:
        """Print the report as one document, or nothing when there is no report at all."""
        if report is None:
            return
        blocks = [block for block in report if not block.plain_only]
        if not blocks:
            return
        self._write(self._document(blocks))

    def _document(self, blocks: Sequence[Block]) -> Any:
        """Fold the blocks into the one value this command answers with."""
        if len(blocks) == 1:
            return self._value(blocks[0])
        document: dict[str, Any] = {}
        for block in blocks:
            if isinstance(block, Facts):
                document.update(self._facts(block))
            else:
                document[block.key] = self._value(block)
        return document

    def _value(self, block: Block) -> Any:
        """One block's document: an array for a listing, an object for a set of facts."""
        if isinstance(block, Listing):
            return [self._row(block, row) for row in block.rows]
        if isinstance(block, Facts):
            return self._facts(block)
        raise TypeError(f"unrenderable block: {block!r}")  # pragma: no cover - closed set

    @staticmethod
    def _row(block: Listing, row: Mapping[str, Any]) -> dict[str, Any]:
        """One record, with every declared key present (``null`` where it is absent)."""
        return {
            column.key: column.json(row.get(column.key))
            for column in block.columns
            if column.json is not None
        }

    @staticmethod
    def _facts(block: Facts) -> dict[str, Any]:
        """One facts block as an object, keeping every declared key.

        ``omit_absent`` is a *plain* concession — ``info`` leaves out a reading the
        firmware does not answer for, so a dash never claims the radio reported nothing.
        A consumer cannot act on that distinction and pays a membership test on every
        field for it, so the document keeps the key and writes ``null``.
        """
        return {
            column.key: column.json(block.values.get(column.key))
            for column in block.fields
            if column.json is not None
        }

    def _write(self, document: Any) -> None:
        """Write one compact document and terminate it with a newline."""
        line = json.dumps(
            document,
            # A node name in Cyrillic is a node name, not a run of `\uXXXX`. The scripted
            # console already reconfigures stdout to UTF-8 for exactly this class of
            # character.
            ensure_ascii=False,
            # Compact and one line, so a stream and a one-shot answer are read the same
            # way and `jq .` can re-indent for a person at no cost. Nothing can un-stream
            # a document that was pretty-printed across four hundred lines.
            separators=(",", ":"),
        )
        self.console.file.write(line + "\n")
        self.console.file.flush()

    @contextmanager
    def stream(self, listing: Listing) -> Iterator[Any]:
        """Print one document per record, flushed, as each arrives.

        Every line is the same shape, which is what makes the stream readable without a
        discriminator: a consumer reads line by line and never has to ask which kind of
        record it is holding. The closing per-node summary the plain face draws is marked
        ``plain_only`` for that reason — it is derivable from the records above it, and a
        second shape mid-stream would cost every reader a branch it otherwise never needs.
        """

        def row(record: Mapping[str, Any]) -> None:
            self._write(self._row(listing, record))

        yield row


def for_format(output: OutputFormat, console: Console) -> Renderer:
    """The renderer for one output format.

    Args:
        output: What the caller asked for.
        console: The console to print on.

    Returns:
        The renderer.
    """
    return JsonRenderer(console) if output is OutputFormat.JSON else PlainRenderer(console)


@contextmanager
def stream(ctx: AppContext, listing: Listing) -> Iterator[Any]:
    """Open a live listing on whichever renderer this run is using.

    Args:
        ctx: The application context, for its output format and console.
        listing: The stream's shape (see :meth:`Renderer.stream`).

    Yields:
        A callable taking one typed row mapping.
    """
    with for_format(ctx.output, ctx.console).stream(listing) as row:
        yield row


def _lane_width(lane: Lane) -> int:
    """How wide a streamed lane is pinned.

    A live capture cannot size its columns the way :func:`~meshterm.ui.script.columns`
    does, because it has not seen the records yet. So each lane declares the width it is
    pinned to, and a value wider than that overruns and pushes the rest of its row right
    rather than being cut — which is why the one unbounded field goes last (see
    :class:`meshterm.ui.script.Lanes`).
    """
    return max(len(lane.header), lane.width)
