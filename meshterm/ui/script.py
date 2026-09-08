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
from dataclasses import dataclass
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

#: The separator between a route's hops, spaced — the same ``→`` the menu's path lines
#: draw with (:data:`meshterm.ui.pathline._ARROW`), and device-verified present in the
#: PicoCalc console font. It carries the direction the packet travelled, and it is what
#: makes the quoting unnecessary: a comma inside a node's name can no longer be read as a
#: hop boundary.
ARROW = " → "

#: How wide a *written page* wraps. Not the terminal's width: the same paragraph must read
#: the same when it is piped, redirected, or shipped as the machine face's ``text`` field,
#: and 72 is the width every screen in this app is already held to.
PAGE_WIDTH = 72


#: The characters with a readable escape of their own. Everything else that cannot travel
#: in a record falls through to ``\xNN``/``\uNNNN`` below.
_BREAKS = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}

#: Codepoints that must never reach stdout unescaped, as a *category* rather than a list:
#: every C0 and C1 control, plus the two Unicode separators a terminal also breaks a line
#: on. A four-entry table caught the newline and the tab and let ESC through — and an ESC
#: inside a node name is a live colour run written into the caller's file, which is the
#: one thing "no colour, not a single escape sequence" exists to prevent. A BEL was worse:
#: silently dropped, so the printed name was not the advertised one.
_UNPRINTABLE = frozenset([*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0), 0x2028, 0x2029])


