# scripts

Device-side helpers that put MeshTerm on real hardware: bringing up a ClockworkPi PicoCalc
(Luckfox Lyra, Calculinux) and building its console font, giving that handheld a real
MeshCore radio over UART, and fronting an SPI-attached LoRa chip as if it were a companion
device. They run **on the device**, most of them as root, and they are meant to be read
before they are run.

**[`docs/hardware.md`](../docs/hardware.md) is the manual** — what each target is, what the
scripts assume about the machine, why each step is the way it is, and what has only ever
been proven on one bench. Start there and pick your target; the table below is the index.

| File | Runs on | What it does |
| --- | --- | --- |
| [`calculinux-setup.sh`](calculinux-setup.sh) | the Lyra (root) | One-time bring-up: python packages, the Wi-Fi boot-scan kick, a boot clock sync (there is no RTC), the deploy user, the checkout, the venv and `pip install -e .`, the login PATH, and the console font. Idempotent. |
| [`calculinux-console-font.sh`](calculinux-console-font.sh) | the Lyra (root) | Just the console font: builds, installs and persists the 512-glyph **meshterm** PSF (braille, node marks, rounded frame corners, the list cursor) and restores the stock 16-slot palette. Run it alone after a MeshTerm update. |
| [`calculinux-wifi-set.sh`](calculinux-wifi-set.sh) | the Lyra (sudo) | Interactive Wi-Fi join: prompts for an SSID and passphrase, writes iwd's credentials, connects now. This is how a network *becomes* known to the boot kick. |
| [`calculinux-radio-bridge.sh`](calculinux-radio-bridge.sh) | the Lyra (root) | **Unfinished, unsupported.** An early UART-to-TCP pump, superseded by `xiao-radio/lyra-setup.sh`; its header describes routing it does not contain. See the manual before reading it. |
| [`meshterm-spi-bridge`](meshterm-spi-bridge) | the radio host (your user) | Runs a mesh node in software over an SPI-attached SX1262 (uConsole AIO, Waveshare HATs) and serves the companion protocol on `127.0.0.1:5000`. Setup menu, preflight, optional `systemd --user` service. **Written for `pymc_core` 1.0.x — see the manual's rename note before installing.** |
| [`xiao-radio/`](xiao-radio/) | see below | Everything for the XIAO nRF52840 + Wio-SX1262 radio wired to the Lyra's UART1. |
| [`xiao-radio/build-firmware.sh`](xiao-radio/build-firmware.sh) | dev machine | Clones and patches MeshCore, builds the `Xiao_nrf52_companion_radio_serial` environment, emits the `.uf2`. |
| [`xiao-radio/flash.py`](xiao-radio/flash.py) | dev machine | Flashes the `.uf2` to the XIAO over its UF2 bootloader, refusing the wrong board or SoftDevice; prints the serial-DFU command when the bootloader offers no drive. |
| [`xiao-radio/meshcore-uart1.patch`](xiao-radio/meshcore-uart1.patch) | — | The two firmware hunks: the companion on nRF52 `Serial1`, and I²C moved off the UART pads. Not upstream yet. |
| [`xiao-radio/lyra-setup.sh`](xiao-radio/lyra-setup.sh) | the Lyra (root) | Boot-persistent UART1 → GP4/GP5 mux service, `dialout` membership, and a default MeshTerm serial profile for `/dev/ttyS1`. |
| [`xiao-radio/uart1-mux.py`](xiao-radio/uart1-mux.py) | the Lyra (root) | The matrix-IO register poke that routes UART1 to the header pads, run once per boot by the service above. |

The PicoCalc scripts and the SPI bridge belong to different machines and share nothing but
MeshTerm. On the Lyra, only one program may hold `/dev/ttyS1` — MeshTerm directly, or a pump
in front of it, never both.
