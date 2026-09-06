---
name: verify
description: How to build, launch, and drive MeshTerm to verify changes at runtime.
model: haiku
---

# Verifying MeshTerm changes

## Launch

- Machine-specific values (this venv, the BLE companions below) live in `.dev.env` at
  the repo root — gitignored, with `.dev.env.example` showing the shape. Read it first;
  the `$DEV_*` names here are its keys.
- Venv interpreter: `$DEV_VENV_PYTHON`
- Run the app as a module: `python -m meshterm …` (no install step needed).
- `--mock` swaps in the `MockDevice` simulator (four fake nodes streaming
  adverts/telemetry, packets within a second) — no hardware required. It still
  writes to the **real default database**, so expect run ids / totals to grow.
- Real hardware is available too: two BLE MeshCore companions —
  `$DEV_BLE_COMPANION_PAIRED` is PIN-protected, `$DEV_BLE_COMPANION_OPEN` is open.

## Surfaces

- **CLI subcommands** (`meshterm --mock <tool> …`) run in a plain console and
  are fully drivable from the PowerShell tool. Good first stop for any change
  that has a CLI path. `--help` / bad flags exercise Typer validation.
- **Interactive TUI** (bare `meshterm`) is a prompt_toolkit full-screen app.
  It CANNOT be driven headlessly on this machine: without a real Windows
  console it dies with `NoConsoleScreenBufferError` (no winpty/pexpect in the
  venv). Verify TUI-only behavior via the CLI-reachable pieces and note the
  gap; the user runs the TUI on real hardware themselves.

## Gotchas

- Time-unbounded commands (e.g. `monitor` / `chat listen` with `--seconds 0`,
  the default) block until Ctrl-C — always pass an explicit `--seconds N`.
- INFO log lines (event hub / monitor) print to the console in CLI mode;
  that's normal outside the full-screen session.
- **Housekeeping is part of verifying**: every `--mock` run pollutes the real
  database with fake nodes. When the mock runs are done, run the
  `purge-test-data` skill before handing back.
