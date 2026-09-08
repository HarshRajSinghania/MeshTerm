# The MeshTerm command line

MeshTerm has two front ends over one codebase. The **menu** — what you get by running
`meshterm` with no arguments — is a full-screen session meant to be read by a person. The
**CLI** is meant to be read by `awk`.

This is the manual for the second one: what it prints, what it returns, and every command
it answers to.

- [Running it](#running-it)
- [Output conventions](#output-conventions)
- [Exit status](#exit-status)
- [Command reference](#command-reference)
  - [Finding a device](#finding-a-device)
  - [This node](#this-node)
  - [The mesh around you](#the-mesh-around-you)
  - [Messaging](#messaging)
  - [Tracing](#tracing)
  - [Other nodes](#other-nodes)
  - [MeshTerm itself](#meshterm-itself)
- [Recipes](#recipes)
- [What has no command](#what-has-no-command)

---

## Running it

```
meshterm [GLOBAL OPTIONS] COMMAND [ARGS]
```

With no command, MeshTerm launches the interactive menu instead. Global options go
**before** the command:

| Option | What it does |
| --- | --- |
| `-p`, `--profile NAME` | Use a named device profile from `config.toml`. |
| `--port PORT` | Serial port to connect to (`COM5`, `/dev/ttyACM0`), overriding the profile. |
| `--ble ADDRESS` | Bluetooth address of a companion; selects the BLE transport. |
| `--ble-pin PIN` | Pairing PIN, if the Bluetooth companion asks for one. |
| `--tcp HOST[:PORT]` | Network address of a TCP companion; selects the TCP transport. Default port 5000. |
| `--mock` | Use the built-in simulator instead of real hardware. Nothing transmits. |
| `--db PATH` | Use this SQLite database instead of `~/.meshterm/meshterm.db`. |
| `--json` | Machine-readable output. Only `devices` and `tx-optimize` honour it; everything else is already parseable as plain text. |
| `-q`, `--quiet` | Suppress console logging entirely (the log file still records). |
| `--platform NAME` | Force the UI flavour (`regular`\|`picocalc`) instead of detecting it. |

Commands that need a radio open one, do their work, and close it. Commands that only read
stored history (`records`) or MeshTerm's own state (`preferences`, `about`) need no device
at all and work with nothing attached.

> **One transmission per invocation.** A trace transmits exactly once — repeaters
> penalise, and can blacklist, nodes that burst traffic. To sample more, run the command
> again with your own pacing between runs.

---

## Output conventions

The CLI prints like a standard Unix utility, and every rule below exists so its output can
be piped somewhere without being cleaned up first.

**No colour.** Not a hue, not a bold, not a dim — stdout carries no escape sequence at all,
whether or not it is a terminal. A colour that survives into a file is noise in it.

**No wrapping.** A record is a line. Wrapping would put half a record's fields under the
wrong headings, so nothing folds: a long line runs off the right and your terminal (or
`less -S`, or your pipe) decides what to do about it.

**No frames.** No borders, no boxes, no rules, no titles, no legends. The command you typed
is the title.

**Listings** are an uppercase header line followed by space-aligned records, the shape `ps`
and `df` print. Numeric columns right-align.

```console
$ meshterm contacts
NAME              TYPE      HEARD                      PKTS  KEY
"Alice"           node      2026-09-07T19:58:54-04:00  12    d4e5f6a700000000000000000000000000000000000000000000000000000000
"Yagi-Repeater"   repeater  2026-09-07T19:58:53-04:00  97    a1b2c3d400000000000000000000000000000000000000000000000000000000
```

**Node names are quoted.** A name can hold a space, a comma, even a quote, so a bare name
is not one field — it is however many fields the name happens to split into. Quotes and
backslashes inside a name are escaped the ordinary way (`"He said \"hi\""`), so a quoted
value reads back unambiguously.

**Key/value blocks** are a key, the gutter, and the rest of the line as its value — the
shape `sysctl -a` prints. There is no header, and the value is *not* quoted: a line has
exactly two fields, so the value is whatever follows the key.

```console
$ meshterm info
name              MockCompanion
public_key        0000000000000000000000000000000000000000000000000000000000000000
role              companion
battery_v         4.10
uptime_s          93784
noise_floor_dbm   -110
```

A `get` prints the bare value alone, ready for `$(...)`:

```console
$ meshterm config get tx_power
20
```

**Paths** are quoted node names, each with its hash in parentheses *outside* the quotes,
joined by a bare comma — the same comma `--path` takes:

```
"MockCompanion" (00),"Yagi-Repeater" (a1),"Alice" (d4),"Yagi-Repeater" (a1),"MockCompanion" (00)
```

Your own node is a hop like any other, named and hashed — never a `★`. The hash is there
because the CLI has no colour: on screen a route's hops are told apart by their key-derived
hues, and in plain text the hash is what carries that identity. It is also what joins a
route line to the per-hop table underneath it.

**Times are absolute** local ISO-8601 to the second — `2026-09-07T18:22:41-04:00` — never
`3h ago`. A relative age is for someone watching a screen; a script wants something it can
sort and subtract.

**`-` is the one token for nothing** — absent, unknown, or not applicable. You learn it
once; there is no `—`, no `n/a`, no `never`, no empty cell.

**stdout is the answer; everything else is stderr.** Errors print as
`meshterm: what went wrong`. Progress bars draw on stderr too, and draw nothing at all when
stderr is not a terminal. So `meshterm contacts > contacts.txt` puts contacts in the file
and any complaint on your screen.

---

## Exit status

The return value is half the report.

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Failure with no more specific code — a bad value, an unreadable file, an unhandled fault. |
| `2` | Usage error: an unknown flag, a missing argument, a value the parser rejected. |
| `3` | No companion device could be selected: none attached, none matching the connection flags, or the choice was ambiguous. **Nothing was transmitted.** |
| `4` | A device was reached but the operation failed — a command error, a timeout, a link lost mid-run. Retrying is reasonable. |
| `5` | The command completed and there was **nothing to report**: an empty list, a target that never answered, a conversation with no messages. stdout is empty. This is not an error. |

The table is printed under `meshterm --help` as well.

`5` is the one worth building on. `grep` conflates "found nothing" with "worked", and a
caller that has to count output lines to tell an empty mesh from a full one is parsing when
it could be branching:

```bash
if meshterm contacts > contacts.txt; then
    echo "$(($(wc -l < contacts.txt) - 1)) contacts"      # minus the header
elif [ $? -eq 5 ]; then
    echo "no contacts yet"
else
    echo "could not reach the radio" >&2
fi
```

`3` and `4` split the failures a retry might fix from the ones it never will:

```bash
meshterm trace --target Alice
case $? in
  0) echo "reachable" ;;
  5) echo "no reply — the walk ran, nothing came home" ;;
  3) echo "no radio attached" >&2; exit 1 ;;
  4) echo "radio trouble — worth retrying" >&2; exit 1 ;;
  *) echo "failed" >&2; exit 1 ;;
esac
```

---

## Command reference

### Finding a device

#### `meshterm devices`

List every attached serial device and in-range Bluetooth companion. Opens no radio — it
only enumerates what is attached or advertising.

| Option | What it does |
| --- | --- |
| `--ble` / `--no-ble` | Include a Bluetooth LE scan. On by default; skipping it saves a few seconds. |

```console
$ meshterm devices --no-ble
TARGET  TRANSPORT  NAME                          HARDWARE                 MESHCORE  SERIAL            ACTIVE
COM12   serial     "USB Serial Device (COM12)"   "Adafruit"               maybe     D030D2CCD00F08EB  no
COM16   serial     "USB Serial Device (COM16)"   "Adafruit"               maybe     D8D7D4BB5E4546E0  no
COM1    serial     "Communications Port (COM1)"  "(Standard port types)"  no        -                 no
```

`TARGET` is what `--port` and `--ble` take, verbatim. `MESHCORE` is three-valued and stays
that way: `yes` only once a connection has proved the device speaks the protocol, `maybe`
for a USB vendor ID that suggests a LoRa board or a bridge chip, `no` for anything else — a
vendor ID is a hint, and the column would be lying if it rounded one up.

Returns `5` when nothing is found.

---

### This node

#### `meshterm info`

The connected companion's live status: which radio this is, and how it is doing.

```console
$ meshterm info
name              MockCompanion
public_key        0000000000000000000000000000000000000000000000000000000000000000
role              companion
model             MeshCore Simulator
firmware          mock mock
battery_v         4.10
storage_used_kb   128
storage_total_kb  1024
clock             2026-09-08T01:05:09-04:00
clock_drift_s     -125
uptime_s          93784
noise_floor_dbm   -110
last_rssi_dbm     -62
last_snr_db       +9.5
tx_air_s          42
rx_air_s          360
packets_sent      210
packets_received  1234
receive_errors    3
```

The unit is in the key, so nothing has to be pulled back out of prose. A reading the
firmware does not answer for is **absent** rather than `-`: a missing key says "this device
does not report it", where a dash would claim it reported nothing.

What the radio is *set to* is `config show`'s answer, not this one.

#### `meshterm config`

View and change every device setting. With no subcommand it runs `show`.

| Subcommand | What it does |
| --- | --- |
| `show` | Print every setting as `key value`. |
| `get KEY` | Print one setting's value, bare. |
| `set KEY VALUE` | Change one setting. |
| `backup PATH` | Write every setting to a TOML file. |
| `restore PATH [--dry-run]` | Apply settings from a TOML backup. `--dry-run` prints the plan and changes nothing. |
| `custom KEY VALUE` | Set an experimental custom variable. |
| `channel INDEX NAME [--secret HEX]` | Configure a channel slot (see also the `channels` group). |
| `advert [--flood]` | Broadcast an advertisement. Zero-hop unless `--flood`. |
| `advert-cadence HOURS [--flood]` | How often to auto-advertise in the background. `0` disables. |
| `share` | Print this node's contact card as a `meshcore://` URI. |
| `sync-clock` | Set the device clock from this computer. |
| `export-key [--out PATH]` | Export the private key. **Sensitive.** |
| `import-key KEY_HEX --yes` | Import a private key, overwriting this node's identity. |
| `reboot --yes` | Reboot the device. |
| `factory-reset --yes` | Erase all data and reset to defaults. |

```console
$ meshterm config show
name                 MockCompanion
adv_lat              0.0
adv_lon              0.0
device_pin           ••••••
radio_freq           869.618
radio_bw             62.5
radio_sf             8
radio_cr             5
tx_power             20
manual_add_contacts  false
flood_scope          ""
adv_loc_policy       0
```

`show` names each setting by the key `get` and `set` take, and prints a value they will
take back — an enum's **number**, not the reader's label; `""` for an empty string; `-` for
something the firmware never reported. So a line read out of `show` can be typed straight
back in:

```console
$ meshterm config set radio_sf 9
$ meshterm config get radio_sf
9
```

The pairing PIN stays masked here, because this is the whole-device dump — the thing that
gets redirected into a file and pasted into a bug report. Name it deliberately with
`meshterm config get device_pin`.

Custom variables are namespaced `custom.*` in the output, since they have no spec and
`config set` will not take them (use `config custom`).

Destructive operations are gated behind `--yes` rather than a prompt, so nothing surprising
happens in a script. Without it the command is refused as a **usage error** — nothing on
stdout, the reason on stderr, exit `2`.

---

### The mesh around you

#### `meshterm contacts`

The contacts your device knows.

| Option | What it does |
| --- | --- |
| `-s`, `--sort ORDER` | `heard` (default), `name`, or `packets`. |

```console
$ meshterm contacts --sort name
NAME              TYPE      HEARD                      PKTS  KEY
"Alice"           node      2026-09-07T19:58:54-04:00  12    d4e5f6a700000000000000000000000000000000000000000000000000000000
"Local-Repeater"  repeater  2026-09-07T19:58:53-04:00  48    b2c3d4e500000000000000000000000000000000000000000000000000000000
```

`TYPE` is the node's advertised role in words — `node`, `repeater`, `room`, `sensor`, or
`unknown`. `PKTS` is how many packets passive monitoring has overheard from it. `KEY` is
the full public key, never elided: a truncated key is not something you can hand back to
`--to` or `--path`.

This lists *contacts only*. Your own node is not a contact — `meshterm info` reports it, in
far more detail than a row could hold.

Returns `5` when the device knows no contacts yet.

#### `meshterm monitor`

Capture overheard packets in the foreground for a bounded window, then summarise it.
Transmits nothing; it only listens. Recording to history is always on anyway — this adds
the live view.

| Option | What it does |
| --- | --- |
| `-s`, `--seconds N` | How long to capture. `0` (the default) runs until Ctrl-C. |

```console
$ meshterm monitor --seconds 3
TIME  NODE  NAME  SNR_DB  RSSI_DBM  LAT  LON
2026-09-08T01:07:25-04:00  a1b2c3d4  "Yagi-Repeater"  +7.0  -101  45.50190  -73.56740
2026-09-08T01:07:25-04:00  c3d4e5f6  "Observer-Bot"  +4.9  -93  -  -

NODE      NAME              PKTS  MEDIAN_SNR_DB  BEST_SNR_DB  RSSI_DBM  LAST_HEARD
c3d4e5f6  "Observer-Bot"    54    +5.2           +12.4        -108      2026-09-08T01:07:33-04:00
a1b2c3d4  "Yagi-Repeater"   48    +5.1           +12.8        -100      2026-09-08T01:07:33-04:00
```

Two blocks, separated by a blank line. The **stream** is a header and then a record per
packet as it arrives; it cannot be column-aligned, because the widths are not known until
it ends, so its fields are gutter-separated and the name carries its quotes. The
**summary** is the aggregate the stream cannot give — a count, a median, a best per node —
most recently heard first.

Returns `5` when the window heard nothing.

#### `meshterm records`

The record-setting walks every trace has been scored against. Reads the database; needs no
device and never transmits.

| Option | What it does |
| --- | --- |
| `-c`, `--category ID` | One discipline: `long_haul`, `far_point`, `long_leg`, `grand_tour`, `clean_trail`, `thin_thread`, `big_loop`. |
| `-w`, `--width N` | Only records set at this per-hop hash width (`1`, `2`, or `4`). |

```console
$ meshterm records --category long_haul --width 2
CATEGORY   WIDTH  SCORE  UNIT  RECORDED                   VERSION  ROUTE
long_haul  2      273.0  km    2026-08-17T18:14:28-04:00  0.2.8    "Yagi-Repeater" (a1b2),"Alice" (d4e5),"Yagi-Repeater" (a1b2)
long_haul  2      112.0  km    2026-09-03T17:46:10-04:00  0.2.8    "Local-Repeater" (b2c3),"Alice" (d4e5),"Local-Repeater" (b2c3)
```

`SCORE` is a bare number and `UNIT` is its own column, so the scores in a column are
comparable. A Longest-distance score prefixed `>=` is a **lower bound**: some hop on that
walk has no known position, so the distance is a floor rather than a measurement.

Records are kept per hash width, because the width bounds both a walk's maximum length and
its collision odds — boards at different widths are measuring different games.

Returns `5` when no records match.

---

### Messaging

#### `meshterm chat`

| Subcommand | What it does |
| --- | --- |
| `send TEXT --to NAME` | Send a direct message. |
| `send TEXT --channel N` | Broadcast on a channel slot. |
| `history [--to NAME \| --channel N] [--limit N]` | Print a conversation's stored transcript. Default limit 50. |
| `list` | Every channel and contact with its unread count and last message. |
| `listen [-s SECONDS] [--debug]` | Tail inbound messages live. |

Exactly one of `--to` and `--channel` is required on `send` and `history`.

```console
$ meshterm chat send "on my way" --to Alice
acked  yes
```

A direct message prints its `acked` state, which is the one thing the send does not already
tell you — the radio accepted it either way, and whether the peer answered is a separate
fact. A **channel** broadcast prints nothing: there is no acknowledgement on a channel, so
the exit status is the whole answer.

```console
$ meshterm chat history --to Alice
TIME                       DIRECTION  PEER     SNR_DB  TEXT
2026-09-08T01:07:41-04:00  out        "Alice"  -       on my way
2026-09-08T01:09:02-04:00  in         "Alice"  +6.4    see you there
```

`DIRECTION` is `in` or `out`; `PEER` is always the *other* party. `TEXT` is last and
unquoted — it is the rest of the line, and a message body is the one field that can hold
absolutely anything.

```console
$ meshterm chat list
CONVERSATION      KIND     UNREAD  LAST_TIME                  LAST_TEXT
"Public"          channel  0       -                          -
"Alice"           direct   2       2026-09-07T19:58:54-04:00  see you there
```

`listen` prints a header and then a record per message as it arrives, the same shape
`monitor`'s stream takes. `--debug` routes the message-pull logging to stderr, for working
out whether messages are being pulled from the companion at all.

`history`, `list` and `listen` return `5` when there is nothing to report.

#### `meshterm channels`

| Subcommand | What it does |
| --- | --- |
| `list` | The configured channel slots. |
| `add INDEX NAME [--secret HEX]` | Add a channel. A leading `#` makes it public (the firmware derives the key from the name); otherwise it is private, with a random key unless you give one. |
| `join INDEX NAME SECRET` | Join a channel from its name and 32-hex-character key. |
| `import INDEX URL` | Import a `meshcore://channel/add` link. |
| `share INDEX` | Print a slot's share link. |
| `clear INDEX --yes` | Clear a slot, removing the channel from the device. |

```console
$ meshterm channels list
SLOT  NAME        TYPE     HASH
0     "Public"    public   8f
1     "brigade"   private  2c
```

`share` prints the `meshcore://` URI alone. (The menu draws a QR code beside it; redirected
into a file that is a block of block characters wrapped around the one thing that is
actually the answer.)

`clear` is gated behind `--yes` because a private channel's key is lost with the slot unless
it is saved elsewhere.

`list` returns `5` when no slots are configured.

#### `meshterm courier`

A store-and-forward outbox: queue a message for a contact that is not reachable right now,
and it goes out the moment the contact is next heard — or at a time you name.

| Subcommand | What it does |
| --- | --- |
| `queue CONTACT TEXT… [--at HH:MM]` | Queue a message. `--at` holds it until the next occurrence of that local time. |
| `list` | The outbox: waiting entries then finished ones. |
| `send ID` | Force one delivery attempt now, skipping the "wait until heard" check. |
| `cancel ID` | Remove a waiting entry. |
| `clear` | Drop every finished (delivered / given-up) entry. |

```console
$ meshterm courier queue Alice "ping me when you are back" --at 18:30
id  10

$ meshterm courier list
ID  STATE      NODE       SCHEDULED                  FINISHED                   TEXT
10  waiting    "Alice"    2026-09-08T18:30:00-04:00  -                          ping me when you are back
8   delivered  "Bob"      -                          2026-08-11T18:10:17-04:00  Courier test!
```

`queue` prints the entry's **id**, which is the one fact you have to keep: it is what `send`
and `cancel` take. An entry with no `SCHEDULED` time goes as soon as the contact is heard.

```console
$ meshterm courier send 10
outcome  delivered
```

`outcome` is one of `delivered`, `no ack`, `gave up`, `unknown contact`, `busy`, `gone`.

The *sending* is done by a running interactive session's courier; a queued message sits in
the outbox until one runs.

`list` returns `5` on an empty outbox; `cancel` and `clear` return `5` when there was
nothing to act on.

---

### Tracing

#### `meshterm trace`

Walk the path to a target once and report what came back.

| Option | What it does |
| --- | --- |
| `-t`, `--target NAME` | **Required.** Target node name or key prefix. |
| `-p`, `--path SPEC` | Force a route: comma-separated contact names and/or hex key prefixes, mixed freely (`3d,f2,3d`). Omit it and the device routes. |

```console
$ meshterm trace --target Alice
target      Alice
path        auto
success     yes
hops        3
min_snr_db  +2.6
rtt_ms      459
route       "MockCompanion" (00),"Yagi-Repeater" (a1),"Alice" (d4),"MockCompanion" (00)

HOP  FROM  TO  SNR_DB
0    00    a1  +6.0
1    a1    d4  +2.6
2    d4    00  +5.1
```

Two blocks. The first is what the walk did, `route` among the facts. The second is a record
per hop, naming its two ends by the **hash** rather than repeating the names: the route line
above is where the names are, and the hash is what joins the two blocks. `path` reads `auto`
when the device routed it — every real value there is comma-separated hex.

A trace that never came home is **not** a failure of the command: the radio transmitted and
the walk ran. It reports `success no` and returns `5`.

#### `meshterm trace-path`

The other half of tracing: no target, just a route you compose by hand — out and back
whichever way you choose. It only has to end within earshot of this node.

| Option | What it does |
| --- | --- |
| `-p`, `--path SPEC` | **Required.** The whole walk, comma-separated. |

```console
$ meshterm trace-path --path "a1,d4,a1"
target      -
path        a1,d4,a1
success     yes
hops        4
min_snr_db  +2.0
rtt_ms      458
route       "MockCompanion" (00),"Yagi-Repeater" (a1),"Alice" (d4),"Yagi-Repeater" (a1),"MockCompanion" (00)

HOP  FROM  TO  SNR_DB
0    00    a1  +6.0
1    a1    d4  +2.0
2    d4    a1  +2.4
3    a1    00  +6.0
```

`target` is `-` because a path walk has none. Walks record under a sentinel so they stay out
of the target picker's history.

#### `meshterm tx-optimize`

Sweep a remote node's transmit power against a target and pick the lowest level that still
gets through reliably.

| Option | What it does |
| --- | --- |
| `-p`, `--path SPEC` | **Required.** Forced path ending at the target (`Repeater,Target`, or `3d,f2`). The hop before the target is the node being tuned. |
| `-n`, `--samples N` | Traces per TX level. Default 3. |
| `--step N` | Coarse sweep step. Default 3. |
| `--min N` / `--max N` | Bound the sweep (else the `tx_opt_min` / `tx_opt_max` preferences). |
| `--password TEXT` | Admin password for the tuned node; else the remembered one, else prompted. |
| `--apply` / `--no-apply` | Set the winner on the node. On by default. |

```console
$ meshterm tx-optimize --path "Yagi-Repeater,Alice" --samples 5
tuning_node      Yagi-Repeater
target           Alice
path             a1,d4
optimal_tx_dbm   22
target_snr_db    +4.1
reliability      1.00
previous_tx_dbm  27
applied          yes

TX_DBM  TARGET_SNR_DB  SUCCESSES  SAMPLES
    18           -1.2          2       5
    21           +2.8          5       5
    22           +4.1          5       5
```

The winner comes first because it is what you asked for; the per-level records follow so the
choice can be checked against the measurements it was made from. This transmits many times
(paced by the `trace_cooldown_s` preference) — it is the one command that is not a single
transmission.

Returns `5` when no trace reached the target at any level: nothing was measured, and the
node was left at its original power.

---

### Other nodes

#### `meshterm repeater-admin`

Send one command to a remote repeater or room server over the mesh and print its reply.

```
meshterm repeater-admin NODE COMMAND... [--password TEXT]
```

| Argument / option | What it does |
| --- | --- |
| `NODE` | The remote contact's name. |
| `COMMAND...` | The text command to send, as the node's own CLI spells it. |
| `--password TEXT` | Admin password; else the one remembered from an interactive login. |

```console
$ meshterm repeater-admin Yagi-Repeater get name
Yagi-Repeater
```

The reply is printed as the node sent it — its own text, and the whole answer. A node that
does not answer is not a failure (the command may well have landed), but there is nothing to
report, so it returns `5`.

Logging in happens automatically from the remembered password; run the interactive flow once
to store one, or pass `--password`. A refused login clears the saved password and fails with
`4`.

---

### MeshTerm itself

#### `meshterm preferences`

How MeshTerm behaves, as opposed to how the radio is configured. Reads nothing from the
companion, transmits nothing, and works with no device attached.

| Subcommand | What it does |
| --- | --- |
| `show` | Every preference, its value, and its built-in default. |
| `get KEY` | One preference's value, bare. |
| `set KEY VALUE` | Change one preference. |
| `reset --yes` | Return every preference to its default. |

```console
$ meshterm preferences show
PREFERENCE                   VALUE                                 DEFAULT
trace_cooldown_s             5                                     5
flood_advert_cooldown_s      5                                     60
map_view_fraction            0.5                                   0.5
history_days                 365                                   365
```

Values print in the form `preferences set` accepts back — no unit suffix on a number,
`on`/`off` for a boolean. Where `VALUE` differs from `DEFAULT`, this install has an override.

Overrides live in `~/.meshterm/preferences.yaml`, which lists only what you have changed;
delete a line and the default takes over again.

#### `meshterm about`, `about-author`, `discord`, `support`

The four written pages, printed as plain text. `about` is what MeshTerm is and the terms it
ships under; `about-author` is who wrote it; `discord` is the community invite; `support` is
what keeps it going.

```console
$ meshterm discord
Join the Discord
  Questions, ideas, bug reports, and mesh talk.

  • https://discord.gg/AZwe5Uvb3S
```

The menu draws a QR code beside each link; the scripted face prints the link alone.

#### `meshterm platform`

A diagnostic for which UI flavour a given invocation resolves to, and why.

```console
$ meshterm platform
platform           regular
platform_source    default
flag               -
env                -
device_tree_model  -
icons              yes
icons_source       no-console
```

`icons` is a separate question from the flavour, and the usual reason a Windows session looks
plainer than the screenshots.

#### `meshterm specimen`

Print the visual-language specimen — every mark, icon, colour scale and fold on one card.

**This is the one command that keeps its colour**, and it builds its own themed console to do
it. That is deliberate: the colour *is* the output. It is the font-and-palette acceptance
card on the PicoCalc console, and with `--platform picocalc` it previews that flavour from a
desktop. A monochrome specimen would test nothing.

---

## Recipes

**Watch a node and shout if it goes quiet.** `trace` returning `5` means the walk ran and
nothing came home.

```bash
#!/usr/bin/env bash
target=${1:?usage: watch NODE}
meshterm trace --target "$target" > /dev/null
status=$?          # capture it first: `if ! cmd` would have reset $? to 0
if [ "$status" -eq 5 ]; then
    echo "$target did not answer" | mail -s "mesh alert" me@example.com
fi
```

**Nightly config archive**, keeping the path the command actually wrote:

```bash
out=$(meshterm config backup "$HOME/mesh/$(date +%F).toml") && echo "archived $out"
```

`config backup` prints the file it wrote, bare and one per line, so the path comes back to
the caller.

**Fields out of a listing.** Names are quoted, so a CSV-aware reader is the safe way in;
`awk` works when you split on the quotes rather than on spaces.

```bash
# every repeater's key
meshterm contacts | awk 'NR > 1 && $0 ~ /repeater/ { print $NF }'

# names alone, quotes stripped
meshterm contacts | awk -F'"' 'NR > 1 { print $2 }'
```

**One value into a variable:**

```bash
sf=$(meshterm config get radio_sf)
[ "$sf" -lt 9 ] && meshterm config set radio_sf 9
```

**Capture a window of traffic to a file**, with the progress noise left on the terminal:

```bash
meshterm monitor --seconds 300 > /var/log/mesh-$(date +%s).tsv
```

**Sample a trace politely.** A trace transmits exactly once per invocation by design; pace
the repeats yourself:

```bash
for i in $(seq 5); do
    meshterm trace --target Alice --path "3d,f2,3d"
    sleep "$(meshterm preferences get trace_cooldown_s)"
done
```

**Queue for someone who is offline**, then chase it later:

```bash
id=$(meshterm courier queue Alice "call me" | awk '{print $2}')
# ... later ...
meshterm courier send "$id"
```

**Cron-friendly quiet:** `--quiet` suppresses console logging entirely, leaving only the
command's own output and any error line.

```cron
*/30 * * * * meshterm --quiet --profile yagi config advert --flood
```

---

## What has no command

Some features have no scripted face, and that is the honest answer rather than a degraded
one. Each of these is a *live picture* — its meaning is where things sit on a grid, how they
move, and what colour they are — and none of that survives being turned into records.

| Feature | Why not, and what to use instead |
| --- | --- |
| **Map** | A drawing of braille cells whose nodes are told apart by colour. Stripped of colour to match the rest of the CLI it would be unreadable; left coloured it could not be piped anywhere useful. `meshterm contacts` lists the same nodes, coordinates and all. |
| **Dashboard** | A live overview that repaints every second. `meshterm monitor` captures the same traffic as records. |
| **Live feed** | Every packet as it arrives, newest first, with a viewer behind each row. `meshterm monitor` is the scripted tail. |
| **Watchtower** | A background sentinel over starred nodes; it exists to *interrupt* a session. Build the same alarm from `meshterm trace`'s exit status (see [Recipes](#recipes)). |
| **Time machine** | Braille charts over a switchable window. The underlying history is in the database `--db` points at. |
| **Mesh walk** | An evidence graph walked one node at a time. `meshterm records` and `meshterm trace` cover the walks it is built from. |

Everything else in the menu has a command, listed above.
