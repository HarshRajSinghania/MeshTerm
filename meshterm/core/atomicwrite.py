"""THE way MeshTerm replaces a file it owns: all of the new contents, or none of them.

Every store under ``core/`` keeps its state in one file that it rewrites whole. Writing
in place means a crash — or a laptop lid closing — halfway through leaves a truncated
file where a contact list used to be, so each write goes to a neighbouring temporary file
first and lands with a single rename, which the filesystem either does or doesn't.

The neighbouring file's name carries the writing process's id. That is not fussiness:
MeshTerm's data directory is shared by every copy on the machine — a checkout, a
downloaded build, a second instance for a second radio — and a fixed ``.tmp`` name means
two of them writing at the same moment take turns clobbering one scratch file, then each
rename whatever is in it over the real one. The rename is atomic; the thing being renamed
was not. Per-process names make the two writes independent again, and the last rename
simply wins, which is a lost update rather than a corrupted file.

``MESHTERM_HOME`` is the way to avoid sharing the directory in the first place — see
:func:`meshterm.core.config.default_config_dir`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = ["write_atomically"]


def write_atomically(path: Path, text: str, *, owner_only: bool = False) -> None:
    """Replace ``path`` with ``text`` in one step, creating parent directories as needed.

    Args:
        path: The file to replace. Its directory is created if it does not exist.
        text: The complete new contents, written as UTF-8.
        owner_only: Restrict the file to the owner (``0600``) before it is put in place,
            for anything holding a credential. Applied to the temporary file rather than
            the destination, so there is no moment where the real file exists with wider
            permissions. Best-effort: not every platform or filesystem honours it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if owner_only:
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:  # pragma: no cover - platform-dependent
                pass
        tmp.replace(path)
    except BaseException:
        # A failed write leaves no litter behind. `missing_ok` because the failure may
        # well have been the write that would have created it.
        tmp.unlink(missing_ok=True)
        raise
