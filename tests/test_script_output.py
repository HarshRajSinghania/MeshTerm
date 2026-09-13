# SPDX-License-Identifier: Apache-2.0
"""Tests for the plain CLI's output language (:mod:`meshterm.ui.script`).

The plain face is read by a person at a prompt — ``--json`` carries the machine now — and
every rule that makes it legible is testable: no colour, no wrapping, no framing, bare
names that still cannot break a record, relative ages, arrows for a route and commas for a
path, one token for absent, one documented exit status per outcome. These are the
enforcement point for :mod:`meshterm.ui.script` the way ``test_gallery`` is for the
screens, and :mod:`tests.test_report` is for the seam above them.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from meshterm.core import exitcodes
from meshterm.ui import script

#: Any ANSI escape sequence. Nothing the scripted console emits may match this.
_ANSI = re.compile(r"\x1b\[")


def _rendered(*renderables: object) -> str:
    """Print ``renderables`` through the scripted console and return what came out."""
    buffer = io.StringIO()
    console = script.console(buffer)
    for renderable in renderables:
        console.print(renderable)
    console.file.flush()
    return buffer.getvalue()


# -- names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Yagi-Repeater", "Yagi-Repeater"),
        ("YUL Cartierville", "YUL Cartierville"),
        ("Node, Inc", "Node, Inc"),
        ('He said "hi"', 'He said "hi"'),
        ("back\\slash", "back\\\\slash"),
    ],
)
def test_a_name_is_bare_because_alignment_is_the_delimiter(value: str, expected: str) -> None:
    """The quotes are retired; a column that lines up is already one field to the eye.

    They existed so a name holding a space or a comma stayed one field for whatever was
    splitting the line. The splitter reads JSON now, and in plain text the padding says
    where a field ends — so a reader gets back the six cells the quotes were spending on
    every row, and a name reads as the name the node broadcast.
    """
    assert script.name(value) == expected


def test_a_name_still_cannot_end_the_record_it_sits_in() -> None:
    """The escaping under the quoting stays, and it is the half that was load-bearing.

    A node broadcasts its own name, so every name here is remote data. Nothing about
    alignment stops a newline, a tab or an ESC in one from ending the record early — and
    an ESC inside a name is a live colour run written into the caller's file, which is the
    one thing "not a single escape sequence" exists to prevent.
    """
    assert script.name("two\nlines") == "two\\nlines"
    assert script.name("tabbed\there") == "tabbed\\there"
    assert script.name("bell\a") == "bell\\x07"
    assert "\x1b" not in script.name("esc\x1b[31m")


@pytest.mark.parametrize(
    "body,expected",
    [
        ("line one\nline two", "line one\\nline two"),
        ("tabbed\there", "tabbed\\there"),
        ("carriage\rreturn", "carriage\\rreturn"),
        ("back\\slash", "back\\\\slash"),
        ('a "quoted" body', 'a "quoted" body'),
        ("plain", "plain"),
    ],
)
def test_a_message_body_can_never_end_its_own_record(body: str, expected: str) -> None:
    """``TEXT`` is the rest of the line, and "the rest of the line" has to stay one line.

    A body holding a newline used to end its record early and leave the remainder indented
    under the other columns, reading as a second record with an empty ``TIME``. The body is
    also the one field a stranger fills in, so this is not a hypothetical message. Quotes
    inside it are left alone: nothing wraps it, so nothing needs escaping from.
    """
    assert script.text(body) == expected
    assert "\n" not in script.text(body)


@pytest.mark.parametrize("absent", [None, ""])
def test_a_node_that_never_gave_a_name_reads_as_absent_not_as_empty(absent: str | None) -> None:
    """``""`` says "called nothing"; ``-`` says "never said". They are different facts.

    Seven of the ten places that printed a name made the first claim by accident, so the
    same unnamed node read as ``""`` in one listing and ``-`` in the next. One helper now
    answers it, and the distinction is the one the machine face needs too — an absent name
    is ``null``, an empty one is ``""``, and a field that flattened both cannot say which
    it meant.
    """
    assert script.name(absent) == script.NONE
    assert script.name("Alice") == "Alice"


# -- timestamps ----------------------------------------------------------------------


def test_stamp_is_absolute_local_iso_to_the_second() -> None:
    """A time is printed as something a script can sort and subtract."""
    when = datetime(2026, 9, 7, 22, 22, 41, tzinfo=timezone.utc)
    text = script.stamp(when)
    assert datetime.fromisoformat(text) == when
    assert text.endswith(when.astimezone().strftime("%z")[:3] + ":00")


def test_stamp_reads_an_unknown_offset_as_absent() -> None:
    """A naive datetime is a time whose offset we don't know, which is not a fact.

    The same rule :func:`~meshterm.ui.widgets.age_seconds` applies to relative ages.
    """
    assert script.stamp(None) == script.NONE
    assert script.stamp(datetime(2026, 9, 7, 22, 22, 41)) == script.NONE


def test_a_time_is_an_age_unless_the_instant_is_itself_the_fact() -> None:
    """The rule that reversed, and the reason it reversed.

    A person at a prompt reading ``2026-09-07T19:58:53-04:00`` is doing arithmetic to
    answer "recently?", which is the question they typed the command to ask. So an age is
    the default now. :func:`~meshterm.ui.script.stamp` survives for the three places the
    instant *is* the answer — the device clock, an appointment set with ``--at``, and a
    live capture's own ``TIME`` column, every row of which would otherwise read ``now`` —
    and for every column under ``--absolute``.
    """
    fresh = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert script.age(fresh) == "now"
    assert script.stamp(fresh)[:4].isdigit()


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "now"),
        (59, "now"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86400, "1d"),
        (604800, "1w"),
    ],
)
def test_the_age_ladder_steps_where_a_person_would_step(seconds: int, expected: str) -> None:
    """``now``, ``5m``, ``3h``, ``2d``, ``4w`` — the menu's own column ladder.

    Delegated to :func:`~meshterm.ui.widgets.format_age` rather than re-derived, so the
    two faces of the same age cannot drift apart.
    """
    assert script.age(datetime.now(timezone.utc) - timedelta(seconds=seconds)) == expected


def test_an_age_reads_an_unknown_offset_as_absent_exactly_as_a_stamp_does() -> None:
    """A naive datetime is a time whose offset we don't know, which is not a fact."""
    assert script.age(None) == script.NONE
    assert script.age(datetime(2026, 9, 7, 22, 22, 41)) == script.NONE


