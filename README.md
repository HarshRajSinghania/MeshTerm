# MeshTerm

A modern, extensible command-line toolkit for tuning and exploring a
[MeshCore](https://meshcore.co.uk/) mesh through a serial- or Bluetooth-connected companion
device.

MeshTerm is interactive by default (a full-screen Rich + prompt_toolkit TUI) and fully
scriptable (every menu option is also a Typer subcommand). Every run is logged to SQLite,
building a longitudinal history of the mesh.

## Features

| Feature | Status | What it does |
| --- | --- | --- |
| **Chat** | ✅ working | Live full-screen channel and direct messaging — a scrolling transcript with a pinned input line where sent and received messages stream together. Every message is logged to SQLite, unread counts show in the menu header, and `chat send/history/list` script the same from the CLI. Channels are picked here; edit them in `config`. |
| **Channels** | ✅ working | Create, join, reorder, and share mesh channels — with QR codes and `meshcore://` links. |
| **Map** | ✅ working | Mesh nodes on a pannable street map, repeaters highlighted, fed by everything the monitor has overheard. |
| **Device config** | ✅ working | View and change every setting (name, radio, behavior, experimental), with TOML backup/restore, channels, and gated destructive ops. |
| **Device discovery** | ✅ working | Enumerate serial *and* Bluetooth LE companions, pick one interactively, and remember the last good default. A dropped link (unplug, power-off, or BLE out-of-range) is detected live and offers to reconnect. |
| **Node list** | ✅ working | List this node and its known contacts — recency heat-map, overheard packet counts, full public keys with the path-hash prefix highlighted. |
| **Trace** | ✅ working | A live trace screen: pick a target, watch each trace stream in hop by hop, with running per-hop medians and reliability. |
| **TX-power optimization** | ✅ working | Sweep a remote node's transmit power live (coarse + refine + verify), watch each level land, then decide whether to apply the winner. |
| **Passive monitor** | ✅ working | Always-on background logger that records every overheard advert/telemetry (SNR, RSSI, location) to a longitudinal history while you use the app; live packet counts show in the menu header, the data feeds Nodes and Map, and `meshterm monitor --seconds 60` captures a bounded window from the CLI. |

## Install

```bash
pip install -e ".[dev]"
```

Python 3.10+ is required (matching the `meshcore` library).

## Usage

```bash
# Interactive menu (default)
meshterm

# Connect over Bluetooth instead of USB (address from `meshterm devices`)
meshterm --ble AA:BB:CC:DD:EE:FF info

# Scripted: every menu option is also a subcommand
meshterm trace --target Alice --samples 10 --profile yagi

# Force a route through specific repeaters (MeshCore-app style): comma-separated
# contact names and/or hex key prefixes, mixed freely. Blank lets the device route.
meshterm trace --target Alice --path "3d,f2,3d"
meshterm trace --target Alice --path "3d,Bravo-Repeater,f2"
meshterm tx-optimize --path "Bravo-Repeater,Alice" --samples 6 --step 3 --apply
meshterm info

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
services/     Algorithms (trace aggregation, tx search) — no UI
persistence/  SQLite schema, repository, structured logging
ui/           Rich theme/widgets + the full-screen TUI (screens, dialogs, map, chat)
viz/          Plotly renderers -> interactive HTML
```

Adding a feature means subclassing `Tool`, decorating it with `@register`, and
implementing `run()`. The same registry builds both the CLI and the menu, and the base
class wraps every execution in a logged `runs` row automatically.
