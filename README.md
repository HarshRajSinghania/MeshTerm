# MeshTerm

**A full-screen terminal companion for [MeshCore](https://meshcore.co.uk/) LoRa mesh
devices** — tune your radio, chat across the mesh, watch the network live, and keep a
longitudinal record of everything you overhear, all from a connected serial, Bluetooth, or
network (TCP) companion.

MeshTerm is interactive by default: a modern, keyboard-driven TUI built on
[Rich](https://github.com/Textualize/rich) and
[prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit). It is also
fully scriptable — **every screen has a mirror `meshterm` subcommand** — so the same
capabilities drive both an evening of exploring the mesh and a cron job. Every packet the
radio overhears is recorded to a local SQLite database, so the longer you run it, the more
your mesh's history is worth.

<p>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="For MeshCore" src="https://img.shields.io/badge/for-MeshCore-8A2BE2">
  <img alt="Interface: TUI + CLI" src="https://img.shields.io/badge/interface-TUI%20%2B%20CLI-orange">
</p>

<!--
  Screenshots welcome here. Drop PNGs into docs/ and reference them, e.g.:
  ![Dashboard](docs/dashboard.png)
  ![Mesh map](docs/map.png)
-->

---

## Why MeshTerm

- **One tool, two faces.** Launch `meshterm` for a full-screen menu; pass a subcommand for
  a scripted one-shot. The registry that builds the menu builds the CLI, so they never
  drift apart.
- **It remembers.** A passive monitor logs every overheard advert and telemetry frame —
  SNR, RSSI, shared location — to SQLite from the moment the radio opens. Maps, contact lists,
  charts, and the Time Machine all read back that history.
- **It measures.** Live trace with per-hop reliability, a coarse→refine→verify TX-power
  sweep, and a walkable graph of how the mesh actually hangs together.
- **It talks.** Live channel and direct messaging, store-and-forward courier delivery for
  contacts that aren't there yet, and shareable channels as QR codes and `meshcore://` links.
- **It connects however your companion does.** Serial (USB), Bluetooth LE, or TCP (a WiFi
  board or a network proxy) — the discoverable transports are auto-found and picked
  interactively, a network device is named by hand, and every kind reconnects live when a link
  drops. No radio? A built-in simulator covers development.
- **It works offline.** The street map caches OpenStreetMap tiles to disk and falls back to
  a blank grid when there's no network — it never needs the internet to run.

## Install

```bash
git clone <your-fork-url> meshterm
cd meshterm
pip install -e ".[dev]"
```

Python **3.10+** is required (matching the `meshcore` library). The `[dev]` extra adds
pytest and ruff; drop it for a runtime-only install (`pip install -e .`).

```bash
python -m pytest -q      # run the test suite
```

## Quick start

```bash
# Interactive full-screen menu (auto-discovers serial + Bluetooth companions)
meshterm

# Connect over Bluetooth (address from `meshterm devices`)
meshterm --ble AA:BB:CC:DD:EE:FF info

# Connect over the network to a TCP companion (host[:port], port defaults to 5000)
meshterm --tcp 192.168.1.50 info

# No radio attached? Use the built-in simulator for development.
meshterm --mock

# On a ClockworkPi PicoCalc (Luckfox Lyra / Calculinux) the handheld flavour is
# auto-detected: a 53-column layout, a 16-slot palette, a compact glyph language on a
# custom console font, and an F-key hint lane. Preview it anywhere, or inspect it:
meshterm --mock --platform picocalc
meshterm specimen
```

Handheld bring-up (fonts, palette, boot services) lives in
`scripts/calculinux-setup.sh` + `scripts/calculinux-console-font.sh`.

## Features

MeshTerm's menu answers one question — *what would you like to do?* — so its five
sections are named for the doing, not the subject. Every interactive screen is listed
here with its scripted equivalent, where one exists.

### 💬 Message — the people on the other end

| Feature | What it does | Scripted |
| --- | --- | --- |
| **💬 Chat** | Live full-screen channel and direct messaging — a scrolling transcript with a pinned input line where sent and received messages stream together. Every message is logged; unread counts show in the menu header. | `meshterm chat send / history / list` |
| **📻 Channels** | Create, join, reorder, mute, and share mesh channels — with QR codes and `meshcore://` share links. | `meshterm channels list / add / join / import / share / clear` |
| **📨 Courier** | Store-and-forward outbox for contacts that aren't reachable yet. Queue a message; it goes out (with ack tracking and polite exponential backoff) the moment the contact is next heard, or at a scheduled time. | `meshterm courier queue / list / send / cancel / clear` |
| **👥 Contacts** | This node and its known contacts — a recency heat-map, overheard packet counts, and full public keys with the path-hash prefix highlighted. | `meshterm contacts` |

### 📊 Watch — what the mesh is doing, and what it did

| Feature | What it does | Scripted |
| --- | --- | --- |
| **📊 Dashboard** | The live mesh overview: a two-hour all-packet activity chart with a pulse line, session traffic tallies by packet class, and window RF health beside the radio's own live numbers. Repaints every second. | — |
| **📰 Live feed** | Every packet as it arrives, newest first: time, what the frame *is* (its parsed payload class), and what it is *about* — the node for an advert, the channel for a channel text, sender → recipient for a direct message or a request, the tag for a trace — with reception quality beside it. Enter opens any row in the packet viewer, route graph and all. | — |
| **🚨 Watchtower** | A passive sentinel over the nodes you star: silence alarms when a watched node goes quiet, SNR-sag warnings when reception degrades, recovery notes when it returns, and a heads-up when a never-before-seen node appears. Runs in the background all session; unacked alerts show as a header badge. | — |
| **⏳ Time machine** | Everything the recorder ever heard, as braille charts: reception volume, the median-SNR band, hour-of-day rhythm, packets and nodes per day, and first-ever arrivals — over a switchable 24h / 7d / 30d / all-time window, per node or mesh-wide. | — |
| **🎧 Monitor** | Passive recording is always on in the app. On the CLI, capture a bounded foreground window, tailing each overheard packet and summarising when it ends. It transmits nothing — it only listens. | `meshterm monitor --seconds 60` |

### 🧭 Explore — where the nodes are and how they reach each other

| Feature | What it does | Scripted |
| --- | --- | --- |
| **🌍 Map** | Located nodes plotted over a real OpenStreetMap street basemap rendered as Unicode braille (streets, rivers, place names). Pannable and zoomable; repeaters highlighted and drawn on top. Falls back to a blank grid offline. | `meshterm map` (one-shot render) |
| **🌐 Mesh walk** | The mesh's *observed shape*, walked one node at a time: an evidence graph built from trace walks, firmware routes, overheard relay chains, and repeater neighbour tables. SNR-coloured braille edges, quality bars, Enter to walk, ⌫ to backtrack, type to find any node. The map answers *where*; the walk answers *how it hangs together*. | — |
| **🎯 Trace target** | A live trace screen: pick a target and watch each trace stream in hop by hop, with running per-hop medians and reliability. Compose or force a route through specific repeaters. A trace transmits **exactly once** (repeaters can blacklist nodes that burst) — sample more by running it again. | `meshterm trace --target …` |
| **👣 Trace path** | The other half of tracing: compose the whole circuit by hand — out and back whichever way you choose — and walk it. The path composer suggests each next hop from the links actually observed, strongest first, and can fetch a repeater's neighbour table over the mesh when you hold its admin password. | `meshterm trace --path …` |
| **🏆 Trophy case** | Every trace that comes home is scored, on seven boards: longest distance, farthest node, longest single leg, most nodes (with and without revisits), weakest surviving link, and biggest enclosed loop. Records are kept per hash width, and a record walk must be a *trail* — no link crossed twice the same way. | `meshterm records` |

### 🔧 This node — the radio in your hand

| Feature | What it does | Scripted |
| --- | --- | --- |
| **📋 Device info** | The connected companion's identity and full radio configuration at a glance. | `meshterm info` |
| **🔧 Device configuration** | View and *stage* changes to every setting (name, radio, behaviour, experimental) for review before applying, with TOML backup/restore. | `meshterm config` / `config set <key> <value>` |
| **🔨 Device actions** | Immediate operations on the box itself: clock sync, TOML backup/restore, the identity key, reboot, and factory reset — each acting the moment it's confirmed, with destructive ops gated. | `config backup` / `restore` / `reboot` / … |
| **📡 Send advert** | Announce this node to the mesh — a zero-hop or flood advertisement, or share this node's contact card as a QR code. | `meshterm config advert` / `config share` |
| **🔌 Device discovery** | Enumerate serial *and* Bluetooth LE companions, pick one interactively (or auto-select the only one present), add a network (TCP) companion by host:port, and remember the last good default. A dropped link is detected live and offers to reconnect. | `meshterm devices` |

### 🗼 Other nodes — someone else's radio, over the mesh

| Feature | What it does | Scripted |
| --- | --- | --- |
| **🗼 Repeater admin** | Set up remote repeaters and room servers over the mesh: log in (remembered or prompted password), then a config-style editor speaking the node's text CLI — including repeater-only knobs (TX delay, airtime factor, advert intervals) — plus one-shot actions and a readline remote command line. | `meshterm repeater-admin <node> <command…>` |
| **📶 TX optimize** | Sweep a remote node's transmit power live — coarse, then refine, then verify — watch each level land, and decide whether to apply the winner. | `meshterm tx-optimize --path … [--apply]` |

## CLI cookbook

Every menu option is also a Typer subcommand — ideal for scripting, cron, and bots.

```bash
# Trace — a trace transmits exactly once; run it again to sample more.
meshterm trace --target Alice --profile yagi

# Force a route through specific repeaters (MeshCore-app style): comma-separated
# contact names and/or hex key prefixes, mixed freely. Blank lets the device route.
meshterm trace --target Alice --path "3d,f2,3d"
meshterm trace --target Alice --path "3d,Bravo-Repeater,f2"

# Sweep and apply a remote node's TX power
meshterm tx-optimize --path "Bravo-Repeater,Alice" --samples 6 --step 3 --apply

# Messaging: live chat in the menu, or scripted from the CLI
meshterm chat                                  # interactive: pick a conversation, chat live
meshterm chat send --to Alice "on my way"      # direct message
meshterm chat send --channel 0 "net in 5"      # channel broadcast
meshterm chat history --to Alice
meshterm chat list

# Store-and-forward: queue for a contact that's offline right now
meshterm courier queue Alice "ping me when you're back" --at 18:30
meshterm courier list                 # the outbox — waiting and finished
meshterm courier send 3               # force one delivery attempt now

# Remote repeater admin (one transmission per invocation)
meshterm repeater-admin Bravo-Repeater "get name"

# Device configuration: view, set, back up, restore
meshterm config                       # show all current settings
meshterm config set radio_sf 9        # change one setting
meshterm config backup node.toml      # archive every setting to TOML
meshterm config restore node.toml --dry-run
meshterm config advert-cadence 2      # auto-advert to neighbours every 2 h (0 = off)
meshterm config advert-cadence 24 --flood   # flood the wider mesh daily

# Passive capture window (records to history; transmits nothing)
meshterm monitor --seconds 60
```

Global options (before the subcommand): `--profile/-p`, `--port`, `--ble`, `--ble-pin`,
`--tcp`, `--mock`, `--db`, `--json`, `--quiet/-q`.

## Configuration

MeshTerm runs with zero configuration — it discovers attached serial devices and nearby
Bluetooth companions, lets you pick one, and remembers the last good default. A network (TCP)
companion isn't discoverable, so reach it with `--tcp host:port`, a TCP profile, or the
picker's "add a network device" prompt. Define a config file only to give your hardware stable
aliases or tune behaviour.

Copy [`config.example.toml`](config.example.toml) to `~/.meshterm/config.toml`:

```toml
default_profile = "s3"
trace_cooldown_s = 1.0
direct_message_soft_retries = 2   # auto-resend an unacked DM up to N extra times (0–2)
history_days = 365                # days of overheard-packet history to keep (0 = forever)

[profiles.s3]
port = "COM5"
baudrate = 115200
default_tx_power = 20
description = "XIAO ESP32-S3 + Wio SX1262 serial companion"

# A Bluetooth LE companion: give it an `address` instead of a `port`.
[profiles.handheld]
address = "AA:BB:CC:DD:EE:FF"
# ble_pin = "123456"   # only if your device requires a pairing PIN
description = "Pocket handheld over Bluetooth"

# A network (TCP) companion: give it a `host` (and optional `tcp_port`, default 5000).
[profiles.wifi]
host = "192.168.1.50"
description = "Basestation over WiFi"
```

Timestamps are stored as UTC and rendered in your local time. History older than
`history_days` is pruned once at session start.

## Architecture

MeshTerm is layered so a new feature is one file in `meshterm/tools/`:

```
cli.py        Typer app; no subcommand -> interactive menu
context.py    AppContext (console, config, repository, device) dependency container
core/         Domain: models, config + device profiles, connection abstraction (+ mock)
tools/        Pluggable "menu options"; each self-registers and gets logging for free
services/     Background algorithms (monitor, trace, tx search, courier, watchtower) — no UI
persistence/  SQLite schema, repository, structured logging
ui/           Rich theme/widgets + the full-screen TUI (screens, dialogs, map, chat)
```

Adding a feature means subclassing `Tool`, decorating it with `@register`, and
implementing `run()`. The same registry builds both the CLI and the menu, and the base
class wraps every execution in a logged `runs` row automatically.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q       # tests
ruff check .              # lint
ruff format .            # format
```

Screens are designed to stay readable at **72 columns**.

Want to send a pull request? See [CONTRIBUTING.md](CONTRIBUTING.md) first.

## Acknowledgements & attribution

MeshTerm stands on the [MeshCore](https://meshcore.co.uk/) project — its firmware and the
`meshcore` Python companion library are what make talking to the radio possible.

### Map data

The street map is rendered from **© OpenStreetMap contributors** data, served as vector
tiles by [OpenFreeMap](https://openfreemap.org/). OpenStreetMap data is available under the
[Open Database License (ODbL)](https://www.openstreetmap.org/copyright). Tiles are decoded
with a small built-in reader for the [Mapbox Vector Tile](https://github.com/mapbox/vector-tile-spec)
format.

### Open-source dependencies

MeshTerm is built with these libraries; each is used under its own license (see the
respective project for the authoritative terms):

| Library | Role | License |
| --- | --- | --- |
| [meshcore](https://pypi.org/project/meshcore/) | Companion-device protocol (serial / BLE) | MIT |
| [pycryptodome](https://www.pycryptodome.org/) | AES/HMAC for decrypting overheard channel packets | BSD-2-Clause / Public Domain |
| [pyserial](https://github.com/pyserial/pyserial) | Serial-port enumeration and I/O | BSD-3-Clause |
| [bleak](https://github.com/hbldh/bleak) | Bluetooth LE scanning + connection | MIT |
| [Typer](https://typer.tiangolo.com/) | Scripted CLI (subcommands mirror the menu) | MIT |
| [Rich](https://github.com/Textualize/rich) | Console rendering | MIT |
| [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) | Full-screen interactive TUI | BSD-3-Clause |
| [Segno](https://github.com/heuer/segno) | Pure-Python QR codes (channel share links) | BSD-3-Clause |
| [tomli](https://github.com/hukkin/tomli) / [tomli-w](https://github.com/hukkin/tomli-w) | TOML config read/write | MIT |

## License

Copyright © 2026 Jean-Pierre Martineau.

MeshTerm is open source under the [Apache License, Version 2.0](LICENSE) — free to
use, modify, and redistribute, commercially or otherwise. The "MeshTerm" name and any
associated logo are trademarks reserved by the author and aren't covered by the code
license (see [NOTICE](NOTICE)); a redistributed fork should go by its own name. The
donate link built into the app supports this project and its original author —
forks that keep soliciting through it without redirecting it to themselves aren't
affiliated with this project.
