# Configuring MeshTerm

Preferences, device profiles, and where everything is kept. You need none of it to run
MeshTerm: it discovers a companion, lets you pick one, and remembers the choice.

Two files, two jobs — and you need neither to run.

**Preferences** are how MeshTerm behaves: the pause it leaves between transmissions, how
hard it retries a message, how far back it keeps history, how a map frames itself. Every one has a built-in default, so change them only where you disagree — from
the **Preferences** page in the menu (grouped, staged, saved by one action at the bottom),
or from a shell:

```console
$ meshterm preferences show                 # every preference, its value, its default
$ meshterm preferences set history_days 90
$ meshterm preferences reset --yes          # back to the built-in defaults
```

They are kept in `~/.meshterm/preferences.toml`, which lists only what you have changed;
delete a line and the default takes over again.

**Config** is where things live and which device to talk to. MeshTerm runs with none of it
— it discovers attached serial devices and nearby Bluetooth companions, lets you pick one,
and remembers the last good default. A network (TCP) companion isn't discoverable, so reach
it with `--tcp host:port`, a TCP profile, or the picker's "add a network device" prompt.
Write a config file only to give your hardware stable aliases.

Copy [`config.example.toml`](../config.example.toml) to `~/.meshterm/config.toml`:

```toml
default_profile = "s3"
connect_on_start = true   # false opens the radio link lazily instead of at launch

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

Both live in `~/.meshterm`, along with everything else MeshTerm remembers: the SQLite
history, the outbox, the contact and channel caches, stored admin passwords, and the log.
**`$MESHTERM_HOME` moves the whole directory**, which is the way to run a second radio —
or a `--mock` session — without touching the one you use every day. `--db` moves the
database alone; the rest stays where it was. A `--mock` run that names neither writes to
`meshterm-mock.db` beside the real history, so the simulator's invented nodes never enter
it. [`docs/cli.md`](cli.md#where-meshterm-keeps-its-state)
lists every file.

Timestamps are stored as UTC and rendered in your local time. History older than the
`history_days` preference is pruned once at session start.

