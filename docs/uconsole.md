# MeshTerm on the uConsole

A ClockworkPi uConsole with the hackergadgets AIO LoRa board has no companion
microcontroller and no firmware to flash — the LoRa chip sits straight on the host's SPI
bus. So a small bridge runs the mesh node in software on the uConsole itself and puts the
standard MeshCore companion protocol in front of it on a local TCP port. MeshTerm then
connects to that port like it would any network companion. By the end of this guide you'll
have the bridge running as a background service and MeshTerm talking to it.

> **Read the whole manual through once before you start.** Which route you take in
> Step 2 depends on your Debian release, and whether the GUI is running changes what the
> preflight and the service will do — both are easier to get right knowing the later steps.

- [What you need](#what-you-need)
- [Step 1 — Prepare the system](#step-1--prepare-the-system)
- [Step 2 — Install the radio runtime](#step-2--install-the-radio-runtime)
- [Step 3 — Get the bridge](#step-3--get-the-bridge)
- [Step 4 — Run the preflight](#step-4--run-the-preflight)
- [Step 5 — Run it once in the foreground](#step-5--run-it-once-in-the-foreground)
- [Step 6 — Install it as a service](#step-6--install-it-as-a-service)
- [Step 7 — Install MeshTerm and connect](#step-7--install-meshterm-and-connect)
- [Updating and uninstalling](#updating-and-uninstalling)
- [Troubleshooting](#troubleshooting)
- [What this has been tested on](#what-this-has-been-tested-on)

---

## What you need

**Hardware** — one of:

| Board | Notes |
| --- | --- |
| ClockworkPi **uConsole** (CM4 module) + hackergadgets **AIO** LoRa board | SX1262 on SPI bus 1. This guide's defaults are tuned for it. |
| A Raspberry Pi + **Waveshare SX1262 LoRa HAT** | Also supported. Its pins differ from the AIO's, so set them through the `MESHCORE_*` variables in the **Hardware knobs** table of [`docs/hardware.md`](hardware.md#hardware-knobs). |
| An antenna for your region's LoRa band | Required either way. |

Installing the AIO board into the uConsole is out of scope here — follow
[hackergadgets' own setup guide](https://hackergadgets.com/pages/hackergadgets-uconsole-rtl-sdr-lora-gps-rtc-usb-hub-all-in-one-extension-board-setup-guide)
for that part.

The bridge assumes the AIO's wiring by default:

| Setting | Default |
| --- | --- |
| SPI bus | 1 |
| Reset pin | 25 |
| Busy pin | 24 |
| IRQ pin | 26 |
| TX-enable pin | none |
| DIO2 (RF switch) | on |
| DIO3 (TCXO) | on |
| Frequency | 910.525 MHz |
| TX power | 22 dBm |
| Spreading factor | 7 |
| Bandwidth | 62.5 kHz |
| Coding rate | 4/5 |

Every one of these is overridable with a `MESHCORE_*` environment variable if your board or
region differs — see the **Hardware knobs** table in [`docs/hardware.md`](hardware.md#hardware-knobs).

**Software:**

- Raspberry Pi OS / Debian **bookworm** or **trixie** on the uConsole, 64-bit.
- Python 3 (the system one is fine — the bridge finds whichever interpreter has the radio
  library, and re-execs itself under it).
- A `/dev/spidev*` node. This needs `dtoverlay=spi1-1cs` in `/boot/firmware/config.txt`.
- `/dev/gpiochip0`, and membership of the `spi` and `gpio` groups so you can read and write
  both without root.
- Optionally, the [`meshcore-uconsole`](https://github.com/cwill747/meshcore-uconsole)
  package (it brings the `meshcore-console` GUI and a radio runtime). Whether you can use it depends on your Debian release — see
  [Step 2](#step-2--install-the-radio-runtime).

The radio library that actually drives the chip was renamed in 2026: `pymc_core` became
`openhop_core`. PyPI's `pymc-core` stopped at 1.0.12; `openhop-core` carries on from 1.1.x.
The bridge supports both and prefers the newer one when it finds both installed.

**MeshTerm itself** — either:

- the Linux ARM64 one-file release from
  [the latest release page](https://github.com/jpmartineau/MeshTerm/releases/latest), or
- an install from the repository with `pipx` (Python 3.10 or newer). Plain `pip install`
  into the system Python is refused on bookworm and trixie; `pipx` (`sudo apt install pipx`)
  gives MeshTerm a virtual environment of its own.

---

## Step 1 — Prepare the system

Enable the SPI overlay. Edit `/boot/firmware/config.txt` and add:

```
dtoverlay=spi1-1cs
```

Add yourself to the groups that own the SPI and GPIO device nodes:

```bash
sudo usermod -aG spi,gpio $USER
```

Reboot so both the overlay and the group membership take effect:

```bash
sudo reboot
```

After it comes back, check the device nodes exist and that you can use them:

```bash
ls -l /dev/spidev* /dev/gpiochip0
groups
```

You should see `/dev/spidev1.0` (or another `spidev*` node) and `/dev/gpiochip0` listed,
and `spi` and `gpio` in your `groups` output.

---

## Step 2 — Install the radio runtime

Which route you take depends on your Debian release:

- **trixie**, with `meshcore-uconsole` installed from the vendor's package: you already
  have a working runtime (Python 3.13-based). Skip to [Step 3](#step-3--get-the-bridge).
- **bookworm**: the `meshcore-uconsole` 1.12.0+ package is built for Python 3.13 and will
  **not run** — its virtualenv points at an interpreter bookworm doesn't have. Don't take
  the upgrade if apt offers it. Instead give the bridge a runtime of its own:

```bash
python3 -m venv ~/.local/share/meshterm-spi-bridge/venv
~/.local/share/meshterm-spi-bridge/venv/bin/pip install "openhop-core[hardware]"
```

This is also the route to take on any Debian-family host that isn't running the
`meshcore-uconsole` package at all (a Waveshare HAT on a plain Raspberry Pi OS install, for
example).

You'll confirm which runtime the bridge actually found in [Step 4](#step-4--run-the-preflight) —
its first check line names the interpreter and the runtime.

---

## Step 3 — Get the bridge

`scripts/meshterm-spi-bridge` is a single, self-contained Python file — no install step of
its own. Either clone the MeshTerm repo:

```bash
git clone https://github.com/jpmartineau/MeshTerm
cd MeshTerm/scripts
```

or download just the one file:

```bash
curl -L -o meshterm-spi-bridge \
  https://raw.githubusercontent.com/jpmartineau/MeshTerm/main/scripts/meshterm-spi-bridge
```

Make it executable:

```bash
chmod +x meshterm-spi-bridge
```

---

## Step 4 — Run the preflight

```bash
./meshterm-spi-bridge --check
```

This prints a `Prerequisite check:` block with one line per check. What each line means and
what to do if it fails:

| Check | Meaning if it fails | Fix |
| --- | --- | --- |
| `node runtime` | Neither `openhop_core` nor `pymc_core` is importable by any interpreter the bridge tried. | Build the venv from [Step 2](#step-2--install-the-radio-runtime), or set `MESHTERM_PYMC_PYTHON` to a python that has one. |
| `SPI device` | No `/dev/spidev*` node exists, or it exists but you can't read/write it. | No node at all: add `dtoverlay=spi1-1cs` to `/boot/firmware/config.txt` and reboot. Node present but no permission: `sudo usermod -aG spi $USER`, then log out and back in. |
| `GPIO chip` | `/dev/gpiochip0` is missing or not read/write for you. | `sudo usermod -aG gpio $USER`, then log out and back in. |
| `bridge service` / `radio in use` / `port 5000 busy` / `radio is free` | Tells you whether something already holds the radio: the bridge's own boot service, a running `meshcore-console`, or something else already listening on the TCP port. | Only one program may use the radio at a time — see [Step 7](#step-7--install-meshterm-and-connect). If `meshcore-console` is running, close it before running the bridge. |

If the first three checks all pass, you're ready to run it.

---

## Step 5 — Run it once in the foreground

```bash
./meshterm-spi-bridge --run
```

This runs the preflight again, prints it, then starts the bridge in this terminal. A
healthy startup logs something like:

```
node runtime openhop_core 1.1.3 (/home/you/.local/share/meshterm-spi-bridge/venv/bin/python)
radio config bus_id=1 reset_pin=25 busy_pin=24 irq_pin=26 frequency=910525000 tx_power=22
radio.begin() attempt 1/4
radio ready
node identity ab12cd34ef567890… (hash 0xab, name 'uConsole')
READY — connect with:  meshterm --tcp 127.0.0.1:5000
```

The `radio config` line lists every setting as `key=value`, so the one above is
shortened; yours is longer.

Leave it running, and in another terminal on the same machine try:

```bash
meshterm --tcp 127.0.0.1:5000
```

Back in the bridge's terminal, press **Ctrl+C** to stop it.

---

## Step 6 — Install it as a service

Run the bridge with no flags to get the interactive menu:

```bash
./meshterm-spi-bridge
```

Choose **2) Install as a service**. This:

- copies the script to `~/.local/bin/meshterm-spi-bridge`,
- writes a `systemd --user` unit at
  `~/.config/systemd/user/meshterm-spi-bridge.service` that restarts on failure and starts
  with your session,
- enables and starts it now,
- tries to enable lingering for your user, so the service comes up at boot even before you
  log in. If it can't, it prints the command to run yourself:

```bash
sudo loginctl enable-linger $USER
```

It also offers to add a `[profiles.bridge]` block to your MeshTerm `config.toml` — say yes,
it's the profile you'll use in [Step 7](#step-7--install-meshterm-and-connect).

Check on it any time:

```bash
systemctl --user status meshterm-spi-bridge.service
journalctl --user -u meshterm-spi-bridge.service -f
```

Menu option **4) Show status** prints the same information from inside the script.

---

## Step 7 — Install MeshTerm and connect

Install MeshTerm on the uConsole. The simplest way is the Linux ARM64 one-file release:

```bash
chmod +x meshterm-*-linux-arm64
sudo mv meshterm-*-linux-arm64 /usr/local/bin/meshterm
```

Or install it from the repository with `pipx`:

```bash
sudo apt install pipx
pipx install git+https://github.com/jpmartineau/MeshTerm
pipx ensurepath
```

If you accepted the profile in Step 6, your `config.toml` now has:

```toml
[profiles.bridge]
host = "127.0.0.1"
tcp_port = 5000
description = "Local SPI radio via meshterm-spi-bridge"
```

Connect with:

```bash
meshterm -p bridge
```

or, without a profile:

```bash
meshterm --tcp 127.0.0.1:5000
```

You're connected when the device page or dashboard shows a node name (`uConsole` by
default) instead of a connection error. MeshTerm reports the device model as
`pyMC-spi-bridge` — that's how you tell a bridged node from a real companion.

**Identity.** If the `meshcore-console` GUI's package is present, the bridge loads *that*
package's identity key, so MeshTerm and the GUI are the same node — same contacts, same
public key. On a library-only install, the bridge mints and keeps its own identity key
under `~/.local/share/meshterm-spi-bridge/identity.key`.

**Only one program may use the radio at a time.** Never run the bridge and the
`meshcore-console` GUI at the same time — whichever started second will fail to open the
radio.

---

## Updating and uninstalling

To update, pull or re-download `meshterm-spi-bridge` and reinstall the service (menu option
2 again — it overwrites the copy in `~/.local/bin/`).

To remove the bridge, run the menu and choose **3) Uninstall the service**. This stops and
removes the `systemd --user` unit and the installed script copy. It does **not** touch
`meshcore-console`, the radio library, your node identity, or your saved contacts — those
stay put. If you'd added `[profiles.bridge]` to your MeshTerm config, it tells you to remove
that block by hand if you no longer want it.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `node runtime` fails in the preflight | Did you build the venv in [Step 2](#step-2--install-the-radio-runtime)? Is `MESHTERM_PYMC_PYTHON` pointing at the right interpreter, if set? |
| `SPI device` shows "no /dev/spidev* found" | Is `dtoverlay=spi1-1cs` in `/boot/firmware/config.txt`? Did you reboot after adding it? |
| `SPI device` or `GPIO chip` shows a permission error | Are you in the `spi` and `gpio` groups (`groups`)? Did you log out and back in after `usermod`? |
| Preflight warns "radio in use — meshcore-console is running" | Close the GUI before running or installing the bridge. |
| Preflight warns "port 5000 busy" | Something else is already listening there — check `systemctl --user status meshterm-spi-bridge.service` for an already-running instance. |
| Bridge starts but `radio.begin()` fails after 4 attempts | GPIO lines may still be held by a session that just closed — wait a few seconds and try again. |
| MeshTerm connects but direct messages to an old contact say "not found" | Restart the bridge — contacts are restored from its snapshot file at startup; if the snapshot is missing, the contact needs to be heard again. |
| Reboot, factory reset, or setting a device PIN silently does nothing | Expected — the radio library has no handler for these three, whatever runtime you're on. |
| Traces or the live feed don't show up | These rely on compatibility shims the bridge installs (or, under `openhop_core`, on the library's own equivalents). Check the bridge's log for lines starting `compat:` to confirm they're wired. |

---

## What this has been tested on

This has been verified on a bookworm uConsole with `openhop-core` 1.1.3: the radio comes up,
contacts are restored across a restart, and `info` and `contacts` answer correctly over the
TCP connection. Tracing, sending messages, and the live packet feed under that same runtime
have not yet been exercised end-to-end, though the runtime's own trace-push support is what
the bridge relies on for the first of those. The older `pymc-core` 1.0.x path, with all
three of the bridge's compatibility shims active, has been exercised in full, including
traces and the live feed. For the complete, honest list of what is and isn't verified —
including the on-air trace and acknowledgement behaviour — see
["What is verified and what is not"](hardware.md#what-is-verified-and-what-is-not) in
`docs/hardware.md`.
