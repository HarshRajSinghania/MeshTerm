"""The scripted CLI's output language — plain text, aligned, unadorned.

MeshTerm has two front ends and they are read by different things. The menu is read by a
person sitting in front of it, and everything else in ``meshterm/ui/`` is built for that
reader: colour that carries meaning, glyphs that stand for concepts, frames that group.
The CLI is read by ``awk``. So it gets its own vocabulary, and this module is all of it.

The rules, and why each one:

* **No colour.** ``stdout`` is a pipe more often than a terminal, and a colour that
  survives into a file is noise in it. The console this module builds has
  ``color_system=None``, so Rich emits no escape sequence at all — not a dim, not a bold.
  That is stricter than ``no_color``, which keeps the attributes and drops only the hues.
  A *highlight* colour is reserved for the one thing that would be unreadable without it
  (a route graph's emphasised path); nothing in the CLI draws one, and nothing claims the
  colour speculatively.
* **No wrapping.** A record is a line. Wrapping turns one record into two and puts the
  second one's fields under the wrong headings, so the console is made :data:`WIDTH`
  cells wide — far past any real line — and every column is ``no_wrap``. Long output runs
  off the right, and the terminal (or ``less -S``, or the reader's pipe) decides what to
  do about it.
* **No frames.** No panel borders, no table boxes, no rules, no titles. The command the
  reader typed is the title; a box around the answer is a second one.
* **Aligned columns, one header line.** ``ps`` and ``df``'s shape: an uppercase header
  row, then records, fields padded apart by :data:`GUTTER` spaces. See :func:`columns`.
* **Names are quoted where they share a line.** A node name can hold a space, a comma,
  even a quote, so in a listing a bare name is not a field — it is however many fields the
  name happens to split into. Every name in a :func:`columns` row or a :func:`path` line
  goes through :func:`quote`. A :func:`pairs` line is the exception and needs no quoting:
  it has exactly two fields, so the value is the rest of the line whatever it holds, and
  quotes there would only be something to strip back off.
* **Times are absolute.** ``3h`` is for a person watching a screen; a script wants
  something it can sort and subtract, so every timestamp is local ISO-8601 to the second
  (:func:`stamp`). The relative ages and their recency heat stay in the menu, where the
  colour they are half made of still exists.
* **One token for "nothing".** :data:`NONE` — a lone ``-`` — in every column, so an
  absent value never has to be told apart from an empty one, an ``—``, an ``n/a`` or a
  blank.
* **No trailing whitespace.** Padding the last column to its width would put invisible
  spaces at the end of every line; the console trims them on the way out
  (:class:`_Trimmed`).

Nothing here is reachable from the menu, and nothing in the menu is reachable from here:
:class:`~meshterm.ui.surface.PlainUi` is the seam, chosen in
:func:`~meshterm.cli.main_callback` by whether a subcommand was named.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

#: The scripted console's width. Wide enough that Rich never wraps or truncates anything
#: MeshTerm can produce (a 64-hex key, a full route, a preference description), leaving
#: the reader's own terminal to deal with a line longer than itself. Not ``sys.maxsize``:
#: Rich sizes a few structures against the width, and a merely enormous number costs
#: nothing where an astronomical one does.
WIDTH = 1 << 14

#: Spaces between columns. Two, so a padded column and its neighbour never touch and a
#: lone space is never mistaken for a separator inside an unquoted field.
GUTTER = 2

#: The one token for a value that is absent, unknown, or does not apply. A reader (and a
#: parser) learns it once. Never ``—``, ``n/a``, ``never``, or an empty cell.
NONE = "-"


def quote(value: str | None) -> str:
    r"""Wrap a name in doublequotes, escaping the ones inside it.

    A node name is user-supplied text that can hold a space, a comma, or a quote, so it
    is never a field on its own — quoting is what makes it one. The escaping is the
    ordinary backslash convention and covers the backslash too, so the value reads back
    unambiguously.

    Args:
        value: The name to quote. ``None`` and the empty string both give ``""``.

    Returns:
        The quoted name, e.g. ``"Yagi-Repeater"`` or ``"He said \"hi\""``.
    """
    text = value or ""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def stamp(when: datetime | None) -> str:
    """Format a timestamp as local ISO-8601 to the second, or :data:`NONE`.

    Stored times are timezone-aware UTC; a naive one is a time whose offset we do not
    actually know, which is not a fact and so reads as absent — the same rule
    :func:`~meshterm.ui.widgets._age_seconds` applies to ages.

    Args:
        when: The instant to format.

    Returns:
        e.g. ``2026-09-07T18:22:41-04:00``, or :data:`NONE`.
    """
    if when is None or when.tzinfo is None:
        return NONE
    return when.astimezone().isoformat(timespec="seconds")


def number(value: Any, spec: str = "") -> str:
    """Format a number, or :data:`NONE` when it is absent.

    Args:
        value: The number to format; ``None`` reads as absent.
        spec: An optional :func:`format` spec, e.g. ``"+.1f"``.

    Returns:
        The formatted number, or :data:`NONE`.
    """
    if value is None:
        return NONE
    return format(value, spec) if spec else str(value)


def columns(*headers: str, right: Sequence[str] = ()) -> Table:
    """Build the scripted table: an uppercase header line, then padded records.

    The shape ``ps`` and ``df`` print — no box, no title, no edge padding, every column
    sized to its widest value and separated by :data:`GUTTER` spaces, nothing wrapped and
    nothing elided. Numeric lanes right-align, which is what makes a column of magnitudes
    comparable by eye without changing what splitting it yields.

    Args:
        *headers: The column headings, already uppercase.
        right: The headings (drawn from ``headers``) whose values right-align.

    Returns:
        A Rich :class:`Table` ready for ``ctx.ui.show``.
    """
    table = Table(
        box=None,
        show_edge=False,
        pad_edge=False,
        expand=False,
        # (top, right, bottom, left): the whole gutter hangs to the right of each cell, so
        # no line opens with an indent and the last column ends where its value does.
        padding=(0, GUTTER, 0, 0),
    )
    wanted = set(right)
    for header in headers:
        table.add_column(
            header,
            justify="right" if header in wanted else "left",
            no_wrap=True,
            # "crop", not "ignore". Both refuse to elide — neither ever writes the "…"
            # that would make a key or a route unusable to the caller — but Rich's
            # ``Text.wrap`` returns early on "ignore" (text.py: ``if overflow ==
            # "ignore": lines.append(line); continue``) and that early return is *above*
            # the justify step, so "ignore" silently discards ``justify="right"``. The
            # two settings differ only past the console's 16384 cells, where "crop" cuts
            # and "ignore" also cuts; they differ on every line before that, where only
            # "crop" aligns.
            overflow="crop",
        )
    return table


def pairs(rows: Iterable[tuple[str, str]]) -> Table:
    """Build the scripted key/value listing: key then value, aligned, no header line.

    What a set of facts about one thing prints as (``meshterm info``, ``config show``,
    ``preferences show``) — the ``sysctl -a`` shape. Keys are the ones the matching
    ``get``/``set`` subcommand takes, so a line read out of ``show`` can be typed back in.

    Args:
        rows: ``(key, value)`` pairs in display order.

    Returns:
        A Rich :class:`Table` with no header line.
    """
    table = Table(
        box=None,
        show_edge=False,
        show_header=False,
        pad_edge=False,
        expand=False,
        padding=(0, GUTTER, 0, 0),
    )
    table.add_column(no_wrap=True, overflow="crop")
    table.add_column(no_wrap=True, overflow="crop")
    for key, value in rows:
        table.add_row(key, value)
    return table


def path(hops: Iterable[tuple[str, str | None]]) -> str:
    """Render a hop sequence as the CLI's path line.

    ``"Origin" (3d),"Relay" (f2),"Us" (a1)`` — each node's name quoted (:func:`quote`),
    its hash in parentheses *outside* the quotes, hops separated by a bare comma.

    The hash is there because the CLI has no colour. On a screen a route's hops are told
    apart by their key-derived hues, and matched to a route graph's one-byte labels the
    same way; with the colour gone, the hash is what carries that identity.

    Our own node is a hop like any other — named and hashed, never the menu's ``★``. The
    star says "you already know who this is", which is true of a reader and false of a
    parser.

    The comma is the separator ``--path`` already takes, and the quoting is what keeps the
    line splittable when a name contains one.

    Args:
        hops: ``(label, hash)`` pairs in propagation order; a ``None`` hash prints the
            name alone, for a node whose identity is genuinely unknown.

    Returns:
        The joined path line, or :data:`NONE` when there are no hops.
    """
    parts = [f"{quote(label)} ({value})" if value else quote(label) for label, value in hops]
    return ",".join(parts) if parts else NONE


# -- the console ---------------------------------------------------------------------


class _Trimmed:
    """A text stream that drops trailing whitespace from every line passing through it.

    Rich pads a table's cells to their column width, the last column's included, which
    would leave invisible spaces at the end of every record. Trimming at the stream is the
    one place that catches all of it — table padding, a justified :class:`Text`, a blank
    line Rich decided to pad — without any renderable having to know.

    What it holds back is only the *trailing whitespace run*, never a whole line. Rich
    writes a rendered row segment by segment and, on Windows, flushes after each one, so a
    wrapper that waited for the newline before deciding would be asked to decide dozens of
    times a row — and one that trimmed at flush would throw away the very padding that
    separates two columns. Everything up to the last non-space character goes straight
    through; the spaces after it wait to find out whether a newline or another column
    follows. At end of stream nothing follows, and trailing whitespace is exactly what it
    is then, so the held run is simply dropped.
    """

    def __init__(self, stream: Any) -> None:
        """Wrap ``stream``, holding back whatever trailing whitespace has arrived."""
        self._stream = stream
        self._held = ""

    def write(self, text: str) -> int:
        """Write ``text``, emitting whitespace only once something follows it on the line."""
        pending = self._held + text
        if "\n" in pending:
            *lines, pending = pending.split("\n")
            self._stream.write("".join(line.rstrip() + "\n" for line in lines))
        body = pending.rstrip()
        if body:
            self._stream.write(body)
        self._held = pending[len(body) :]
        return len(text)

    def flush(self) -> None:
        """Flush the wrapped stream, keeping the held whitespace held.

        Deliberately *not* a place that emits: a flush lands between two columns as often
        as at the end of a line, and it carries no information about which.
        """
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else (``isatty``, ``encoding``, ...) to the real stream."""
        return getattr(self._stream, name)