def test_never_is_a_value_and_the_absent_token_is_not() -> None:
    """A node that has never been heard is a *fact*; a row with no such time is not.

    Flattening both onto ``-`` made "we have not heard from it" and "this kind of row has
    no heard time" read the same, which is the distinction the column exists to draw.
    """
    assert script.age(None, absent="never") == "never"
    assert script.age(None) == script.NONE


@pytest.mark.parametrize(
    "seconds,expected",
    [(42, "42s"), (360, "6m"), (125, "2m 5s"), (93784, "1d 2h"), (3600, "1h"), (-125, "2m 5s")],
)
def test_a_duration_reads_in_two_units_at_most(seconds: int, expected: str) -> None:
    """``93784`` is a number nobody can hold in their head; ``1d 2h`` is the same fact.

    Two adjacent units, largest first, so the magnitude arrives at a glance and the
    precision never outruns what anyone would act on. Only ever a gloss beside the raw
    figure — the key says what the number counts, and a caller reading the key must still
    find a number under it.
    """
    assert script.duration(seconds) == expected


def test_a_location_is_one_field_because_it_is_one_fact() -> None:
    """A coordinate pair is what goes into a map's search box, verbatim.

    Two columns would also mean two ``-`` for every node that has never shared a position,
    which is worse than one.
    """
    assert script.location(45.50190, -73.56740) == "45.50190,-73.56740"
    assert script.location(45.5, None) == script.NONE
    assert script.location(None, None) == script.NONE