def _escaped(text: str) -> str:
    r"""``text`` with everything that cannot travel inside one record folded into an escape.

    A record is a line and a field is a run of printable cells; this is what makes both
    true of a value that arrived over the air. Named escapes where one reads (``\n``),
    numeric escapes everywhere else, and the backslash doubled so the whole thing reads
    back unambiguously.
    """
    out: list[str] = []
    for ch in text:
        if ch in _BREAKS:
            out.append(_BREAKS[ch])
        elif ord(ch) in _UNPRINTABLE:
            out.append(f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def quote(value: str | None) -> str:
    r"""Wrap a name in doublequotes, escaping the ones inside it.

    A node name is user-supplied text that can hold a space, a comma, or a quote, so it
    is never a field on its own — quoting is what makes it one. The escaping is the
    ordinary backslash convention and covers the backslash and the line breaks too, so the
    value reads back unambiguously and cannot end the record it sits in.

    Args:
        value: The name to quote. ``None`` and the empty string both give ``""``.

    Returns:
        The quoted name, e.g. ``"Yagi-Repeater"`` or ``"He said \"hi\""``.
    """
    return '"' + _escaped(value or "").replace('"', '\\"') + '"'


def name(value: str | None) -> str:
    r"""A node's name as a field: quoted when there is one, :data:`NONE` when there isn't.

    :func:`quote` renders a missing name as ``""``, which is a different claim — *this node
    is called nothing* rather than *this node never told us what it is called*. The CLI has
    one token for the second, and seven of the ten places that printed a name were making
    the first claim by accident, so the same unnamed node read as ``""`` in one listing and
    ``-`` in the next.

    It is also the distinction the JSON rendering needs: an absent name is ``null`` and an
    empty one is ``""``, and it cannot be recovered later from a field that flattened both.

    Args:
        value: The name, or ``None``/empty where the node never supplied one.

    Returns:
        The quoted name, or :data:`NONE`.
    """
    return quote(value) if value else NONE


def oneline(value: str | None) -> str:
    r"""``value`` as a field that can end a record without breaking it.

    For the one field a listing leaves unquoted because it *is* the rest of the line — a
    message body. "The rest of the line" and "anything at all" are not compatible: a body
    holding a newline ended its record early and left the remainder indented under the
    other columns, reading as a second record with an empty ``TIME``. A body is also the
    field a stranger fills in, so that is not a hypothetical shape of message.

    Args:
        value: The free text. ``None`` gives :data:`NONE`.

    Returns:
        The text with its line breaks and tabs escaped, and no quotes around it.
    """
    return NONE if value is None else _escaped(value)


def text(value: str | None) -> str:
    r"""Free text as a field that can end a record without breaking it.

    For the field a listing puts last because it *is* the rest of the line — a message
    body, a preference's description. "The rest of the line" and "anything at all" are not
    compatible: a body holding a newline ended its record early and left the remainder
    indented under the other columns, reading as a second record with an empty ``TIME``.
    A body is also the field a stranger fills in, so that is not a hypothetical shape of
    message.

    Args:
        value: The free text. ``None`` gives :data:`NONE`.

    Returns:
        The text with its line breaks and tabs escaped.
    """
    return NONE if value is None else _escaped(value)


def stamp(when: datetime | None) -> str:
    """Format a timestamp as local ISO-8601 to the second, or :data:`NONE`.

    Kept for the times where the instant *is* the fact rather than a way of saying how
    long ago something was: the device's own clock, an appointment set with ``--at``, a
    live capture's per-row time (every row of which would otherwise read ``now``), and
    every column under ``--absolute``.

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


def age(when: datetime | None, *, absent: str = NONE) -> str:
    """How long ago ``when`` was: ``now``, ``5m``, ``3h``, ``2d``, ``4w``.

    The default time form, and the reason is the premise change: a person at a prompt
    reading ``2026-09-07T19:58:53-04:00`` is doing arithmetic to answer "recently?", which
    is the question they typed the command to ask. Delegates to
    :func:`~meshterm.ui.widgets._format_age` so the two faces cannot drift apart.

    Args:
        when: The instant to age. A naive datetime has no offset we know, so it reads as
            absent, exactly as :func:`stamp` treats it.
        absent: What an unknown time reads as. A heard-age passes ``"never"`` — a node
            that has never been heard is a fact, not a missing field — and everything else
            takes the default token, which says this row has no such time at all. The same
            distinction :func:`name` draws between ``-`` and an empty name, one axis over.

    Returns:
        The age, or ``absent``.
    """
    from .widgets import _age_seconds, _format_age

    seconds = _age_seconds(when)
    return absent if seconds is None else _format_age(seconds)


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


#: The units :func:`duration` counts in, largest first.
_UNITS: tuple[tuple[int, str], ...] = ((86400, "d"), (3600, "h"), (60, "m"), (1, "s"))


def duration(seconds: float | None) -> str:
    """A span of seconds as something readable: ``93784`` → ``1d 2h``, ``360`` → ``6m``.

    Two units at most and always adjacent, so the magnitude arrives at a glance and the
    precision never outruns what anyone would act on. Used only as a *gloss* beside the
    raw figure (``uptime_s  93784  (1d 2h)``), never in place of it — the key says what
    the number counts, and a caller reading the key must still find a number under it.

    Args:
        seconds: The span. ``None`` reads as absent; the sign is dropped, since a caller
            glossing a signed reading says which way in its own words.

    Returns:
        The span in one or two units, or :data:`NONE`.
    """
    if seconds is None:
        return NONE
    whole = int(abs(seconds))
    amounts: list[tuple[int, str]] = []
    for size, unit in _UNITS:
        amounts.append((whole // size, unit))
        whole %= size
    lead = next((i for i, (amount, _) in enumerate(amounts) if amount), len(amounts) - 1)
    parts = [f"{amounts[lead][0]}{amounts[lead][1]}"]
    if lead + 1 < len(amounts) and amounts[lead + 1][0]:
        parts.append(f"{amounts[lead + 1][0]}{amounts[lead + 1][1]}")
    return " ".join(parts)


def location(lat: float | None, lon: float | None) -> str:
    """A shared position as one field: ``45.50190,-73.56740``, or :data:`NONE`.

    One column rather than two. A coordinate pair is one fact to a reader, it is what goes
    into a map's search box verbatim, and two columns of ``-`` for every contact that has
    never shared a position is worse than one.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        The pair to five decimals, or :data:`NONE` when either half is missing.
    """
    if lat is None or lon is None:
        return NONE
    return f"{lat:.5f},{lon:.5f}"


def route(hops: Iterable[tuple[str | None, str | None]]) -> str:
    """A walked hop sequence, drawn: ``Yagi-Repeater (a1) → Alice (d4) → Yagi (a1)``.

    A *route* is what a walk actually did, so it is a picture and not a spec — which is
    why it takes :data:`ARROW` and not the comma ``--path`` accepts. Rendering it with
    commas made it look pasteable, which it is not: the names are in it, and it is a
    round trip nothing has.

    The hash rides on every hop and always. It is the join key to the per-hop table below
    a trace, the token ``--path`` takes, and the disambiguator for two contacts sharing a
    name. Dropping it "where the name is unique" would make one line's grammar depend on
    another line's content.

    Our own node is a hop like any other — named and hashed. The menu draws it as ``★``
    because a reader never has to be told which node is theirs; here the line is as often
    read out of a file by someone who was not at the prompt when it ran.

    Args:
        hops: ``(label, hash)`` pairs in propagation order. A hop with both halves reads
            ``Name (hash)``; a hop with one half is that half alone, never an empty
            ``()`` — which is how the menu's :class:`~meshterm.ui.pathline.PathLine`
            labels an unresolved hop too.

    Returns:
        The joined route, or :data:`NONE` when there are no hops.
    """
    parts: list[str] = []
    for label, value in hops:
        if label and value:
            parts.append(f"{_escaped(label)} ({value})")
        else:
            parts.append(_escaped(label) if label else (value or NONE))
    return ARROW.join(parts) if parts else NONE


def spec(hops: Iterable[str]) -> str:
    """A forced path as the radio was given it: ``a1,d4,a1``.

    The other half of the split :func:`route` names. This is the *spec* — comma-separated
    hashes, exactly what ``--path`` takes back — so it is the one line on either face that
    round-trips, and it must never grow a name, an arrow or a space.

    Args:
        hops: The hop hashes in propagation order.

    Returns:
        The comma-joined spec, or :data:`NONE` when there are no hops.
    """
    parts = [hop for hop in hops if hop]
    return ",".join(parts) if parts else NONE


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


def columns(*headers: str, right: Sequence[str] = ()) -> Table:
    """Build the scripted table: an uppercase header line, then padded records.

    The shape ``ps`` and ``df`` print — no box, no title, no edge padding, every column
    sized to its widest value and separated by :data:`GUTTER` spaces, nothing wrapped and
    nothing elided. Numeric lanes right-align, which is what makes a column of magnitudes
    comparable by eye without changing what splitting it yields; an *age* lane aligns the
    same way, so the ladder from ``now`` to ``4w`` reads down the column.

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
    ``trace``) — the ``sysctl -a`` shape. Keys are the ones the matching ``get``/``set``
    subcommand takes, so a line read out of ``show`` can be typed back in.

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