def console(stream: Any = None) -> Console:
    """Build the scripted console: no colour, no wrapping, no trailing whitespace.

    Args:
        stream: The file to write to; defaults to ``sys.stdout``.

    Returns:
        A :class:`~rich.console.Console` that emits plain text and nothing else.
    """
    import sys

    target = sys.stdout if stream is None else stream
    reconfigure = getattr(target, "reconfigure", None)
    if reconfigure is not None:
        try:  # a name or mark outside cp1252 must not raise on a legacy Windows console
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # pragma: no cover - stream not reconfigurable
            pass
    return Console(
        file=_Trimmed(target),
        # Not `no_color`, which keeps bold, dim, reverse and the rest and drops only the
        # hues. `color_system=None` is the one setting under which Rich emits no escape
        # sequence at all.
        color_system=None,
        width=WIDTH,
        # Rich would otherwise notice a real terminal and soft-wrap to its width.
        soft_wrap=True,
        highlight=False,
        emoji=False,
        # A node broadcasts its own name, so every name on this console is remote data.
        # Rich reads `[...]` as a style tag: `[bold]Loud` printed as `Loud` — silently
        # corrupted, and no longer the string that identifies the node — while `[/]Bob`
        # raised MarkupError and took the whole command down with it. Nothing the scripted
        # CLI prints is ever marked up, so the parser has nothing to do here but misfire.
        markup=False,
    )