# -- the absent token -----------------------------------------------------------------


def test_one_token_stands_for_every_kind_of_absence() -> None:
    """A reader learns ``-`` once, and never meets an em dash, an ``n/a`` or a blank.

    ``never`` is the one word that is *not* an absence: it is a value, and the age lane is
    where it is spelled (see the ladder tests above).
    """
    assert script.NONE == "-"
    assert script.number(None) == script.NONE
    assert script.route([]) == script.NONE
    assert script.spec([]) == script.NONE


def test_number_formats_with_its_spec_when_there_is_one() -> None:
    """A present number is formatted; only an absent one becomes the token."""
    assert script.number(2.64, "+.1f") == "+2.6"
    assert script.number(-1.5, "+.1f") == "-1.5"
    assert script.number(7) == "7"


# -- routes and paths ------------------------------------------------------------------


def test_a_route_is_drawn_with_arrows_and_a_path_keeps_its_commas() -> None:
    """The lexicon's own split, finally drawn: a *route* is walked, a *path* is typed.

    Both used to be comma-separated, which made a route look like something you could
    paste back into ``--path``. You cannot — the names are in it — so that comma was
    promising a round trip only the spec has. The arrow says "this is a picture", and
    carries the direction the packet travelled while it is there.
    """
    assert (
        script.route([("Alice", "3d"), ("Yagi-Repeater", "f2")])
        == "Alice (3d) → Yagi-Repeater (f2)"
    )
    assert script.spec(["a1", "d4", "a1"]) == "a1,d4,a1"


def test_a_route_names_our_own_node_like_any_other_hop() -> None:
    """No star, and the reason is who reads the line.

    The menu draws our own node as ``★`` because a reader never has to be told which node
    is theirs. A route line is as often read out of a file, by somebody who was not at the
    prompt when it ran — and by then the star names nothing.
    """
    line = script.route([("MockCompanion", "00"), ("Alice", "3d"), ("MockCompanion", "00")])
    assert "★" not in line
    assert line.startswith("MockCompanion (00)")
    assert line.endswith("MockCompanion (00)")


def test_a_route_hop_never_shows_an_empty_pair_of_parentheses() -> None:
    """Half an identity prints as the half there is — a name alone, or a hash alone.

    Which is how the menu's own :class:`~meshterm.ui.pathline.PathLine` labels an
    unresolved hop, and the reason both halves are optional: history often knows one.
    """
    assert script.route([("Alice", None)]) == "Alice"
    assert script.route([(None, "3d")]) == "3d"
    assert script.route([(None, None)]) == script.NONE


def test_the_arrow_is_what_makes_the_quoting_unnecessary() -> None:
    """The claim moved: the quotes made a comma safe, and now nothing has to.

    A comma inside a node's name can no longer be read as a hop boundary, because the hop
    boundary is not a comma any more.
    """
    line = script.route([("Node, Inc", "3d"), ("Bob", "f2")])
    assert line == "Node, Inc (3d) → Bob (f2)"
    assert line.count("→") == 1


def test_a_spec_is_the_one_line_on_either_face_that_round_trips() -> None:
    """It is what ``--path`` takes back, so it grows no name, no arrow and no space."""
    assert script.spec(["a1b2", "d4e5", "a1b2"]) == "a1b2,d4e5,a1b2"
    assert " " not in script.spec(["a1", "d4"])


# -- live streams ----------------------------------------------------------------------


def test_a_stream_pins_its_lanes_because_it_cannot_measure_them() -> None:
    """A live capture cannot size its columns, because it has not seen the records yet.

    :func:`~meshterm.ui.script.columns` sizes each lane to its widest value once every
    record is in. ``monitor`` prints a row the instant a packet lands, and the row after it
    may be twice as wide — so those two streams were gutter-joined with no alignment at
    all, which is the last thing the quoting was holding up.
    """
    lanes = script.stream(("TIME", 19), ("NODE", 8), ("SNR_DB", 6), right=("SNR_DB",))
    assert lanes.header == "TIME                 NODE      SNR_DB"
    assert lanes.record("2026-09-08T02:25:45", "a1b2c3d4", "+7.0") == (
        "2026-09-08T02:25:45  a1b2c3d4    +7.0"
    )