@dataclass(frozen=True, slots=True)
class Lanes:
    """A fixed-width column layout for output whose widths arrive with the data.

    :func:`columns` sizes each lane to its widest value, which it can only do once it has
    seen every record. A live capture has no "once": ``monitor`` prints a row the instant
    a packet lands, and the row after it may be twice as wide. Those two streams used to
    be gutter-joined with no alignment at all, which is why a name in one still carried
    its quotes — nothing else said where the field ended.

    Pinning the lanes up front fixes it properly. A value wider than its lane **overruns
    and pushes the rest of that row right** rather than being elided: one row out of many
    is simply wider, and nothing lies about what it holds. Put the one unbounded field
    (a node's name, a message body) last and it cannot push anything at all.

    Attributes:
        widths: ``(header, width)`` per lane, in order.
        right: The headers whose cells right-align.
    """

    widths: tuple[tuple[str, int], ...]
    right: frozenset[str] = frozenset()

    @property
    def header(self) -> str:
        """The one header line, printed before the first record."""
        return self.record(*(header for header, _ in self.widths))

    def record(self, *cells: str) -> str:
        """One record, padded into the lanes and joined by the gutter."""
        out: list[str] = []
        for (header, width), cell in zip(self.widths, cells, strict=True):
            out.append(cell.rjust(width) if header in self.right else cell.ljust(width))
        return (" " * GUTTER).join(out).rstrip()


