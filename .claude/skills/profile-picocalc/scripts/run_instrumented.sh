#!/bin/sh
# Hand this to drive_console.py as --exe to get a per-repaint stage breakdown.
# It runs MeshTerm through instrument.py, which appends CSV rows to
# $MESHTERM_TRACE (default ~/tmp/keytrace.csv).
#
# The checkout is $MESHTERM_REPO, or ~/MeshTerm -- never a hardcoded /home/<someone>,
# since this runs as whatever user the device deployed under.
REPO="${MESHTERM_REPO:-$HOME/MeshTerm}"
cd "$REPO" || exit 1
exec "$REPO/.venv/bin/python" "$REPO/instrument.py" "$@"
