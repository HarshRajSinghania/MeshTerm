---
name: purge-test-data
description: Purge simulator/mock node pollution from the default MeshTerm database. Run after any --mock CLI session or simulator test drive, and whenever fake nodes (Alice, Yagi-Repeater, hub-1, hopN hashes, a1b2c3d4… keys) show up in the atlas, dashboard, or trace history.
model: haiku
---

# Purging test data from the MeshTerm database

`meshterm --mock` and simulator test drives write to the **real** default
database (`~/.meshterm/meshterm.db`), polluting the history that feeds the
atlas, dashboard, and trace suggestions. This skill removes exactly the rows
carrying the simulator's fingerprints and nothing else.

## Run

```
$DEV_VENV_PYTHON .claude\skills\purge-test-data\purge.py --dry-run
$DEV_VENV_PYTHON .claude\skills\purge-test-data\purge.py
```

- Always `--dry-run` first and sanity-check the counts (real-session tables
  should mostly report 0; a huge observations count is a red flag — stop and
  investigate before deleting).
- The real run writes a timestamped `meshterm.db.bak-…` next to the database
  before touching anything, then VACUUMs.
- Note: `--dry-run` can under-report `runs` — run rows are only deletable once
  their child traces are actually gone, so the real pass may delete more.
- Finish with a second `--dry-run`: every count should be 0.

## What counts as test data

The `MockDevice` identities in `meshterm/core/connection.py` — keep
`purge.py`'s `MOCK_PREFIXES` / `MOCK_NAMES` in sync if the simulator grows new
nodes:

- key prefixes `a1b2c3d4`, `b2c3d4e5`, `c3d4e5f6`, `d4e5f6a7`, and the all-zero
  mock self key
- names `Yagi-Repeater`, `Local-Repeater`, `Observer-Bot`, `Alice`,
  `MockCompanion`, plus demo targets `hub-1`, `relay-1`, `relay-2`
- the `hopN` placeholder hop hashes only the simulator emits

Deliberately conservative: short hops (e.g. a real `a1` traced at 1-byte
width) never match, and background monitor/chat run rows are left alone — a
stray narrow mock hop left behind is better than a purged real row.

## When to run

After **any** work that launched `meshterm --mock` (the verify skill's first
stop) or exercised `MockDevice` against the default database. Do it in the
same session, before handing back to the user — fake nodes in the atlas on
real hardware are exactly what this prevents.