def stream(*widths: tuple[str, int], right: Sequence[str] = ()) -> Lanes:
    """Build a fixed-lane layout for a live stream (see :class:`Lanes`).

    Args:
        *widths: ``(header, width)`` per lane, in order.
        right: The headings whose values right-align.

    Returns:
        The layout, which prints its own header and formats each record.
    """
    return Lanes(widths=widths, right=frozenset(right))


def wrap(body: str, width: int = PAGE_WIDTH) -> str:
    """Fold a written page's paragraphs to ``width``, leaving its structure alone.

    The deliberate exception to "no wrapping", and the reason the rule exists says why:
    wrapping splits a *record* and lands half its fields under the wrong headings. A
    written page has no records and no headings — and left unwrapped, ``meshterm about``
    prints paragraphs six hundred cells long that no terminal can read.

    A line's own indent is kept and carried onto its continuations, so a bullet stays a
    bullet and an indented block stays indented; a blank line stays blank; a line that is
    already short is untouched.

    Args:
        body: The page, as its plain renderer laid it out.
        width: The cell width to fold to.

    Returns:
        The page with every over-long line folded under its own indent.
    """
    import textwrap

    out: list[str] = []
    for line in body.splitlines():
        stripped = line.lstrip()
        if not stripped:
            out.append("")
            continue
        indent = line[: len(line) - len(stripped)]
        # A bullet's continuations hang under its text, not under its marker.
        hanging = indent + ("  " if stripped[:1] in "-*•" else "")
        out.extend(
            textwrap.wrap(
                stripped,
                width=max(width - len(indent), 20),
                initial_indent=indent,
                subsequent_indent=hanging,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [indent + stripped]
        )
    return "\n".join(out)


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
    log line, an acknowledgement and an error message all go here instead — visible in a
    terminal, absent from ``meshterm contacts > contacts.txt``. This one keeps its colour,
    because a terminal is usually the only thing that reads it.

    **Off a terminal it stops wrapping**, and that is the point of the width below. Rich
    falls back to 80 cells when it cannot measure the destination, so ``2> errors.log``
    used to hard-wrap every message at 80 — and ``grep 'not already connected'`` then
    found nothing, because the sentence it was looking for had been broken across three
    lines by the logger. In a real terminal the width is left alone: a progress bar sized
    to 16384 cells is not a progress bar.

    Returns:
        A themed :class:`~rich.console.Console` writing to ``sys.stderr``.
    """
    import sys

    from .theme import active_theme

    try:
        wraps = bool(sys.stderr.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - a closed or exotic stream
        wraps = False
    return Console(
        file=sys.stderr,
        theme=active_theme(),
        markup=False,
        emoji=False,
        width=None if wraps else WIDTH,
    )


# -- the safety net ------------------------------------------------------------------


def flatten(renderable: RenderableType) -> list[RenderableType]:
    """Strip framing from anything reaching the scripted console still wearing it.

    Every CLI surface is *written* plain — a tool states its answer as a
    :mod:`~meshterm.ui.report` and the plain renderer builds it through :func:`columns`
    and :func:`pairs`. This is the net under that, for a renderable shared with the menu
    that still arrives boxed: a :class:`Panel` gives up its border and title and yields
    its body, a :class:`Table` gives up its box, title and expansion, and a :class:`Group`
    is flattened member by member.

    It is deliberately not a *design*: a screen's table has the menu's columns, not the
    CLI's, so passing it through here makes it printable, not right.

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
