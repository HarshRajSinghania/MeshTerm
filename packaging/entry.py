"""Launcher for the frozen builds.

PyInstaller freezes a *script*, not a console-script entry point, so this is the script:
it does what the ``meshterm`` command does and nothing else. Kept in ``packaging/``
rather than in the package, because it exists for the installers and has no business
being importable.
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # Windows and macOS spawn child processes by re-running this executable. Without
    # this call a frozen app that ever starts one forks a second copy of the whole UI
    # instead, which looks like the app launching itself over and over.
    multiprocessing.freeze_support()

    from meshterm.cli import main

    sys.exit(main())
