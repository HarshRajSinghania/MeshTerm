"""Tests for the one-interactive-session-per-directory guard, and the atomic writer.

Both exist for the same reason: every copy of MeshTerm on a machine shares one data
directory unless told otherwise, so two of them running at once is a thing that happens
by accident rather than by choice.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from meshterm.core.atomicwrite import write_atomically
from meshterm.core.instancelock import LOCK_NAME, InstanceBusy, hold_instance_lock

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_lock_is_released_when_the_holder_leaves(tmp_path: Path) -> None:
    """Two sessions in sequence are fine; the guard is against overlap, not against reuse."""
    with hold_instance_lock(tmp_path):
        pass
    with hold_instance_lock(tmp_path):
        pass


def test_the_lock_file_lives_beside_the_data(tmp_path: Path) -> None:
    """The lock sits in the directory it guards, and the directory is made if missing."""
    target = tmp_path / "not-yet"
    with hold_instance_lock(target):
        assert (target / LOCK_NAME).exists()


def test_a_second_process_is_turned_away(tmp_path: Path) -> None:
    """The real test of an OS lock: another *process* cannot take it.

    Nothing in-process proves anything here — a lock that only excluded the thread that
    already holds it would pass a single-process test and fail in the one situation it
    exists for.
    """
    program = (
        "import pathlib, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from meshterm.core.instancelock import hold_instance_lock, InstanceBusy\n"
        "try:\n"
        f"    with hold_instance_lock(pathlib.Path({str(tmp_path)!r})):\n"
        "        print('TAKEN')\n"
        "except InstanceBusy:\n"
        "    print('REFUSED')\n"
    )
    with hold_instance_lock(tmp_path):
        done = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=60
        )
    assert done.stdout.strip() == "REFUSED", done.stderr[-500:]


def test_the_refusal_says_how_to_run_two(tmp_path: Path) -> None:
    """The message is mostly the way out, because that is what the reader needs next.

    Someone who sees this is doing something reasonable — trying a downloaded build, or
    running a second radio — so being told *no* without being told *how* would be the
    unhelpful half of the answer.
    """
    message = str(InstanceBusy(tmp_path))
    assert "MESHTERM_HOME" in message
    assert str(tmp_path) in message


def test_a_write_lands_whole_or_not_at_all(tmp_path: Path) -> None:
    """The written file holds exactly what was asked for, and no scratch file survives."""
    target = tmp_path / "sub" / "contacts.json"
    write_atomically(target, json.dumps({"a": 1}))
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_the_scratch_file_is_named_for_its_writer(tmp_path: Path, monkeypatch) -> None:
    """Two processes writing the same file must not share one temporary name.

    The rename is atomic; the thing being renamed is not. A fixed ``.tmp`` name lets two
    writers interleave inside one scratch file and then each rename whatever ended up
    there over the real one.
    """
    seen: list[Path] = []
    real = Path.write_text

    def spy(self: Path, *args: object, **kwargs: object) -> int:
        seen.append(self)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy)
    write_atomically(tmp_path / "x.json", "{}")
    assert seen and str(__import__("os").getpid()) in seen[0].name


def test_a_failed_write_leaves_no_litter(tmp_path: Path, monkeypatch) -> None:
    """A write that raises cleans up after itself rather than leaving a partial file."""

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        write_atomically(tmp_path / "y.json", "{}")
    assert list(tmp_path.rglob("*.tmp")) == []
