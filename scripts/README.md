# meshterm-spi-bridge

Expose an **SPI-attached** MeshCore radio to MeshTerm.

Some boards wire the LoRa chip straight to the host's SPI bus — the ClockworkPi
uConsole with the hackergadgets AIO, Waveshare LoRa HATs, and similar. There is
**no companion microcontroller and no firmware to flash**: the mesh node runs in
software on the host via the [`pymc_core`](https://pypi.org/project/pymc-core/)
library (on the uConsole this arrives with the `meshcore-uconsole` apt package,
which also ships the `meshcore-console` GUI).

MeshTerm, however, speaks the MeshCore **companion protocol** over serial / BLE /
TCP and expects to talk to a *node*, not to drive a bare radio. This bridge fills
the gap: it runs the `pymc_core` node itself and puts the standard companion
protocol in front of it on a local TCP port, so that

```
meshterm --tcp 127.0.0.1:5000
```

connects exactly as if a real companion device were plugged in.

## Usage

Copy `meshterm-spi-bridge` onto the radio host and run it with any Python 3 —
it finds the `pymc_core` runtime and re-execs the bridge under it for you:

```
./meshterm-spi-bridge          # interactive setup menu
./meshterm-spi-bridge --check  # run prerequisite checks and exit
./meshterm-spi-bridge --run    # run the bridge now, in this terminal
```

The menu offers:

1. **Run now** — runs in the foreground; Ctrl+C stops it.
2. **Install as a service** — a `systemd --user` unit that starts on boot and
   restarts on failure, running as you (so it uses your node identity).
3. **Uninstall** — removes only the bridge service + its helper copy. It never
   touches the `meshcore-console` / `pymc_core` packages.

## Caveats

- **Only one program may use the radio at a time.** Run *either* the
  `meshcore-console` GUI *or* this bridge — never both. The bridge preflight
  warns you when the GUI is running.
- The bridge and MeshTerm are two cooperating processes; keep the bridge running
  while you use MeshTerm.
- **Same identity as the GUI:** when the `meshcore-console` stack is present, the
  bridge loads the node key from `~/.local/share/meshcore-uconsole/identity.key`,
  so MeshTerm sees the same node. On a generic `pymc_core`-only install it mints
  and persists its own key under `~/.local/share/meshterm-spi-bridge/`.
- **Trace is not wired yet.** Messaging, contacts, adverts and config work
  today; the node's trace / path-discovery replies aren't yet pushed to the
  companion server, so MeshTerm's Trace screen won't populate until that's added.

## Overriding hardware pins

Defaults match the hackergadgets uConsole AIO. For other SPI boards, set env
vars before launching (read by the direct-`pymc_core` path):
`MESHCORE_BUS_ID`, `MESHCORE_CS_ID`, `MESHCORE_RESET_PIN`, `MESHCORE_BUSY_PIN`,
`MESHCORE_IRQ_PIN`, `MESHCORE_FREQUENCY`, `MESHCORE_TX_POWER`,
`MESHCORE_SPREADING_FACTOR`, `MESHCORE_BANDWIDTH`, `MESHCORE_CODING_RATE`. To
force a specific interpreter, set `MESHTERM_PYMC_PYTHON`.
