"""End-to-end tests for what the CLI actually prints and returns.

:mod:`tests.test_script_output` covers the vocabulary; this covers the commands built out
of it — the real Typer app, driven against the simulator, asserting the four things a
script depends on: no escape sequences, no wrapped records, a header its records line up
under, and a documented exit status.

Every command here is read-only or simulator-backed, so nothing transmits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshterm.core import exitcodes

#: Any ANSI escape sequence. Nothing on the CLI's stdout may match this.
_ANSI = re.compile(r"\x1b\[")

#: The shape of a scripted path line: one or more quoted node names, each optionally
#: followed by its hash in parentheses *outside* the quotes, joined by a bare comma. This
#: is the whole grammar — a line matching it splits, whatever the names hold.
_HOP = r'"(?:[^"\\]|\\.)*"(?: \([^)]*\))?'
_PATH_LINE = re.compile(f"{_HOP}(?:,{_HOP})*")

#: The commands whose output is a listing or a key/value block, and the fields a caller
#: should find in each. Every one runs against the mock companion or the database alone.
_LISTINGS: list[tuple[str, list[str], list[str]]] = [
    # (command, arguments, headings or keys the output must carry)
    ("contacts", [], ["NAME", "TYPE", "HEARD", "PKTS", "KEY"]),
    ("info", [], ["name", "public_key", "role"]),
    ("config", ["show"], ["name", "radio_freq", "tx_power"]),
    ("preferences", ["show"], ["PREFERENCE", "VALUE", "DEFAULT"]),
    ("chat", ["list"], ["CONVERSATION", "KIND", "UNREAD", "LAST_TIME", "LAST_TEXT"]),
    ("platform", [], ["platform", "icons"]),
]


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - a closure
    """Invoke the real CLI against the simulator, in a config directory of its own.

    ``--db`` moves the history and *nothing else*: the contact cache, the outbox, the
    channel cache, the stored admin passwords and any device profiles all live in the
    config directory regardless. So a suite that set only ``--db`` read whatever was in
    the developer's own ``~/.meshterm`` — which is how a contact named ``[/]Bob``, planted
    by hand on one machine, could crash this suite on that machine and pass everywhere
    else. ``$MESHTERM_HOME`` is the override that moves the whole directory, and it is
    the one a test wants.
    """
    from meshterm.cli import app

    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))
    runner = CliRunner()

    def invoke(*args: str):  # noqa: ANN202
        return runner.invoke(app, ["--mock", "--db", str(tmp_path / "test.db"), *args])

    return invoke


# -- the four rules -------------------------------------------------------------------


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_prints_no_escape_sequence(run, command, args, fields) -> None:  # noqa: ANN001
    """Nothing is coloured: stdout is a pipe more often than a terminal."""
    result = run(command, *args)
    assert result.exit_code == exitcodes.OK, result.output
    assert not _ANSI.search(result.output)


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_carries_the_fields_a_caller_looks_for(run, command, args, fields) -> None:  # noqa: ANN001
    """The headings and keys are the contract; renaming one silently breaks a script."""
    result = run(command, *args)
    for field in fields:
        assert field in result.output, f"{command} lost {field}"


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_draws_no_frame_and_no_rule(run, command, args, fields) -> None:  # noqa: ANN001
    """No borders, no boxes, no header rules — the command typed is the title."""
    result = run(command, *args)
    assert not set(result.output) & set("─│┌┐└┘├┤━┃")


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_leaves_no_trailing_whitespace(run, command, args, fields) -> None:  # noqa: ANN001
    """Column padding must not become invisible spaces at the end of every record."""
    for line in result_lines(run(command, *args)):
        assert line == line.rstrip(), repr(line)


def result_lines(result) -> list[str]:  # noqa: ANN001
    """The command's stdout as lines, with the trailing blank dropped."""
    return result.output.splitlines()


# -- listings -------------------------------------------------------------------------


def test_contacts_quotes_every_name_and_stamps_every_time(run) -> None:  # noqa: ANN001
    """A record is quoted where a name could hold a space, and timed absolutely."""
    header, *records = result_lines(run("contacts"))
    assert header.split() == ["NAME", "TYPE", "HEARD", "PKTS", "KEY"]
    assert records, "the simulator always has contacts"
    for line in records:
        assert line.startswith('"')
        stamp = line.split()[-3]
        assert stamp == "-" or stamp[:4].isdigit()


def test_contacts_never_elides_a_key(run) -> None:  # noqa: ANN001
    """A truncated key is not something a caller can hand back to ``--to`` or ``--path``."""
    for line in result_lines(run("contacts"))[1:]:
        assert "…" not in line and not line.endswith("...")