def stderr_console() -> Console:
    """The console for everything that is *about* the run rather than part of its answer.

    stdout carries what the command was asked for and nothing else, so a progress bar, a
    log line and an error message all go here instead — visible in a terminal, absent from
    ``meshterm contacts > contacts.txt``. This one keeps its colour and its wrapping: a
    terminal is the only thing that ever reads it.

    Returns:
        A themed :class:`~rich.console.Console` writing to ``sys.stderr``.
    """
    import sys

    from .theme import active_theme

    return Console(file=sys.stderr, theme=active_theme(), markup=False, emoji=False)


# -- the safety net ------------------------------------------------------------------


def flatten(renderable: RenderableType) -> list[RenderableType]:
    """Strip framing from anything reaching the scripted console still wearing it.

    Every CLI surface is *written* plain — the tools build their output through
    :func:`columns`, :func:`pairs` and :func:`path`. This is the net under that, for a
    renderable shared with the menu that still arrives boxed: a :class:`Panel` gives up
    its border and title and yields its body, a :class:`Table` gives up its box, title and
    expansion, and a :class:`Group` is flattened member by member.

    It is deliberately not a *design*: a screen's table has the menu's columns, not the
    CLI's, so passing it through here makes it printable, not right. Anything a script is
    meant to read gets a plain renderer of its own.

    Args:
        renderable: What a tool handed to :meth:`~meshterm.ui.surface.PlainUi.show`.

    Returns:
        The unframed renderables to print, in order.
    """
    if isinstance(renderable, Panel):
        return flatten(renderable.renderable)
    if isinstance(renderable, Table):
        renderable.title = None
        renderable.caption = None
        renderable.box = None
        renderable.show_edge = False
        renderable.pad_edge = False
        renderable.expand = False
        renderable.padding = (0, GUTTER, 0, 0)
        for column in renderable.columns:
            column.no_wrap = True
            column.overflow = "crop"
            column.ratio = None
        return [renderable]
    if isinstance(renderable, Group):
        out: list[RenderableType] = []
        for child in renderable.renderables:
            out.extend(flatten(child))
        return out
    return [renderable]


def blank() -> Text:
    """One empty line, for separating a listing from the block after it."""
    return Text("")
