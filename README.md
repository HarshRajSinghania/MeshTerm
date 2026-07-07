# MeshTerm

A modern, extensible command-line toolkit for tuning and exploring a
[MeshCore](https://meshcore.co.uk/) mesh through a serial-connected companion device.

MeshTerm is interactive by default (a Rich + Questionary menu) and fully scriptable
(every menu option is also a Typer subcommand). Every run is logged to SQLite and can be
turned into interactive HTML visualizations.

## Features

| Feature | Status | What it does |
| --- | --- | --- |
| **Trace** | ✅ skeleton | Run repeated path traces to a target and aggregate per-hop SNR. |
| **Device info** | ✅ skeleton | Show the connected companion device's identity and radio config. |
| **Node list** | ✅ working | List this node and its known contacts — full public keys with the path-hash prefix highlighted. |
| **Device config** | ✅ working | View and change every setting (name, radio, behavior, experimental), with TOML backup/restore, channels, and gated destructive ops. |
| **Device discovery** | ✅ working | Enumerate serial devices, pick one interactively, and remember the last good default. |
| **History / DB** | ✅ skeleton | Every tool execution and measurement is persisted and queryable. |
| **TX-power optimization** | ✅ working | Sweep transmit power (coarse + refine), converge on the best signal, optionally apply, render an interactive chart. |
| **Link budget** | ✅ working | Predict time-on-air, receiver sensitivity, range, and duty-cycle/dwell headroom for a radio config — offline, no radio needed. |
| **Chat** | ✅ working | Live full-screen channel and direct messaging — a scrolling transcript with a pinned input line where sent and received messages stream together. Every message is logged to SQLite, unread counts show in the menu header, and `chat send/history/list` script the same from the CLI. Channels are picked here; edit them in `config`. |
| **Passive monitor** | ✅ working | Toggle a non-blocking background logger that records every overheard advert/telemetry (SNR, RSSI, location) to a longitudinal history while you keep using the app; on/off is remembered between sessions and live packet counts show in the menu. |
| **Link health** | ✅ working | Compare a target's recent traces to its rolling baseline and flag SNR/reliability regressions, end-to-end and per hop. |
| **Route map** | ✅ working | Aggregate trace history into an interactive route-stability graph — which links are stable, which the mesh flaps between, with churn metrics. |
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
meshterm

# Scripted: every menu option is also a subcommand
meshterm trace --target Alice --samples 10 --profile yagi

# Force a route through specific repeaters (MeshCore-app style): comma-separated
# contact names and/or hex key prefixes, mixed freely. Blank lets the device route.
meshterm trace --target Alice --path "3d,f2,3d"
meshterm trace --target Alice --path "3d,Bravo-Repeater,f2"
meshterm tx-optimize --target Alice --samples 6 --step 3 --apply
meshterm info
meshterm history

# Messaging: live chat in the menu, or scripted from the CLI
meshterm chat                                  # interactive: pick a conversation, chat live
meshterm chat send --to Alice "on my way"      # direct message
meshterm chat send --channel 0 "net in 5"      # channel broadcast
meshterm chat history --to Alice
meshterm chat list

# Device configuration: view, set, back up, restore
meshterm config                       # show all current settings
meshterm config set radio_sf 9        # change one setting
meshterm config backup node.toml      # archive every setting to TOML
meshterm config restore node.toml --dry-run

# No radio attached? Use the built-in simulator for development.
meshterm --mock trace --target Alice
```

## Architecture

MeshTerm is layered so features are added by dropping a single file in `meshterm/tools/`:

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