def test_a_streamed_value_wider_than_its_lane_overruns_rather_than_lying() -> None:
    """One row out of many is simply wider, and nothing is elided.

    Which is why the one unbounded field — a node's name, a message body — goes last: it
    can then push nothing at all.
    """
    lanes = script.stream(("NODE", 8), ("NAME", 6))
    assert lanes.record("a1", "A Very Long Node Name") == "a1        A Very Long Node Name"


# -- the console ----------------------------------------------------------------------


def test_the_scripted_console_emits_no_escape_sequence_at_all() -> None:
    """Not a colour, not a bold, not a dim — ``stdout`` is a pipe more often than a screen."""
    loud = Text.assemble(
        ("brand", "brand"), ("bold", "bold"), ("hex", "#ff8800"), ("reverse", "reverse")
    )
    assert not _ANSI.search(_rendered(loud))


@pytest.mark.parametrize(
    "name",
    ["[bold]Loud", "[/]Bob", "[red]a[/red]", ":fire:Hot", "[[weird]]", "100% [done]"],
)
def test_a_node_name_is_never_read_as_rich_markup(name: str) -> None:
    """A node broadcasts its own name, so every name printed here is remote data.

    Rich reads ``[...]`` as a style tag unless told not to. That made a name into two
    different bugs: ``[bold]Loud`` printed as ``Loud`` — silently corrupted, and no longer
    the string that identifies the node to whoever reads the output — and ``[/]Bob`` raised
    ``MarkupError`` and took the whole command down. ``:fire:`` is the same hazard one
    parser over. Neither is hypothetical: a name is the one field a stranger controls.
    """
    table = script.columns("NAME", "TYPE")
    table.add_row(script.name(name), "node")
    record = _rendered(table).splitlines()[1]
    assert record.startswith(script.name(name)), record


def test_the_scripted_console_never_wraps_a_record_onto_a_second_line() -> None:
    """A record is a line: wrapping would put half its fields under the wrong headings."""
    table = script.columns("KEY", "VALUE")
    table.add_row("basemap_tilejson_url", "x" * 400)
    lines = _rendered(table).splitlines()
    assert len(lines) == 2  # the header and the one record
    assert lines[1].endswith("x" * 400)


def test_the_scripted_console_leaves_no_trailing_whitespace() -> None:
    """Padding the last column to its width would put invisible spaces on every line."""
    table = script.columns("NAME", "TYPE")
    table.add_row(script.name("a-long-name"), "node")
    table.add_row(script.name("b"), "repeater")
    for line in _rendered(table).splitlines():
        assert line == line.rstrip(), repr(line)


def test_the_gutter_still_separates_columns_after_the_trimming() -> None:
    """The trimmer holds trailing whitespace back; it must not eat the padding *between*.

    Rich writes a rendered row segment by segment and flushes after each one, so a
    wrapper that trimmed at flush would throw away the very spaces that make the columns
    columns (see :class:`meshterm.ui.script._Trimmed`).
    """
    table = script.columns("NAME", "TYPE", "PKTS", right=("PKTS",))
    table.add_row(script.name("Alice"), "node", "12")
    header, record = _rendered(table).splitlines()
    assert header.split() == ["NAME", "TYPE", "PKTS"]
    assert record.split() == ["Alice", "node", "12"]
    # Spelled out, not written as `" " * script.GUTTER`: an assertion that reads the
    # constant it is checking narrows to `" " in record` when the gutter narrows, which
    # is exactly the state the gutter exists to prevent.
    assert "Alice  node" in record


