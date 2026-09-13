# SPDX-License-Identifier: Apache-2.0
"""What a tool answers with, stated as data before anything decides how it looks.

A scripted run used to say its answer by *printing* it: ``Tool.run`` called
``ctx.ui.show(<a table built by ui/script.py>)`` and returned a :class:`ToolResult`
carrying only bookkeeping. By the time the CLI boundary saw that result the contacts had
already gone down the pipe as a space-aligned table, and there was nothing left to hand a
second format. That is exactly why ``--json`` reached two commands out of twenty and
stopped: each one that wanted it grew an ``if ctx.json_output:`` branch *above* the
rendering, restating the whole answer in a second dialect nobody else could reuse.

``exit_code`` already showed the shape that works — the tool states it, the boundary turns
it into a process status, and no tool has ever called :func:`sys.exit`. This module lets
the answer travel the same way:

    **A tool states its answer as data. A renderer turns data into bytes. The CLI
    boundary picks the renderer** (see :mod:`meshterm.ui.renderers`).

A report is a short sequence of blocks, and there are only two kinds because the plain
face proved only two are needed — a :class:`Listing` (records under a header line) and
:class:`Facts` (one thing described key by key). A :class:`Facts` block covers the
one-scalar answer too, through :data:`BARE`: ``config get`` prints its value alone
*because the caller named the key*, while the machine face still gets the object with the
key, the type and the label in it. Splitting that into a third block type would have
bought a second name for the same data.

**A row holds the typed value.** ``6.0``, ``None``, a :class:`~datetime.datetime` — never
a formatted cell. Each :class:`Column` carries both projections instead: the plain
:class:`Lane` that turns the value into text, and the JSON function that normalises it.
One typed row, two projections, and a third format later is a third function rather than a
third row-builder. :mod:`meshterm.ui.fields` builds the columns for the concepts the whole
app shares — a node, a time, an SNR, a route — so every listing speaks the same shapes by
construction rather than by review.

Nothing here renders. A block does not know what a gutter is, and a column does not know
whether anyone will ever ask it for JSON.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

#: How a :class:`Facts` block reaches the *plain* face. The machine face is the same
#: object either way — this only chooses what a person at a prompt is shown.
PAIRS = "pairs"
#: One named field's value, alone, with no key in front of it. What ``config get`` prints:
#: the caller named the key, so repeating it back is one more thing to strip off.
BARE = "bare"
#: Nothing at all. An acknowledgement — ``config advert``, ``channels join`` — whose plain
#: report is its exit status. The machine face still gets a document, because "prints
#: nothing" is an answer a person can act on and a program cannot.
SILENT = "silent"


def _text(value: Any) -> str:
    """The last-resort plain projection: the value as text, or the absent token."""
    from . import script

    return script.NONE if value is None else str(value)


def normalise(value: Any) -> Any:
    """A typed value in its machine form: JSON-ready, and the same on every host.

    The one place the contract's cross-cutting value rules are applied, so a command
    cannot forget one:

    * a **time** is UTC, RFC 3339, ``Z``-suffixed, to the second — always twenty
      characters, so string comparison is time comparison. A local offset is a fact about
      the machine that ran the command rather than about the event, and two hosts asking
      the same radio the same question must produce the same document. A *naive* time is
      one whose offset we do not actually know, which is not a fact, so it reads as absent
      exactly as it does on the plain face.
    * an **enum** is its value, not its Python name.
    * a **path** is its text.
    * everything else travels as it is, typed: ``9`` and not ``"9"``, ``false`` and not
      ``"false"``, ``None`` and not the plain face's ``-``.

    Args:
        value: The typed value from a report row.

    Returns:
        The value in a form :mod:`json` can write.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, Enum):
        return normalise(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [normalise(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class Lane:
    """One plain-text column, drawn from its :class:`Column`'s typed value.

    A lane is the *reader's* half of a column. Several may share one column where the
    plain face spends more room on a concept than the machine face does — a node is one
    JSON object and up to four plain columns (its name, its role, its hash, its key),
    which is the whole reason a column owns a list of these rather than one header string.

    Attributes:
        header: The uppercase heading, and the field a caller greps for. Renaming one
            silently breaks a script, so it is as much a contract as the exit status.
        render: Turns the column's typed value into this lane's cell.
        align: ``"right"`` for a lane of magnitudes — a column of numbers or ages is only
            comparable by eye once the digits line up.
        width: The cells this lane is pinned to in a *live* listing, where the widths
            cannot be measured because the records have not arrived yet. Ignored by an
            ordinary listing, which sizes every lane to its widest value.
    """

    header: str
    render: Callable[[Any], str] = _text
    align: str = "left"
    width: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Column:
    """One concept in a report, with a projection for each face.

    Attributes:
        key: The name this concept goes under: the JSON key, and the key a row's mapping
            holds its typed value at.
        lanes: The plain columns this concept draws — usually one. **Empty means the
            concept has no plain face**, which is how ``config show`` carries a setting's
            type and enum label into the document while printing the two fields a reader
            wants.
        json: The typed value's machine projection, or ``None`` where the concept has no
            machine face — the mirror of an empty ``lanes``, and what a preference's
            DESCRIPTION is: prose for whoever is choosing a value, carrying nothing a
            program would branch on. Defaults to :func:`normalise`, which handles the
            scalars; a shared shape (a node, a position) passes its own.
    """

    key: str
    lanes: tuple[Lane, ...] = ()
    json: Callable[[Any], Any] | None = normalise


@dataclass(frozen=True, slots=True, kw_only=True)
class Block:
    """One part of an answer.

    Attributes:
        key: The name this block goes under in a multi-block document.
        plain_only: Whether this block is a rendering for a person that the machine face
            has no use for. The one case is a stream's closing summary: ``monitor`` prints
            a per-node aggregate under its live capture, and a consumer holding every
            record of that capture can compute the same thing — where a second *shape* of
            line in the middle of an otherwise homogeneous NDJSON stream costs every
            reader a discriminator it would otherwise never need.
    """

    key: str
    plain_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Listing(Block):
    """Records under a header line: a listing plain, an array of objects in JSON.

    Attributes:
        columns: The concepts each record carries, in machine-face order.
        rows: One mapping per record, keyed by ``Column.key`` and holding **typed**
            values.
        order: The lane headers in plain draw order. Empty means "as declared", which is
            what most listings want; a listing states it where the two orders genuinely
            differ — ``contacts`` puts HEARD and PKTS between a node's TYPE and its HASH,
            and ``monitor`` puts the one unbounded field (a node's name) last so it can
            never push a column.
        headed: Whether the plain face draws the header line. ``config show`` is the one
            listing that does not: it is an array of settings to a parser and a
            ``sysctl -a`` block to a reader, and a header line over two columns whose keys
            are the answer would be furniture.
    """

    columns: tuple[Column, ...]
    rows: Sequence[Mapping[str, Any]] = ()
    order: tuple[str, ...] = ()
    headed: bool = True

    def __post_init__(self) -> None:
        """Reject an ``order`` that does not name this listing's own lanes.

        A typo here draws a listing with a column missing and no complaint, which is the
        one failure a header line cannot show. Cheaper to raise at construction.
        """
        if not self.order:
            return
        known = {lane.header for column in self.columns for lane in column.lanes}
        if set(self.order) != known or len(self.order) != len(known):
            raise ValueError(f"{self.key}: order must name every lane exactly once")

    def lanes(self) -> list[tuple[Column, Lane]]:
        """The plain columns in draw order, each with the column it reads from."""
        pairs = [(column, lane) for column in self.columns for lane in column.lanes]
        if not self.order:
            return pairs
        by_header = {lane.header: (column, lane) for column, lane in pairs}
        return [by_header[header] for header in self.order]


@dataclass(frozen=True, slots=True, kw_only=True)
class Facts(Block):
    """One thing, described key by key: a ``sysctl -a`` block plain, an object in JSON.

    Attributes:
        fields: The concepts, in the order both faces present them.
        values: The typed value per ``Column.key``.
        shape: :data:`PAIRS`, :data:`BARE` or :data:`SILENT` — what the *plain* face
            prints. The machine face is the object either way.
        bare: With :data:`BARE`, the one field whose value is printed alone.
        omit_absent: Whether a field with no value is left out of the plain block
            entirely. Only ``info`` wants this, and deliberately: its readings are
            optional device queries, so a missing key says "this radio does not report
            it" where a dash would claim it reported nothing. The machine face keeps every
            declared key and writes ``null``, because "does not report" and "reported
            nothing" are the same fact to a caller and a variable key set costs every
            consumer a membership test on every field.
    """

    fields: tuple[Column, ...]
    values: Mapping[str, Any] = field(default_factory=dict)
    shape: str = PAIRS
    bare: str = ""
    omit_absent: bool = False


#: A tool's whole answer. Deliberately a bare sequence rather than a class of its own:
#: there is nothing a report *does*, and the two rules about how several blocks combine
#: belong to the renderer that combines them (see :mod:`meshterm.ui.renderers`).
Report = tuple[Block, ...]
