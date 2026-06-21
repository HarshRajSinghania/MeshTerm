# MeshTools

A modern, extensible command-line toolkit for tuning and exploring a
[MeshCore](https://meshcore.co.uk/) mesh through a serial-connected companion device.

MeshTools is interactive by default (a Rich + Questionary menu) and fully scriptable
(every menu option is also a Typer subcommand). Every run is logged to SQLite and can be
turned into interactive HTML visualizations.

## Features

| Feature | Status | What it does |
| --- | --- | --- |
| **Trace** | ✅ skeleton | Run repeated path traces to a target and aggregate per-hop SNR. |
| **Device info** | ✅ skeleton | Show the connected companion device's identity and radio config. |
| **Device config** | ✅ working | View and change every setting (name, radio, behavior, experimental), with TOML backup/restore, channels, and gated destructive ops. |
| **Device discovery** | ✅ working | Enumerate serial devices, pick one interactively, and remember the last good default. |
| **History / DB** | ✅ skeleton | Every tool execution and measurement is persisted and queryable. |
| **TX-power optimization** | ✅ working | Sweep transmit power (coarse + refine), converge on the best signal, optionally apply, render an interactive chart. |
| **Path optimization** | 🚧 planned | Discover and score mesh paths from neighbors outward. |
| **Visualizations** | 🟡 partial | Plotly TX-sweep chart (HTML) done; pyvis mesh graph + time-series planned. |

## Install

```bash
pip install -e ".[dev]"
```

Python 3.10+ is required (matching the `meshcore` library).

## Usage

```bash
# Interactive menu (default)
meshtools

# Scripted: every menu option is also a subcommand
meshtools trace --target Alice --samples 10 --profile yagi

# Force a route through specific repeaters (MeshCore-app style): comma-separated
# contact names and/or hex key prefixes, mixed freely. Blank lets the device route.
meshtools trace --target Alice --path "3d,f2,3d"
meshtools trace --target Alice --path "3d,Bravo-Repeater,f2"
meshtools tx-optimize --target Alice --samples 6 --step 3 --apply
meshtools info
meshtools history

# Device configuration: view, set, back up, restore
meshtools config                       # show all current settings
meshtools config set radio_sf 9        # change one setting
meshtools config backup node.toml      # archive every setting to TOML
meshtools config restore node.toml --dry-run

# No radio attached? Use the built-in simulator for development.
meshtools --mock trace --target Alice
```

## Architecture

MeshTools is layered so features are added by dropping a single file in `meshtools/tools/`:

```
cli.py        Typer app; no subcommand -> interactive menu
context.py    AppContext (console, config, repository, device) dependency container
core/         Domain: models, config + device profiles, connection abstraction (+ mock)
tools/        Pluggable "menu options"; each self-registers and gets logging for free
services/     Algorithms (trace aggregation, tx search, path exploration) — no UI
persistence/  SQLite schema, repository, structured logging
ui/           Rich theme/widgets + Questionary menu
viz/          Plotly / pyvis renderers -> HTML & PNG
```

Adding a feature means subclassing `Tool`, decorating it with `@register`, and
implementing `run()`. The same registry builds both the CLI and the menu, and the base
class wraps every execution in a logged `runs` row automatically.
