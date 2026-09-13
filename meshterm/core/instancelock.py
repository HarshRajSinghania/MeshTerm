# SPDX-License-Identifier: Apache-2.0
"""One interactive MeshTerm per data directory, and a clear answer when you want two.

Every copy of MeshTerm on a machine reads and writes the same ``~/.meshterm`` — a
checkout, a downloaded build, a second instance for a second radio. Nothing about how it
was installed changes that. Two running at once will not corrupt anything (the database
is in WAL mode and every file lands by rename) but each holds the JSON stores in memory
and rewrites them whole, so the second one to save quietly discards the first one's
changes. A lost contact is much harder to notice than a crash.

So the interactive session takes an exclusive lock on the directory it is using, and a
second one is turned away with the fix in the message: set ``MESHTERM_HOME``.

The lock is held by the operating system rather than written down as a process id in a
file. A pid file has to answer "is that process still alive?", the answer is different on
every platform, and it is wrong after a reboot recycles the number — so a crash leaves a
lock nobody can explain and everybody works around. An OS lock is released when the
process ends, however it ends.

One-shot CLI subcommands do not take it. They are brief, mostly read, and blocking
``meshterm contacts`` because a menu is open elsewhere would be an obstacle rather than a
guard.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["InstanceBusy", "LOCK_NAME", "hold_instance_lock"]

#: The lock file, kept beside the data it guards. Empty; only the lock on it matters.
LOCK_NAME = ".lock"


class InstanceBusy(RuntimeError):
    """Raised when another interactive MeshTerm already holds this data directory."""

    def __init__(self, config_dir: Path) -> None:
        """Build the message, which is mostly the way out of the situation."""
        super().__init__(
            f"Another MeshTerm is already using {config_dir}.\n\n"
            "Two of them sharing one directory will quietly overwrite each other's\n"
            "contacts and settings, so this one stopped instead.\n\n"
            "To run a second one — a downloaded build beside your own, or a second\n"
            "radio — give it a directory of its own:\n\n"
            "    MESHTERM_HOME=~/meshterm-other meshterm\n"
        )
        self.config_dir = config_dir


def _try_lock(handle) -> bool:
    """Take an exclusive, non-blocking lock on an open file; ``False`` if someone has it."""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextmanager
def hold_instance_lock(config_dir: Path) -> Iterator[None]:
    """Hold ``config_dir`` for this process, releasing it however the process ends.

    Args:
        config_dir: The data directory to claim.

    Raises:
        InstanceBusy: Another interactive MeshTerm holds it.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / LOCK_NAME
    handle = open(path, "a+b")  # noqa: SIM115 - closed in the finally below
    try:
        if not _try_lock(handle):
            raise InstanceBusy(config_dir)
        yield
    finally:
        # Closing releases the lock on every platform. The file itself stays: deleting it
        # would race a second process that has already opened it and is about to lock,
        # which is how one directory ends up handed to two of them.
        handle.close()