def test_a_config_line_can_be_typed_back_into_config_set(run) -> None:  # noqa: ANN001
    """``show`` names each setting by the key ``get``/``set`` take, and prints its value."""
    values = dict(line.split(None, 1) for line in result_lines(run("config", "show")) if line)
    assert values["name"] == "MockCompanion"
    # An enum prints its number, not the reader's label: `parse_value` takes the number.
    assert values["adv_loc_policy"].isdigit()
    assert run("config", "get", "name").output.strip() == "MockCompanion"


def test_every_setting_show_prints_is_one_set_takes_back(run) -> None:  # noqa: ANN001
    """The round-trip claim, checked on every setting rather than on the two easy ones.

    ``config show > f`` and feeding ``f`` back in is the obvious thing to do with a dump,
    and the failure mode is silent: an empty string printed as ``""`` used to parse back
    as *the two quote characters*, and the next dump looked identical, so nothing ever
    said the setting had been replaced. A refusal is fine here — you cannot set a value
    the firmware never reported — but a value that parses to something else is not.
    """
    from meshterm.core import device_config as dc
    from meshterm.tools.config import _script_value

    probes = {
        "str": ["", "Yagi-Repeater", "a name with spaces"],
        "bool": [True, False],
        "int": [0, 1, 12],
        "float": [0.0, 3.5],
    }
    checked = 0
    for _category, specs in dc.settings_by_category():
        for spec in specs:
            if spec.value_type == "enum" and spec.choices:
                values: list = list(spec.choices)
            else:
                values = probes.get(spec.value_type, [])
            for value in values:
                printed = _script_value(spec, value)
                try:
                    parsed = dc.parse_value(spec, printed, {})
                except dc.DeviceConfigError:
                    continue  # a loud refusal is honest; a wrong value is not
                checked += 1
                assert parsed == value, f"{spec.key}: show printed {printed!r} -> {parsed!r}"
    assert checked > 40, f"only {checked} round-trips exercised"


def test_an_unreported_pin_is_not_masked_as_though_one_were_set(run) -> None:  # noqa: ANN001
    """Bullets say "there is a secret here". An absence must not borrow that claim.

    ``conceal`` already declines to mask the *menu's* absence token for exactly this
    reason, but the scripted token is ``-``, which it had never been shown — so a radio
    that had never reported a PIN dumped six bullets, and the reader (or the bug report
    they pasted it into) read that as PIN-locked.
    """
    from meshterm.ui import script
    from meshterm.ui.config_editor import MASK_MARK, conceal

    # The rule at the boundary where it broke: a real value is masked, an absence is not,
    # and each surface says which glyph it writes an absence with.
    assert conceal("1234", absent=script.NONE) == MASK_MARK * 6
    assert conceal(script.NONE, absent=script.NONE) == script.NONE
    assert conceal("?") == "?"

    # And the two surfaces must never disagree about the same radio: `show` wears bullets
    # only where `get` has something to conceal.
    values = dict(
        line.split(None, 1) for line in result_lines(run("config", "show")) if " " in line
    )
    got = run("config", "get", "device_pin").output.strip()
    assert (MASK_MARK in values["device_pin"]) == (got != script.NONE), (
        f"show says {values['device_pin']!r} while get says {got!r}"
    )


def test_config_get_prints_the_bare_value(run) -> None:  # noqa: ANN001
    """The caller named the key; repeating it back is one more thing to strip off."""
    assert run("config", "get", "tx_power").output.strip() == "20"


def test_preferences_get_prints_a_value_its_own_set_would_take(run) -> None:  # noqa: ANN001
    """The page's ``5 s`` carries a unit that ``preferences set`` will not accept back."""
    assert run("preferences", "get", "trace_cooldown_s").output.strip() == "5"


# -- path lines -----------------------------------------------------------------------


def test_a_trace_prints_its_route_as_the_scripted_path_line(run) -> None:  # noqa: ANN001
    """Quoted names, hashes outside the quotes, comma-separated, no star for our own node."""
    result = run("trace", "--target", "Alice")
    assert result.exit_code == exitcodes.OK, result.output
    facts = dict(line.split(None, 1) for line in result_lines(result) if line and " " in line)
    assert facts["success"] == "yes"
    route = facts["route"]
    assert "★" not in route and "→" not in route
    assert _PATH_LINE.fullmatch(route), route


def test_a_trace_reports_its_per_hop_readings_under_a_header(run) -> None:  # noqa: ANN001
    """The second block is a record per hop, joined to the route line by hash."""
    lines = result_lines(run("trace", "--target", "Alice"))
    header = next(line for line in lines if line.startswith("HOP"))
    assert header.split() == ["HOP", "FROM", "TO", "SNR_DB"]


# -- exit statuses --------------------------------------------------------------------


def test_an_empty_result_is_its_own_status_and_prints_nothing(run) -> None:  # noqa: ANN001
    """The simulator configures no channel slots: the command worked and found nothing.

    ``grep`` conflates "found nothing" with "worked"; this is the whole reason
    :data:`~meshterm.core.exitcodes.NO_RESULT` exists.
    """
    result = run("channels", "list")
    assert result.exit_code == exitcodes.NO_RESULT
    assert result.output.strip() == ""


