# What MeshTerm does

Every screen the menu offers, and the command that does the same job without it. This is
the catalogue; [the command line](cli.md) is the manual for the scripted half of it.

MeshTerm's menu answers one question — *what would you like to do?* — so its five
sections are named for the doing, not the subject. Every interactive screen is listed
here with its scripted equivalent, where one exists.

## 💬 Message — the people on the other end

| Feature | What it does | Scripted |
| --- | --- | --- |
| **💬 Chat** | Live full-screen channel and direct messaging — a scrolling transcript with a pinned input line where sent and received messages stream together. Every message is logged; unread counts show in the menu header. | `meshterm chat send / history / list` |
| **📻 Channels** | Create, join, reorder, mute, and share mesh channels — with QR codes and `meshcore://` share links. | `meshterm channels list / add / join / import / share / clear` |
| **📨 Courier** | Store-and-forward outbox for contacts that aren't reachable yet. Queue a message; it goes out (with ack tracking and polite exponential backoff) the moment the contact is next heard, or at a scheduled time. | `meshterm courier queue / list / send / cancel / clear` |
| **👥 Contacts** | This node and its known contacts — a recency heat-map, overheard packet counts, and full public keys with the path-hash prefix highlighted. The scripted listing is the contacts alone (this node is `meshterm info`'s answer, in far more detail). | `meshterm contacts` |

## 📊 Watch — what the mesh is doing, and what it did

| Feature | What it does | Scripted |
| --- | --- | --- |
| **📊 Dashboard** | The live mesh overview: a two-hour all-packet activity chart with a pulse line, session traffic tallies by packet class, and window RF health beside the radio's own live numbers. Repaints every second. | — |
| **📰 Live feed** | Every packet as it arrives, newest first: time, what the frame *is* (its parsed payload class), and what it is *about* — the node for an advert, the channel for a channel text, sender → recipient for a direct message or a request, the tag for a trace — with reception quality beside it. Enter opens any row in the packet viewer, route graph and all. | — |
| **🚨 Watchtower** | A passive sentinel over the nodes you star: silence alarms when a watched node goes quiet, SNR-sag warnings when reception degrades, recovery notes when it returns, and a heads-up when a never-before-seen node appears. Runs in the background all session; unacked alerts show as a header badge. | — |
| **⏳ Time machine** | Everything the recorder ever heard, as braille charts: reception volume, the median-SNR band, hour-of-day rhythm, packets and nodes per day, and first-ever arrivals — over a switchable 24h / 7d / 30d / all-time window, per node or mesh-wide. | — |
| **🎧 Monitor** | Passive recording is always on in the app. On the CLI, capture a bounded foreground window, tailing each overheard packet and summarising when it ends. It transmits nothing — it only listens. | `meshterm monitor --seconds 60` |

## 🧭 Explore — where the nodes are and how they reach each other

| Feature | What it does | Scripted |
| --- | --- | --- |
| **🌍 Map** | Located nodes plotted over a real OpenStreetMap street basemap rendered as Unicode braille (streets, rivers, place names). Pannable and zoomable; repeaters highlighted and drawn on top. Falls back to a blank grid offline. | — (a map is a picture; `meshterm contacts` lists the same nodes) |
| **🌐 Mesh walk** | The mesh's *observed shape*, walked one node at a time: an evidence graph built from trace walks, firmware routes, overheard relay chains, and repeater neighbour tables. SNR-coloured braille edges, quality bars, Enter to walk, ⌫ to backtrack, type to find any node. The map answers *where*; the walk answers *how it hangs together*. | — |
| **🎯 Trace target** | A live trace screen: pick a target and watch each trace stream in hop by hop, with running per-hop medians and reliability. Compose or force a route through specific repeaters. A trace transmits **exactly once** (repeaters can blacklist nodes that burst) — sample more by running it again. | `meshterm trace --target …` |
| **👣 Trace path** | The other half of tracing: compose the whole circuit by hand — out and back whichever way you choose — and walk it. The path composer suggests each next hop from the links actually observed, strongest first, and can fetch a repeater's neighbour table over the mesh when you hold its admin password. | `meshterm trace-path --path …` |
| **🏆 Trophy case** | Every trace that comes home is scored, on seven boards: longest distance, farthest node, longest single leg, most nodes (with and without revisits), weakest surviving link, and biggest enclosed loop. Records are kept per hash width, and a record walk must be a *trail* — no link crossed twice the same way. | `meshterm records` |

## 🔧 This node — the radio in your hand

| Feature | What it does | Scripted |
| --- | --- | --- |
| **📋 Device info** | The connected companion's identity and full radio configuration at a glance. | `meshterm info` |
| **🔧 Device config** | Every setting the companion firmware exposes — name, radio, client repeat, auto-add, telemetry, custom variables — *staged* for review and applied in place, plus the operations on the box itself: clock sync, TOML backup/restore, the identity key, reboot, and factory reset, each acting the moment it's confirmed, with destructive ops gated. Laid out like Repeater admin, so the radio in your hand and one over the mesh are configured the same way. | `meshterm config` / `config set <key> <value>` / `config backup` / `reboot` / … |
| **📡 Send advert** | Announce this node to the mesh — a zero-hop or flood advertisement, or share this node's contact card as a QR code. | `meshterm config advert` / `config share` |
| **🔌 Device discovery** | Enumerate serial *and* Bluetooth LE companions, pick one interactively (or auto-select the only one present), add a network (TCP) companion by host:port, and remember the last good default. A dropped link is detected live and offers to reconnect. | `meshterm devices` |

## 🗼 Other nodes — someone else's radio, over the mesh

| Feature | What it does | Scripted |
| --- | --- | --- |
| **🗼 Repeater admin** | Set up remote repeaters and room servers over the mesh: log in (remembered or prompted password), then a config-style editor speaking the node's text CLI — including repeater-only knobs (TX delay, airtime factor, advert intervals) — plus one-shot actions and a readline remote command line. | `meshterm repeater-admin <node> <command…>` |
| **📶 TX optimize** | Sweep a remote node's transmit power live — coarse, then refine, then verify — watch each level land, and decide whether to apply the winner. | `meshterm tx-optimize --path … [--apply]` |