def test_columns_align_so_every_record_splits_the_same_way() -> None:
    """Every field lands in its heading's column, whatever the widths."""
    table = script.columns("NAME", "TYPE")
    table.add_row(script.name("Yagi-Repeater"), "repeater")
    table.add_row(script.name("Al"), "node")
    header, *records = _rendered(table).splitlines()
    starts = {line.index("re") if "repeater" in line else line.index("node") for line in records}
    assert starts == {header.index("TYPE")}  # every TYPE value opens under its heading


def test_a_numeric_column_really_right_aligns() -> None:
    """A column of magnitudes is only comparable by eye if the digits line up.

    Rich's ``Text.wrap`` returns early on ``overflow="ignore"`` — and that early return
    sits *above* the justify step, so asking for ``overflow="ignore"`` throws
    ``justify="right"`` away without a word. Every ``right=`` in the CLI was a no-op
    until the columns asked for ``"crop"``, which refuses to elide just as firmly and
    still aligns. Checking where a field *starts* cannot see this; only its end can.
    """
    table = script.columns("NAME", "PKTS", right=("PKTS",))
    table.add_row(script.name("Alice"), "1174")
    table.add_row(script.name("Bob"), "3")
    header, *records = _rendered(table).splitlines()
    assert len({len(line) for line in records}) == 1, records
    assert {len(line) for line in records} == {len(header)}


def test_no_column_elides_however_long_its_value_runs() -> None:
    """A key cut short is not something a caller can hand back to ``--to`` or ``--path``.

    The guard against eliding and the guard against mis-alignment are the same setting,
    so a change to one silently moves the other; both are pinned here.
    """
    table = script.columns("KEY", "PKTS", right=("PKTS",))
    table.add_row("d4" * 32, "7")
    out = _rendered(table)
    assert "…" not in out
    assert "d4" * 32 in out


def test_the_trimmer_keeps_the_gutter_when_the_stream_flushes_between_columns() -> None:
    """Rich's legacy-Windows renderer writes a row one segment at a time, flushing each.

    ``LegacyWindowsTerm.write_text`` is ``write(text); flush()``, so on that path a flush
    lands *between two columns* as often as at the end of a line. A wrapper that emptied
    its held run at flush destroyed every gutter in real use while the in-process tests —
    which see one write and one flush per ``print`` — went on passing. That is not a
    hypothetical: it shipped, and this drives the stream the way that renderer does.
    """
    buffer = io.StringIO()
    stream = script._Trimmed(buffer)
    for segment in ("Alice", "  ", "node", "  ", "12", "\n"):
        stream.write(segment)
        stream.flush()
    assert buffer.getvalue() == "Alice  node  12\n"


def test_the_trimmer_still_drops_the_run_that_ends_a_line() -> None:
    """The other half of the rule: holding padding back is only right if it is still cut.

    Held whitespace is emitted when something follows it on the line, and dropped at the
    newline and at the end of the stream — otherwise every record would end in the
    invisible padding of its last column.
    """
    buffer = io.StringIO()
    stream = script._Trimmed(buffer)
    for segment in ("row", "    ", "\n", "next", "   "):
        stream.write(segment)
        stream.flush()
    assert buffer.getvalue() == "row\nnext"


def test_a_value_wider_than_the_console_is_cut_rather_than_elided() -> None:
    """Past :data:`~meshterm.ui.script.WIDTH` cells something gives; it must not be a "…".

    An elision is indistinguishable from a value that really ends there, so a key read out
    of a listing would go back into ``--to`` subtly wrong. A 64-hex key never reaches the
    overflow path at all, so only an over-width value tests the guard that stands there.
    """
    table = script.columns("KEY")
    table.add_row("d" * (script.WIDTH + 500))
    assert "…" not in _rendered(table)