def test_a_bad_flag_is_a_usage_error(run) -> None:  # noqa: ANN001
    """Click's own status, named in the table so it is complete."""
    assert run("contacts", "--no-such-flag").exit_code == exitcodes.USAGE


def test_a_bad_setting_key_is_a_plain_failure(run) -> None:  # noqa: ANN001
    """Retrying will not help: the caller has to change what it asked for."""
    result = run("config", "get", "nosuchkey")
    assert result.exit_code == exitcodes.FAILURE


def test_a_failure_says_so_on_stderr_and_never_on_stdout(run) -> None:  # noqa: ANN001
    """``program: what went wrong`` — so a caller parsing stdout never has to filter it."""
    result = run("config", "get", "nosuchkey")
    assert result.stdout == ""
    assert "meshterm: unknown setting" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("config", "reboot"),
        ("config", "factory-reset"),
        ("config", "import-key", "00" * 32),
        ("channels", "clear", "1"),
        ("preferences", "reset"),
    ],
    ids=["reboot", "factory-reset", "import-key", "channels-clear", "preferences-reset"],
)
def test_a_destructive_command_without_yes_is_a_silent_usage_error(run, args) -> None:  # noqa: ANN001
    """A missing confirmation *is* a bad argument, and it says so where errors go.

    These used to print a red refusal on stdout and exit 1 — a colour and a sentence that
    is not the command's answer, in whatever was reading the command's answer.
    """
    result = run(*args)
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "--yes" in result.stderr


@pytest.mark.parametrize(
    "args,flag",
    [
        (("contacts", "--sort", "bogus"), "--sort"),
        (("records", "--category", "long_hual"), "--category"),
    ],
    ids=["contacts-sort", "records-category"],
)
def test_a_value_outside_a_closed_set_is_refused_rather_than_ignored(run, args, flag) -> None:  # noqa: ANN001
    """A typo in a closed-set option used to be answered with a different question.

    ``--sort bogus`` quietly sorted by the default, and ``--category long_hual`` quietly
    selected *every* discipline — which then came back as exit ``5``, the code that means
    a real empty result. A caller branching on ``5`` could not tell "there are no records"
    from "you misspelled the discipline", which is the worst answer a closed set can give.
    """
    result = run(*args)
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert flag in result.stderr


def test_a_profile_that_names_nothing_is_refused_before_anything_transmits(run) -> None:  # noqa: ANN001
    """``--profile`` is a claim about *which* radio, and an unkeepable one is not a default.

    An unknown name used to fall through to ordinary discovery, so a scheduled
    ``--profile yagi config advert`` with a typo transmitted from whichever companion
    happened to be attached — the one failure mode where being quietly wrong puts a packet
    on the air.
    """
    result = run("--profile", "nosuchprofile", "info")
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "nosuchprofile" in result.stderr


def test_a_port_that_will_not_open_is_no_device_not_a_device_failure(run) -> None:  # noqa: ANN001
    """The two device statuses answer different questions, and this one answered wrong.

    ``3`` says nothing was transmitted — look at what is plugged in. ``4`` says the radio
    was reached and the operation failed, so a retry is reasonable. An unopenable
    ``--port`` reported ``4``, with the message "the connection to the device was lost"
    describing a connection there had never been. The manual's own ``case $?`` recipe
    branches on exactly this split.
    """
    from typer.testing import CliRunner

    from meshterm.cli import app

    result = CliRunner().invoke(app, ["--port", "NOSUCHPORT99", "info"])
    assert result.exit_code == exitcodes.NO_DEVICE
    assert "could not open" in result.stderr
    assert "lost" not in result.stderr


def test_an_empty_result_writes_nothing_to_stdout_at_all(run) -> None:  # noqa: ANN001
    """Not a "no channels configured" line: the status carries it, so the pipe stays clean."""
    result = run("channels", "list")
    assert result.exit_code == exitcodes.NO_RESULT
    assert result.stdout == ""


# -- the map --------------------------------------------------------------------------


def test_the_map_has_no_cli_command_at_all(run) -> None:  # noqa: ANN001
    """A picture has no scripted face; the located nodes stay in ``contacts``."""
    assert run("map").exit_code == exitcodes.USAGE


# -- help -----------------------------------------------------------------------------


def test_help_is_plain_and_names_every_exit_status(run) -> None:  # noqa: ANN001
    """Click's own help: no boxed panels, no colour, and the status table under it."""
    result = run("--help")
    assert not _ANSI.search(result.output)
    assert not set(result.output) & set("─│╭╮╰╯")
    for code in exitcodes.MEANINGS:
        assert f"{code} " in result.output
