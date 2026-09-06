# scripts

Device-side helpers for running MeshTerm on real hardware. Independent, self-contained
targets live here:

- **Calculinux device** (Luckfox Lyra / ClockworkPi PicoCalc) — one-time bring-up and
  the console font.
- **XIAO nRF52840 UART radio** ([`xiao-radio/`](xiao-radio/)) — wire a Seeed XIAO +
  Wio-SX1262 to the Lyra's hardware UART and run MeshTerm against it. Full build → flash →
  setup guide with scripts. This is the way to give a PicoCalc a real MeshCore radio.
- **SPI-attached radio bridge** (`meshterm-spi-bridge`) — expose a bare SPI LoRa chip
  to MeshTerm as if it were a companion device (uConsole AIO, Waveshare HATs).

## Calculinux device (Luckfox Lyra / ClockworkPi PicoCalc)

Helpers for running MeshTerm on a **Luckfox Lyra / ClockworkPi PicoCalc under Calculinux**
(the bare-framebuffer terminal target). They run *on the device* as root, not on your dev
machine.

| Script | What it does |
|---|---|
| [`calculinux-setup.sh`](calculinux-setup.sh) | Complete one-time bring-up: opkg python packages, the Wi-Fi boot-scan kick, a boot-time clock sync (no RTC on this board), the deploy user, the read-only clone, the venv + `pip install -e .`, the login PATH, and the console font. Idempotent. |
| [`calculinux-console-font.sh`](calculinux-console-font.sh) | Just the console font: builds/installs/persists the 512-glyph **meshterm** PSF (Terminus + braille + node glyphs + rounded frame corners + the list cursor). Called by the setup script; also runnable on its own after a MeshTerm update. |

### Usage

Copy the `scripts/` folder to the device and, as root:

```bash
sh calculinux-setup.sh
```

Two prerequisites the setup script checks for but cannot embed:

- **Deploy key** — place the read-only GitHub deploy key at
  `/home/$DEPLOY_USER/.ssh/id_ed25519` (`DEPLOY_USER` defaults to `meshterm`; `KEY_PATH`
  overrides the location outright)
  (mode 600) before running; the clone uses it over SSH.
- **Wi-Fi credentials** — the kick only nudges a network iwd already knows. Either connect
  once by hand (`iwctl station wlan0 connect <SSID>`) or export `WIFI_SSID` and `WIFI_PSK`
  so the script writes the iwd config (secrets stay in your environment, never in the repo).

Both scripts are POSIX `sh`, idempotent, and safe to re-run.

## XIAO nRF52840 UART radio — give a PicoCalc a real radio

A **Seeed XIAO nRF52840 + Wio-SX1262** wired to the Luckfox Lyra's hardware UART1, running
MeshCore companion firmware over `Serial1` (D6/D7). MeshTerm talks to it directly on
`/dev/ttyS1` — no bridge process. Everything (parts, wiring, firmware build/flash, device
setup, the *why*) is in [`xiao-radio/README.md`](xiao-radio/), with scripts for each step:

| Script | Runs on | What it does |
|---|---|---|
| [`xiao-radio/build-firmware.sh`](xiao-radio/build-firmware.sh) | dev machine | clone + patch MeshCore, build the `_usb` env, emit the `.uf2` |
| [`xiao-radio/flash.py`](xiao-radio/flash.py) | dev machine | flash the `.uf2` to the XIAO via its UF2 bootloader |
| [`xiao-radio/lyra-setup.sh`](xiao-radio/lyra-setup.sh) | the Lyra (root) | boot-persistent UART1→GP4/GP5 mux service, `dialout`, default MeshTerm profile |
| [`xiao-radio/uart1-mux.py`](xiao-radio/uart1-mux.py) | the Lyra | the matrix-IO register poke that routes UART1 to the header pads |

Two things this depends on, both handled by the patch/guide: the firmware must bind the
companion to `Serial1` **and** move I²C off D6/D7 (MeshCore maps it there and it steals the
UART pins); and MeshTerm must include the platform-UART liveness fix (`serial_port_present`
treats a soldered `/dev/ttyS1` as present).

## meshterm-spi-bridge — expose an SPI-attached radio

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

### Usage

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

### Caveats

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

### Overriding hardware pins

Defaults match the hackergadgets uConsole AIO. For other SPI boards, set env
vars before launching (read by the direct-`pymc_core` path):
`MESHCORE_BUS_ID`, `MESHCORE_CS_ID`, `MESHCORE_RESET_PIN`, `MESHCORE_BUSY_PIN`,
`MESHCORE_IRQ_PIN`, `MESHCORE_FREQUENCY`, `MESHCORE_TX_POWER`,
`MESHCORE_SPREADING_FACTOR`, `MESHCORE_BANDWIDTH`, `MESHCORE_CODING_RATE`. To
force a specific interpreter, set `MESHTERM_PYMC_PYTHON`.
