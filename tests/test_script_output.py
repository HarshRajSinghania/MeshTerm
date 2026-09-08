"""Tests for the scripted CLI's output language (:mod:`meshterm.ui.script`).

The CLI is read by ``awk``, and every rule that makes that possible is testable: no
colour, no wrapping, no framing, quoted names, absolute times, one token for absent, one
documented exit status per outcome. These are the enforcement point for
:mod:`meshterm.ui.script` the way ``test_gallery`` is for the screens.
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


# -- quoting -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Yagi-Repeater", '"Yagi-Repeater"'),
        ("YUL Cartierville", '"YUL Cartierville"'),
        ("Node, Inc", '"Node, Inc"'),
        ('He said "hi"', '"He said \\"hi\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("", '""'),
        (None, '""'),
    ],
)
def test_quote_makes_any_name_exactly_one_field(value: str | None, expected: str) -> None:
    """A name is quoted and its quotes and backslashes escaped, whatever it holds.

    This is what lets a listing be split: an unquoted ``YUL Cartierville`` is two fields,
    and an unquoted ``Node, Inc`` is two hops of a path line.
    """
    assert script.quote(value) == expected


def test_a_quoted_name_survives_a_round_trip_through_the_escaping() -> None:
    """The escaping is the ordinary backslash convention, so the value reads back."""
    for name in ('a "quoted" name', "back\\slash", 'both\\"kinds'):
        body = script.quote(name)[1:-1]
        assert body.replace('\\"', '"').replace("\\\\", "\\") == name


# -- timestamps ----------------------------------------------------------------------


def test_stamp_is_absolute_local_iso_to_the_second() -> None:
    """A time is printed as something a script can sort and subtract."""
    when = datetime(2026, 9, 7, 22, 22, 41, tzinfo=timezone.utc)
    text = script.stamp(when)
    assert datetime.fromisoformat(text) == when
    assert text.endswith(when.astimezone().strftime("%z")[:3] + ":00")


def test_stamp_reads_an_unknown_offset_as_absent() -> None:
    """A naive datetime is a time whose offset we don't know, which is not a fact.

    The same rule :func:`~meshterm.ui.widgets._age_seconds` applies to relative ages.
    """
    assert script.stamp(None) == script.NONE
    assert script.stamp(datetime(2026, 9, 7, 22, 22, 41)) == script.NONE


def test_no_relative_age_ever_reaches_the_scripted_output() -> None:
    """``3h`` is for a screen; the CLI's stamp is absolute even for a fresh time."""
    assert script.stamp(datetime.now(timezone.utc) - timedelta(seconds=5)) != "now"


# -- the absent token -----------------------------------------------------------------


def test_one_token_stands_for_every_kind_of_absence() -> None:
    """A reader (and a parser) learns ``-`` once, and never meets an em dash or an n/a."""
    assert script.NONE == "-"
    assert script.number(None) == script.NONE
    assert script.path([]) == script.NONE


def test_number_formats_with_its_spec_when_there_is_one() -> None:
    """A present number is formatted; only an absent one becomes the token."""
    assert script.number(2.64, "+.1f") == "+2.6"
    assert script.number(-1.5, "+.1f") == "-1.5"
    assert script.number(7) == "7"


# -- path lines -----------------------------------------------------------------------


def test_a_path_line_quotes_each_node_and_hangs_its_hash_outside_the_quotes() -> None:
    """The CLI's path shape: ``"Name" (hash)``, hops joined by a bare comma."""
    line = script.path([("Alice", "3d"), ("Yagi-Repeater", "f2")])
    assert line == '"Alice" (3d),"Yagi-Repeater" (f2)'


def test_a_path_line_names_our_own_node_like_any_other() -> None:
    """No star: the glyph says "you already know this one", which is false of a parser."""
    line = script.path([("MockCompanion", "a1"), ("Alice", "3d"), ("MockCompanion", "a1")])
    assert "★" not in line
    assert line.startswith('"MockCompanion" (a1)')
    assert line.endswith('"MockCompanion" (a1)')


def test_a_path_line_prints_a_name_alone_when_no_hash_places_it() -> None:
    """An unidentified node has a name and nothing else; it does not get an empty ``()``."""
    assert script.path([("Alice", None)]) == '"Alice"'


def test_a_path_line_splits_even_when_a_name_holds_a_comma() -> None:
    """The quoting is what makes the bare comma separator safe."""
    line = script.path([("Node, Inc", "3d"), ("Bob", "f2")])
    assert line.count(",") == 2  # the one inside the name, and the one separating hops
    assert line == '"Node, Inc" (3d),"Bob" (f2)'


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
    table.add_row(script.quote(name), "node")
    record = _rendered(table).splitlines()[1]
    assert record.startswith(script.quote(name)), record


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
    table.add_row(script.quote("a-long-name"), "node")
    table.add_row(script.quote("b"), "repeater")
    for line in _rendered(table).splitlines():
        assert line == line.rstrip(), repr(line)


def test_the_gutter_still_separates_columns_after_the_trimming() -> None:
    """The trimmer holds trailing whitespace back; it must not eat the padding *between*.

    Rich writes a rendered row segment by segment and flushes after each one, so a
    wrapper that trimmed at flush would throw away the very spaces that make the columns
    columns (see :class:`meshterm.ui.script._Trimmed`).
    """
    table = script.columns("NAME", "TYPE", "PKTS", right=("PKTS",))
    table.add_row(script.quote("Alice"), "node", "12")
    header, record = _rendered(table).splitlines()
    assert header.split() == ["NAME", "TYPE", "PKTS"]
    assert record.split() == ['"Alice"', "node", "12"]
    assert " " * script.GUTTER in record


def test_columns_align_so_every_record_splits_the_same_way() -> None:
    """Every field lands in its heading's column, whatever the widths."""
    table = script.columns("NAME", "TYPE")
    table.add_row(script.quote("Yagi-Repeater"), "repeater")
    table.add_row(script.quote("Al"), "node")
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
    table.add_row(script.quote("Alice"), "1174")
    table.add_row(script.quote("Bob"), "3")
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
    table.add_row(script.quote("Alice"), "node")
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


def test_every_exit_code_is_documented() -> None:
    """A documented return value is half of what makes the CLI scriptable."""
    codes = {
        exitcodes.OK,
        exitcodes.FAILURE,
        exitcodes.USAGE,
        exitcodes.NO_DEVICE,
        exitcodes.DEVICE,
        exitcodes.NO_RESULT,
    }
    assert set(exitcodes.MEANINGS) == codes
    assert all(meaning for meaning in exitcodes.MEANINGS.values())


def test_the_codes_are_distinct_and_ok_is_zero() -> None:
    """The shell's own convention, and no two outcomes share a status."""
    assert exitcodes.OK == 0
    assert len(set(exitcodes.MEANINGS)) == len(exitcodes.MEANINGS)


def test_the_help_epilog_names_every_status() -> None:
    """A caller should not have to find the table in a README."""
    from meshterm.cli import EXIT_STATUS_EPILOG

    for code, meaning in exitcodes.MEANINGS.items():
        assert f"{code} {meaning}" in EXIT_STATUS_EPILOG
