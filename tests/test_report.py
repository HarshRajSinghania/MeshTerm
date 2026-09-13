# SPDX-License-Identifier: Apache-2.0
"""Tests for the seam between a tool's answer and its rendering.

:mod:`meshterm.ui.report` is where a tool states what it found, :mod:`meshterm.ui.fields`
is where the shapes it states it in are defined once, and :mod:`meshterm.ui.renderers` is
where that becomes bytes. The claim the whole design rests on is that **one typed row
projects two ways** — so almost every test here builds one report and asserts both faces
of it, because a rule that holds in only one of them is exactly the drift this replaced.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from meshterm.ui import fields, script
from meshterm.ui.renderers import JsonRenderer, OutputFormat, PlainRenderer, for_format
from meshterm.ui.report import BARE, SILENT, Column, Facts, Lane, Listing

_WHEN = datetime(2026, 9, 7, 22, 22, 41, tzinfo=timezone.utc)


def _plain(*blocks) -> str:  # noqa: ANN002 - blocks vary per test
    """Render a report through the plain face and return what came out."""
    buffer = io.StringIO()
    PlainRenderer(script.console(buffer)).render(blocks)
    buffer.flush()
    return buffer.getvalue()


def _json(*blocks):  # noqa: ANN002, ANN202 - blocks vary per test
    """Render a report through the machine face and return the parsed document."""
    buffer = io.StringIO()
    JsonRenderer(script.console(buffer)).render(blocks)
    lines = [line for line in buffer.getvalue().splitlines() if line]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


def _contacts(rows) -> Listing:  # noqa: ANN001 - rows vary per test
    """A listing in the shape ``contacts`` uses: a node, an age and a count."""
    return Listing(
        key="contacts",
        columns=(
            fields.node(
                "node",
                lanes=(("name", "NAME"), ("type", "TYPE"), ("hash", "HASH"), ("key", "KEY")),
            ),
            fields.when("heard_at", "HEARD", absent="never"),
            fields.integer("packets", "PKTS"),
        ),
        rows=rows,
        order=("NAME", "TYPE", "HEARD", "PKTS", "HASH", "KEY"),
    )


_ALICE = {
    "node": fields.NodeRef(name="Alice", key="d4" * 32, hash="d4", type="node"),
    "heard_at": _WHEN,
    "packets": 12,
}


# -- one row, two faces ----------------------------------------------------------------


def test_one_typed_row_reaches_both_faces_without_either_parsing_the_other() -> None:
    """The claim the seam exists for: a row holds the value, not somebody's rendering.

    The plain face writes ``+5.1`` because a column of readings only compares by eye once
    every one declares its sign; the document writes ``5.1`` because a number carries its
    own. Neither could be derived from the other without a parser, which is what the two
    hand-written ``if ctx.json_output:`` branches were.
    """
    block = Listing(key="hops", columns=(fields.snr("snr_db", "SNR_DB"),), rows=[{"snr_db": 5.14}])
    assert _plain(block).splitlines()[1].strip() == "+5.1"
    assert _json(block) == [{"snr_db": 5.1}]


def test_a_node_is_one_object_however_many_columns_it_is_worth() -> None:
    """One machine key, four plain lanes — the thing :func:`fields.node` earns.

    Every listing that mentions a node asks for the same column, so ``jq '.[].node.key'``
    reads the same on contacts, courier and monitor and a consumer's node handler is
    written once. What differs is only how much room the surface has for it.
    """
    header, record = _plain(_contacts([_ALICE])).splitlines()
    assert header.split() == ["NAME", "TYPE", "HEARD", "PKTS", "HASH", "KEY"]
    assert len(record.split()) == len(header.split())
    assert "Alice" in record and "d4" * 32 in record
    assert _json(_contacts([_ALICE]))[0]["node"] == {
        "name": "Alice",
        "key": "d4" * 32,
        "hash": "d4",
        "type": "node",
        "self": False,
    }


def test_a_listing_may_draw_its_lanes_in_an_order_the_document_does_not_use() -> None:
    """``contacts`` puts HEARD and PKTS between a node's TYPE and its HASH.

    The lane order is a reading decision — the fields a person scans first go left — and
    the key order is the document's. Tying them together would have meant choosing which
    face to make worse.
    """
    header = _plain(_contacts([_ALICE])).splitlines()[0].split()
    assert header.index("HEARD") < header.index("HASH")
    assert list(_json(_contacts([_ALICE]))[0]) == ["node", "heard_at", "packets"]


def test_a_listing_refuses_an_order_that_does_not_name_its_own_lanes() -> None:
    """A typo here draws a listing with a column missing and no complaint.

    That is the one failure a header line cannot show — the records still line up, under
    the wrong headings — so it is caught where it is written instead.
    """
    with pytest.raises(ValueError, match="every lane"):
        Listing(
            key="x",
            columns=(fields.integer("a", "A"), fields.integer("b", "B")),
            order=("A", "NOPE"),
        )


# -- absences --------------------------------------------------------------------------


def test_the_two_faces_spell_absence_in_their_own_alphabets() -> None:
    """``-`` is learned once by a reader; ``null`` is what a parser already has.

    Same fact, and neither face has to know the other's token — which is what stopped the
    plain ``-`` from ever reaching a document as a string.
    """
    empty = {"node": None, "heard_at": None, "packets": None}
    record = _plain(_contacts([empty])).splitlines()[1]
    assert record.split() == ["-", "unknown", "never", "-", "-", "-"]
    assert _json(_contacts([empty]))[0] == {"node": None, "heard_at": None, "packets": None}


def test_a_word_standing_in_for_an_absence_stays_a_word_only_on_the_plain_face() -> None:
    """``unknown`` is a word a reader acts on; a consumer already has ``null`` for it.

    Carrying both would give a program two spellings for one fact and a special case to
    remember on every field.
    """
    row = {"node": fields.NodeRef(hash="a1")}
    block = Listing(key="n", columns=(fields.node("node", lanes=(("type", "TYPE"),)),), rows=[row])
    assert _plain(block).splitlines()[1].strip() == "unknown"
    assert _json(block)[0]["node"]["type"] is None


def test_a_never_heard_node_is_a_fact_and_an_undated_row_is_an_absence() -> None:
    """Two different things, and the age lane says which without a second column.

    A node that has never been heard *has* a heard-age; a row with no such time at all
    does not. Flattening both onto ``-`` would have made "we have not heard from it" and
    "this kind of row has no heard time" the same reading.
    """
    assert script.age(None, absent="never") == "never"
    assert script.age(None) == script.NONE


def test_info_leaves_out_a_reading_the_radio_does_not_answer_for_and_json_keeps_the_key() -> None:
    """The one deliberate disagreement between the faces, and why it is deliberate.

    A missing plain key says "this radio does not report it", where a dash would claim it
    reported nothing — a distinction a person can act on. A consumer cannot, and a
    variable key set costs it a membership test on every field, so the document keeps the
    key and writes ``null``.
    """
    block = Facts(
        key="info",
        fields=(fields.word("model", "model"), fields.integer("uptime_s", "uptime_s")),
        values={"model": None, "uptime_s": 42},
        omit_absent=True,
    )
    assert _plain(block).split() == ["uptime_s", "42"]
    assert _json(block) == {"model": None, "uptime_s": 42}


# -- facts, bare and silent ------------------------------------------------------------


def test_a_get_prints_its_value_alone_and_still_documents_the_key() -> None:
    """The caller named the key, so repeating it back is one more thing to strip off.

    ``$(meshterm config get name)`` needs no stripping — an argument entirely about shell
    substitution, which a JSON consumer is not doing, so the document keeps the key, the
    type and the label the bare form threw away.
    """
    block = Facts(
        key="setting",
        fields=(
            fields.word("key", "key"),
            fields.word("value", "value"),
            fields.hidden("type"),
        ),
        values={"key": "tx_power", "value": "20", "type": "int"},
        shape=BARE,
        bare="value",
    )
    assert _plain(block).strip() == "20"
    assert _json(block) == {"key": "tx_power", "value": "20", "type": "int"}


def test_a_bare_answer_with_nothing_in_it_prints_nothing_at_all() -> None:
    """A node that never answered, a slot with nothing in it — and no lone dash.

    The bare form prints *the value*, so where there is none the exit status is the whole
    report and stdout stays empty. The document still carries the shape, with ``null``
    where the answer would have been, because a consumer branching on it needs the key.
    """
    block = Facts(
        key="remote",
        fields=(fields.word("command", "command"), fields.word("reply", "reply")),
        values={"command": "get name", "reply": None},
        shape=BARE,
        bare="reply",
    )
    assert _plain(block) == ""
    assert _json(block) == {"command": "get name", "reply": None}


def test_an_acknowledgement_prints_nothing_and_is_still_a_document() -> None:
    """Printing nothing is an answer a person can act on and a program cannot.

    ``config advert`` says everything it has to say in its exit status, and that is the
    right plain answer. A caller that needs to log *what was sent* has nowhere to read it,
    which is what the document is for.
    """
    block = Facts(
        key="advert",
        fields=(fields.flag("sent", "sent"), fields.flag("flood", "flood")),
        values={"sent": True, "flood": False},
        shape=SILENT,
    )
    assert _plain(block) == ""
    assert _json(block) == {"sent": True, "flood": False}


# -- several blocks --------------------------------------------------------------------


def test_a_single_block_report_is_its_block_and_nothing_around_it() -> None:
    """``meshterm contacts --json | jq '.[].node.name'`` reads what it looks like.

    There is no envelope: the report a caller needs is ``$?``, exactly as it is without
    the flag, so nothing has to be reached through to get at the answer.
    """
    assert isinstance(_json(_contacts([_ALICE])), list)


def test_several_blocks_make_one_object_with_the_facts_at_its_top_level() -> None:
    """A trace is a set of facts *and* a per-hop table, and the document says so.

    The facts block is the set of facts about the one thing the command answered, so it is
    the document rather than a compartment inside it; a listing needs a name and takes its
    own key.
    """
    facts = Facts(
        key="trace", fields=(fields.flag("success", "success"),), values={"success": True}
    )
    edges = Listing(key="edges", columns=(fields.integer("index", "HOP"),), rows=[{"index": 0}])
    assert _json(facts, edges) == {"success": True, "edges": [{"index": 0}]}
    assert _plain(facts, edges).splitlines() == ["success  yes", "", "HOP", "  0"]


def test_a_block_drawn_only_for_a_person_never_reaches_the_document() -> None:
    """``monitor``'s closing per-node summary is derivable from the records above it.

    Emitting it as a second *shape* of line mid-stream would cost every reader a
    discriminator it otherwise never needs, so the plain face keeps the aggregate and the
    stream stays homogeneous.
    """
    facts = Facts(
        key="window", fields=(fields.integer("packets", "packets"),), values={"packets": 2}
    )
    summary = Listing(
        key="heard",
        columns=(fields.integer("packets", "PKTS"),),
        rows=[{"packets": 2}],
        plain_only=True,
    )
    assert "PKTS" in _plain(facts, summary)
    assert _json(facts, summary) == {"packets": 2}


def test_a_block_that_prints_nothing_leaves_no_blank_line_where_it_stood() -> None:
    """The separator is counted off what was drawn, not off the block index.

    ``config restore --dry-run`` states its outcome silently and its plan as a listing, and
    counting positions instead opened the command with an empty line — output it did not
    have, in whatever was reading it.
    """
    silent = Facts(
        key="restore", fields=(fields.flag("dry_run", "dry_run"),), values={}, shape=SILENT
    )
    plan = Listing(
        key="operations",
        columns=(fields.word("operation", "OPERATION"),),
        rows=[{"operation": "set"}],
    )
    assert _plain(silent, plan).splitlines()[0] == "OPERATION"


# -- the document's own rules ----------------------------------------------------------


def test_a_document_is_one_compact_line_that_never_escapes_a_name() -> None:
    r"""A node name in Cyrillic is a node name, not a run of ``\\uXXXX``.

    One line, so a stream and a one-shot answer are read the same way and ``jq .`` can
    re-indent for a person at no cost. Nothing can un-stream a document that was
    pretty-printed across four hundred lines.
    """
    buffer = io.StringIO()
    row = {"node": fields.NodeRef(name="Наблюдатель", hash="c3")}
    block = Listing(key="n", columns=(fields.node("node"),), rows=[row])
    JsonRenderer(script.console(buffer)).render((block,))
    out = buffer.getvalue()
    assert out.endswith("\n") and out.count("\n") == 1
    assert "Наблюдатель" in out
    assert ", " not in out and ": " not in out


def test_every_time_in_a_document_is_utc_to_the_second() -> None:
    """Twenty characters, always, so string comparison is time comparison.

    A local offset is a fact about the machine that ran the command rather than about the
    event, and two hosts asking the same radio the same question must produce the same
    document.
    """
    stamped = _json(_contacts([_ALICE]))[0]["heard_at"]
    assert stamped == "2026-09-07T22:22:41Z"
    assert len(stamped) == 20


def test_a_time_whose_offset_we_do_not_know_is_absent_on_both_faces() -> None:
    """A naive datetime is not a fact, and neither face may invent an offset for it."""
    naive = {"node": None, "heard_at": datetime(2026, 9, 7, 22, 22, 41), "packets": None}
    assert "2026" not in _plain(_contacts([naive]))
    assert _json(_contacts([naive]))[0]["heard_at"] is None


def test_a_concept_may_have_a_face_on_only_one_side() -> None:
    """Both directions, and each has a real call site.

    A preference's DESCRIPTION is prose for whoever is choosing a value and carries
    nothing a program would branch on; a setting's declared type is the reverse — a fact
    the document wants and a reader dumping ``config show`` does not.
    """
    block = Listing(
        key="prefs",
        columns=(
            fields.word("key", "PREFERENCE"),
            fields.note("help", "DESCRIPTION"),
            fields.hidden("type"),
        ),
        rows=[{"key": "trace_cooldown_s", "help": "Wait this long", "type": "int"}],
    )
    assert "Wait this long" in _plain(block)
    assert _json(block) == [{"key": "trace_cooldown_s", "type": "int"}]


# -- streams ---------------------------------------------------------------------------


def _stream_listing() -> Listing:
    """A live listing in the shape ``monitor`` uses: pinned lanes, the name last."""
    return Listing(
        key="observations",
        columns=(
            fields.instant("observed_at", "TIME"),
            fields.node("node", lanes=(("hash", "NODE"), ("name", "NAME"))),
        ),
        order=("TIME", "NODE", "NAME"),
    )


def test_a_stream_pins_its_lanes_because_the_widths_arrive_with_the_data() -> None:
    """A live capture has no "once every record is in", which is when columns are sized.

    Both streams used to be gutter-joined with no alignment at all, which is why a name in
    one still carried its quotes — nothing else said where the field ended.
    """
    buffer = io.StringIO()
    listing = _stream_listing()
    with PlainRenderer(script.console(buffer)).stream(listing) as row:
        row({"observed_at": _WHEN, "node": fields.NodeRef(name="Alice", hash="d4e5f6a7")})
        row({"observed_at": _WHEN, "node": fields.NodeRef(name="A Very Long Node Name", hash="c3")})
    header, first, second = buffer.getvalue().splitlines()
    assert header.split() == ["TIME", "NODE", "NAME"]
    assert first.index("d4e5f6a7") == second.index("c3")


def test_a_streamed_document_is_one_record_per_line_all_the_same_shape() -> None:
    """A consumer reads line by line and never has to ask which kind of record it holds."""
    buffer = io.StringIO()
    listing = _stream_listing()
    with JsonRenderer(script.console(buffer)).stream(listing) as row:
        row({"observed_at": _WHEN, "node": fields.NodeRef(hash="d4")})
        row({"observed_at": _WHEN, "node": fields.NodeRef(hash="a1")})
    documents = [json.loads(line) for line in buffer.getvalue().splitlines()]
    assert [set(d) for d in documents] == [{"observed_at", "node"}] * 2
    assert [d["node"]["hash"] for d in documents] == ["d4", "a1"]


# -- the boundary ----------------------------------------------------------------------


def test_the_format_picks_the_renderer_and_nothing_else_does() -> None:
    """One choice, made once, at the one place that knows what the caller asked for."""
    console = script.console(io.StringIO())
    assert isinstance(for_format(OutputFormat.PLAIN, console), PlainRenderer)
    assert isinstance(for_format(OutputFormat.JSON, console), JsonRenderer)


def test_a_tool_with_no_scripted_face_renders_nothing_at_all() -> None:
    """The map and the dashboard state no report, and both renderers must be fine with it."""
    assert _plain() == ""
    buffer = io.StringIO()
    JsonRenderer(script.console(buffer)).render(None)
    assert buffer.getvalue() == ""


def test_a_lane_and_a_column_are_separable_so_a_third_format_needs_neither() -> None:
    """The point of the split, stated as a test: a format is a function of the typed row.

    A renderer that wanted CSV would read the same columns and the same rows and write its
    own cells. Nothing about the report knows a gutter, a comma or a brace.
    """
    column = Column(key="n", lanes=(Lane(header="N", render=lambda v: f"<{v}>"),))
    assert column.lanes[0].render(3) == "<3>"
    assert column.json(3) == 3