def test_an_error_stays_greppable_when_stderr_is_not_a_terminal() -> None:
    """A sentence broken across three lines is a sentence nothing can find.

    Rich falls back to 80 cells when it cannot measure the destination, so
    ``2> errors.log`` hard-wrapped every message — and ``grep 'not already connected'``
    then found nothing, because the words it was looking for had a newline in the middle
    of them. In a real terminal the width is left alone: a progress bar sized to 16384
    cells is not a progress bar.
    """
    import sys

    buffer = io.StringIO()
    stderr, sys.stderr = sys.stderr, buffer
    try:
        console = script.stderr_console()
        console.print("meshterm: " + "the connection was not already connected " * 6)
    finally:
        sys.stderr = stderr
    assert len(buffer.getvalue().splitlines()) == 1


# -- framing --------------------------------------------------------------------------


def test_a_panel_gives_up_its_border_and_its_title() -> None:
    """No frames: the command the reader typed is the title."""
    out = _rendered(*script.flatten(Panel(Text("body"), title="Trace summary")))
    assert out.strip() == "body"


def test_a_table_gives_up_its_box_its_title_and_its_expansion() -> None:
    """A shared table that still arrives boxed is unframed on the way out."""
    from rich import box

    table = Table(title="Contacts", box=box.SIMPLE_HEAD, expand=True)
    table.add_column("NAME")
    table.add_row("Alice")
    out = _rendered(*script.flatten(table))
    assert "Contacts" not in out
    assert not any(ch in out for ch in "─│┌└")


def test_a_group_is_unframed_member_by_member() -> None:
    """Framing is stripped however deep a shared renderable nests it."""
    nested = Group(Panel(Text("one"), title="First"), Panel(Text("two"), title="Second"))
    assert _rendered(*script.flatten(nested)).split() == ["one", "two"]


def test_no_rendered_line_is_a_box_drawing_rule() -> None:
    """No borders and no header rules — a listing is its header line and its records."""
    table = script.columns("NAME", "TYPE")
    table.add_row(script.name("Alice"), "node")
    assert "─" not in _rendered(table)


# -- key/value listings ---------------------------------------------------------------


def test_pairs_print_one_fact_per_line_with_no_header() -> None:
    """The ``sysctl -a`` shape: a key, the gutter, and the rest of the line as its value."""
    out = _rendered(script.pairs([("name", "MockCompanion"), ("uptime_s", "93784")]))
    assert [line.split(None, 1) for line in out.splitlines()] == [
        ["name", "MockCompanion"],
        ["uptime_s", "93784"],
    ]


def test_a_pairs_value_is_unquoted_because_it_is_the_rest_of_the_line() -> None:
    """Quoting is for a name sharing a line with other fields; here there are only two."""
    out = _rendered(script.pairs([("name", "YUL Cartierville")]))
    assert out.splitlines()[0].split(None, 1) == ["name", "YUL Cartierville"]


# -- exit statuses --------------------------------------------------------------------


def test_the_exit_statuses_are_the_numbers_a_caller_hard_codes() -> None:
    """``case $? in 3)`` is written against the digits, so the digits are the contract.

    Stated as literals on purpose. Asserting ``set(MEANINGS) == {OK, FAILURE, …}``, or
    ``len(set(d)) == len(d)``, cannot fail however the constants are renumbered: a dict
    has no duplicate keys, so two statuses colliding *removes* a row and both sides of
    the comparison lose the same one together. Renumbering ``NO_DEVICE`` to ``4`` left
    that whole family of assertions green while status 3 vanished from ``--help``.
    """
    assert (exitcodes.OK, exitcodes.FAILURE, exitcodes.USAGE) == (0, 1, 2)
    assert (exitcodes.NO_DEVICE, exitcodes.DEVICE, exitcodes.NO_RESULT) == (3, 4, 5)
    assert sorted(exitcodes.MEANINGS) == [0, 1, 2, 3, 4, 5]
    assert all(meaning for meaning in exitcodes.MEANINGS.values())


def test_the_help_epilog_names_every_status() -> None:
    """A caller should not have to find the table in a README."""
    from meshterm.cli import EXIT_STATUS_EPILOG

    for code, meaning in exitcodes.MEANINGS.items():
        assert f"{code} {meaning}" in EXIT_STATUS_EPILOG
