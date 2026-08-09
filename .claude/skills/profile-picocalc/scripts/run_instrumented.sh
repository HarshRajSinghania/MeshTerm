#!/bin/sh
# Hand this to drive_console.py as --exe to get a per-repaint stage breakdown.
# It runs MeshTerm through instrument.py, which appends CSV rows to
# $MESHTERM_TRACE (default $HOME/tmp/keytrace.csv).
cd $HOME/MeshTerm
exec $HOME/MeshTerm/.venv/bin/python $HOME/MeshTerm/instrument.py "$@"
