"""Tests for the log file: plain text, bounded, and holding the things that went wrong.

The log's one real job is being opened by a person who has hit a problem — often the
person reporting it. Every assertion here is about that reader.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console

from meshterm.persistence.logging import (
    LOG_FILENAME,
    configure_logging,
    get_logger,
    level_from_name,
    log_path,
)


def _read(log_dir: Path) -> str:
    for handler in get_logger().handlers:
        handler.flush()
    return log_path(log_dir).read_text(encoding="utf-8")


def test_the_log_is_plain_text(tmp_path: Path) -> None:
    """One readable line per record: when, how bad, who said it, and what."""
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    get_logger("core.connection").warning("link lost, retrying")
    line = _read(tmp_path).splitlines()[0]
    assert "WARNING" in line
    assert "meshterm.core.connection" in line
    assert "link lost, retrying" in line
    # A date as well as a time. "it happened at 14:40" is useless a week later.
    assert line.startswith("20")


def test_the_file_is_named_plainly(tmp_path: Path) -> None:
    """``meshterm.log`` — an extension every text editor already opens."""
    assert log_path(tmp_path).name == LOG_FILENAME == "meshterm.log"


def test_the_level_keeps_the_quiet_records_out(tmp_path: Path) -> None:
    """At the default, the file holds problems rather than a narration of a working run.

    This is the whole point of the default being WARNING: a log where the dozen useful
    lines sit under twenty thousand dull ones is one nobody reads.
    """
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    log = get_logger()
    log.debug("opened a screen")
    log.info("connected to a radio")
    log.warning("something went wrong")
    written = _read(tmp_path)
    assert "something went wrong" in written
    assert "opened a screen" not in written
    assert "connected to a radio" not in written


def test_turning_it_up_lets_everything_through(tmp_path: Path) -> None:
    """DEBUG is there for chasing something, and keeps what WARNING drops."""
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.DEBUG, quiet=True)
    get_logger().debug("opened a screen")
    assert "opened a screen" in _read(tmp_path)


def test_a_traceback_goes_in_with_the_record(tmp_path: Path) -> None:
    """An exception's traceback is written under the record that reports it.

    This is most of why the file is worth attaching to a bug report: the terminal scrolls
    away and gets closed, and this copy does not.
    """
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    try:
        raise ValueError("the device said something impossible")
    except ValueError:
        get_logger().exception("tool raised")
    written = _read(tmp_path)
    assert "tool raised" in written
    assert "ValueError: the device said something impossible" in written
    assert "Traceback (most recent call last)" in written


def test_the_file_is_bounded(tmp_path: Path) -> None:
    """It rotates. An unbounded log reached 11MB in ten weeks on a real install."""
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    handler = next(h for h in get_logger().handlers if hasattr(h, "maxBytes"))
    assert handler.maxBytes > 0
    assert handler.backupCount > 0


def test_a_level_name_becomes_a_level(tmp_path: Path) -> None:
    """The preference stores a name; the handler needs a number."""
    assert level_from_name("DEBUG") == logging.DEBUG
    assert level_from_name("warning") == logging.WARNING
    assert level_from_name("  Error ") == logging.ERROR


def test_a_nonsense_level_still_starts_the_app() -> None:
    """A typo in a hand-edited preferences file loses log detail, not the app.

    Someone editing the preferences file by hand and writing ``WARN`` should get a slightly quieter log,
    not a program that refuses to launch.
    """
    assert level_from_name("WARN") == logging.WARNING
    assert level_from_name("") == logging.WARNING
