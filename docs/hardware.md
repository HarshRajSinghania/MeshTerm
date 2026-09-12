# MeshTerm on hardware

MeshTerm talks to a **node** — a MeshCore radio that speaks the companion protocol over
serial, Bluetooth LE or TCP. Plug a companion board into USB and none of this document
applies: MeshTerm finds it, and the [command line manual](cli.md) is all you need.

This is the manual for the hardware that *doesn't* present a node that way, and for the
handheld whose console can't draw the interface until you build it a font. Everything under
[`scripts/`](../scripts/) is here: what each script does, what it assumes about the machine
it runs on, why it does it the way it does, and what has only ever been proven on one bench.

The scripts are small, POSIX `sh` or stdlib Python, and meant to be read before they are
run. They are also *device-side* — they change the machine they run on, most of them as
root — so read the section for your target first and run the script second.

- [Three targets, and which one is yours](#three-targets-and-which-one-is-yours)
- [The regular and picocalc platforms](#the-regular-and-picocalc-platforms)
- [Calculinux bring-up](#calculinux-bring-up)
  - [Before you run it](#before-you-run-it)
  - [What the nine phases do](#what-the-nine-phases-do)
  - [The console font on its own](#the-console-font-on-its-own)
  - [Joining a network by hand](#joining-a-network-by-hand)
  - [Re-running, and undoing](#re-running-and-undoing)
- [A real radio for the PicoCalc: XIAO nRF52840 over UART](#a-real-radio-for-the-picocalc-xiao-nrf52840-over-uart)
  - [Parts and wiring](#parts-and-wiring)
  - [Build the firmware](#build-the-firmware)
  - [Flash the XIAO](#flash-the-xiao)
  - [Set up the Lyra](#set-up-the-lyra)
  - [Run it, and check the link](#run-it-and-check-the-link)
  - [Troubleshooting](#troubleshooting)
- [The SPI radio bridge](#the-spi-radio-bridge)
  - [Before you start: the runtime rename](#before-you-start-the-runtime-rename)
  - [Setting it up](#setting-it-up)
  - [Identity, and one program at a time](#identity-and-one-program-at-a-time)
  - [Hardware knobs](#hardware-knobs)
  - [What works through the bridge](#what-works-through-the-bridge)
- [The unfinished UART-to-TCP pump](#the-unfinished-uart-to-tcp-pump)
- [What is verified and what is not](#what-is-verified-and-what-is-not)

---

## Three targets, and which one is yours

There are three independent jobs in `scripts/`, and they belong to different machines.

| I have… | Read |
| --- | --- |
| a ClockworkPi **PicoCalc** with a Luckfox Lyra running Calculinux, and I want MeshTerm on its own screen | [Calculinux bring-up](#calculinux-bring-up) |
| …and I want a **radio** in it | [XIAO nRF52840 over UART](#a-real-radio-for-the-picocalc-xiao-nrf52840-over-uart) |
| a **uConsole** with the hackergadgets AIO, or a Pi with a Waveshare LoRa HAT — an SX1262 on the host's own SPI bus | [The SPI radio bridge](#the-spi-radio-bridge) |
| a MeshCore companion on USB, BLE or WiFi | nothing here — the [main README](../README.md) |

The PicoCalc target and the SPI target have nothing in common but MeshTerm itself. The
console font applies only to the PicoCalc's framebuffer console; the SPI bridge runs a mesh
node in software on a Debian host and never touches a console font. Do not run one target's
scripts on the other's machine.

Within the PicoCalc target the two radio routes are mutually exclusive, and the reason is
the kernel's, not MeshTerm's: **only one program may hold `/dev/ttyS1`.** Either MeshTerm
opens the UART directly, which is what [`lyra-setup.sh`](../scripts/xiao-radio/lyra-setup.sh)
configures and what you want on the device itself, or a pump holds the UART and fronts it on
TCP for other machines — never both at once.

---

## The regular and picocalc platforms

MeshTerm resolves one frozen platform spec at boot and draws everything through it. The
**regular** platform is a desktop or ssh terminal: 72 readable columns, truecolour, emoji,
a footer hint line. The **picocalc** platform is the PicoCalc's framebuffer console, and
every number in it comes from the panel: 53 columns, 26 rows on the 6×12 console font (40
on the 6×8 candidate), no panel border, no emoji anywhere, an F-key lane instead of a hint
line, and sixteen colour slots rather than a colour space — the RGB565 panel is not the
limit, the Linux VT layer is. Nothing loses its colour for being on the console; the node
hue and the recency heat gradient **quantise** to those slots rather than switching rules.
The platform is detected when `/proc/device-tree/model` reads exactly `Luckfox Lyra`, and
`--platform picocalc` or `MESHTERM_PLATFORM=picocalc` forces it anywhere — which is how you
preview the handheld layout on a desktop, alongside `meshterm specimen` for the whole
visual language on one card.

The console font is the part that needs building, and it is built **on the device** by
[`calculinux-console-font.sh`](../scripts/calculinux-console-font.sh) rather than shipped as
a binary. Its only inputs are the stock Terminus console font the Calculinux image already
carries and `python3`; every added glyph is generated geometry in the script itself. So
nothing binary lives in the repo, and the base font is whatever your image installed rather
than whatever a maintainer's was. What it adds is what the bare console cannot draw and
MeshTerm cannot do without: the 256 braille cells every chart and map is made of, eighteen
marks (`●` `◉` `★` `✓` `✗` `▶` `❯` `▸` `⚠` `⌫` `⚙` `↻` among them), and rounded frame
corners aliased onto the square ones.

The budget is the interesting constraint. The framebuffer console caps a font at **512
glyphs**, and braille alone is 256 of them on top of the base font's 256 — so the budget is
full before a single mark is drawn. Each mark is therefore drawn *into a donor slot*: a
pictograph MeshTerm never emits, whose Unicode entry is repointed at the new bitmap. PSF
Unicode tables alias several codepoints onto one glyph, so a careless donation takes a
bystander with it — donating `π` once erased a Cyrillic letter that shared its slot. The
script aborts if a donor is missing, and after the build it verifies a list of keepers with
`π` and `пе` as canaries. A repo test parses the script's own mark, alias and donor tables
and pins them to `meshterm/ui/fontset.py`, the device-verified inventory of the installed
font: the script and the inventory move in the same commit or the test fails.

> **Licensing.** The installed 6×12 font is a **derivative of Terminus**, which is under the
> SIL Open Font Licence 1.1 with "Terminus" as a Reserved Font Name — so the output is named
> `meshterm`, which is exactly what the OFL requires of a renamed derivative, and it must not
> be called Terminus. Nothing Terminus itself is in this repo. The optional 6×8 candidate is
> different: its base bitmap is the Linux kernel's `font_6x8`, which is **GPL-2.0**, embedded
> base64 in the script. That sits awkwardly beside an Apache-2.0 repo and a decision on it is
> pending; the 6×12 Terminus-derived font is the installed default, and the 6×8 build is an
> A/B candidate you flip to by hand.

---

## Calculinux bring-up

[`calculinux-setup.sh`](../scripts/calculinux-setup.sh) turns a freshly flashed Calculinux
SD card into a machine that runs MeshTerm as a dedicated user, with Wi-Fi that survives a
cold boot, a clock that is correct, and a console font. It runs **on the Lyra, as root**,
and the serial console is a fine place to run it from. It is idempotent: every phase checks
before it acts.

```bash
# on the Lyra, as root, with scripts/ copied over
sh calculinux-setup.sh
passwd meshterm          # the script deliberately sets no password
```

Then log in as that user and look at it:

```bash
meshterm --mock          # the interface, with a simulated radio
meshterm specimen        # the visual language on one card
```

### Before you run it

The script assumes Calculinux as shipped — Yocto, systemd, a read-only root with writable
overlays, `opkg` reachable over the network — and that `sh`, `su`, `useradd` or busybox
`adduser`, `systemctl`, `timedatectl`, `sed`, `grep`, `ssh-keyscan`, `git` and `kbd` are
present. It installs `git` and `kbd` anyway rather than trusting the base image.

One thing it cannot embed, and one choice it makes by looking:

- **Wi-Fi credentials.** The boot-time kick only nudges a network `iwd` already knows.
  Either export `WIFI_SSID` and `WIFI_PSK` before running, and the script writes iwd's PSK
  file (mode 600, secrets staying in your environment and never in the repo), or join once
  by hand with [`calculinux-wifi-set.sh`](#joining-a-network-by-hand).
- **How the clone gets in.** MeshTerm is a public repository, so this needs no credential at
  all: with no key file at `$KEY_PATH` the phase clones `$REPO_URL` over public HTTPS and
  says so. Put a read-only GitHub deploy key there (mode 600) and it clones `$REPO_SSH` over
  SSH instead — pinning github.com's host key with `ssh-keyscan` first so the clone never
  blocks on a prompt, and baking the key into the checkout's `core.sshCommand` so later
  pulls need no environment. Either way an existing checkout at
  `/home/$DEPLOY_USER/MeshTerm` is pulled `--ff-only` rather than replaced.

| Knob | Default | Effect |
| --- | --- | --- |
| `DEPLOY_USER` | `meshterm` | the login MeshTerm runs under; its home holds the checkout |
| `TIMEZONE` | `America/Toronto` | any IANA zone; see phase 4 for what happens without tzdata |
| `REPO_URL` | the project's HTTPS URL | cloned when there is no deploy key |
| `REPO_SSH` | the project's SSH URL | cloned when there is one |
| `KEY_PATH` | `/home/$DEPLOY_USER/.ssh/id_ed25519` | the read-only deploy key, if you want the SSH route |
| `WIFI_SSID`, `WIFI_PSK` | empty | set **both** to have iwd's credentials written for you |

### What the nine phases do

Each phase exists because of something that went wrong on the real device, and the *why* is
the part worth reading.

| Phase | What it does, and why |
| --- | --- |
| **1. opkg packages** | Installs `python3-modules`, `python3-pip`, `git`, `kbd`. Calculinux ships a stripped Python 3.13 — no `pip`, no `venv`, no `ensurepip`, and missing `sqlite3`, `ctypes`, `curses` and more — so MeshTerm cannot even import until `python3-modules` restores the interpreter. `/usr` is a writable overlay, so the install persists. |
| **2. Wi-Fi kick** | Writes `/etc/wifi-kick.sh` and a oneshot `wifi-kick.service`. On cold boot the USB RTL8188EU's firmware finishes loading *after* iwd's first scans, so iwd's own scans come back empty and `wlan0` sits UP/NO-CARRIER forever. The kick waits for the interface, then runs **raw `iw dev wlan0 scan`** in rounds until a lease appears. Raw `iw` and not `iwctl scan`: a completed raw scan fills the mac80211 scan results and iwd then associates by itself, whereas `iwctl scan` makes iwd hold the device and the raw scan fails busy. |
| **3. Clock sync** | Writes `/etc/time-sync.sh` and a oneshot ordered after the kick. This board has no battery-backed RTC, so every cold boot starts at the kernel's build epoch — fatal for "heard" ordering and message timestamps. It waits for a real route, then tries `chronyd`, busybox `ntpd`, `sntp`, and finally an HTTPS `Date:` header, and writes the hardware clock best-effort. It always exits 0. |
| **4. Timezone** | Sets `$TIMEZONE` — `America/Toronto` unless you say otherwise — through `timedatectl` once tzdata is installed, and falls back to a POSIX `TZ` rule in `/etc/environment` and the deploy user's `~/.profile` when it is not, because glibc honours a POSIX rule with no zoneinfo database at all and the opkg feed index has been seen to 404. The fallback rule is only known for the default zone; another zone on a device with no tzdata is set through `timedatectl` alone, with a warning. |
| **5. Deploy user** | Creates `$DEPLOY_USER` and puts it in `wheel` for sudo and in `input`, `dialout` and `video`. `input` is not optional: the picocalc platform watches modifier keys directly, and `dialout` is what lets MeshTerm open the radio. Group changes need a fresh login. |
| **6. Clone** | The checkout at `/home/$DEPLOY_USER/MeshTerm`, pulled `--ff-only` if it is already there. See [Before you run it](#before-you-run-it). |
| **7. venv and install** | `python3 -m venv ~/MeshTerm/.venv`, then `pip install -e .` with **`TMPDIR=$HOME/tmp`** — `/tmp` is a small RAM tmpfs and pip's C builds hit ENOSPC in it. |
| **8. Login PATH** | Appends the venv's `bin` to the deploy user's `~/.profile` once, so `meshterm` is just a word. |
| **9. Console font** | Hands off to `calculinux-console-font.sh` if it sits alongside, and otherwise tells you to run it yourself. |

> **A Lyra with no Wi-Fi dongle attached takes about six minutes to boot past the kick**,
> which waits a minute for the interface and then burns thirty rounds before exiting 0 — and
> the clock sync is ordered after it. Nothing is broken; it is a oneshot giving a slow radio
> every chance. Disable `wifi-kick.service` if the device is permanently wired or offline.

### The console font on its own

The font script stands alone, which is what you want after a MeshTerm update that adds a
glyph:

```bash
sh calculinux-console-font.sh
```

It builds both fonts, applies the 6×12 one live with `setfont` on `/dev/tty1`, and persists
it as `FONT=meshterm` in `/etc/vconsole.conf` so systemd loads it at boot. If `setfont`
fails it aborts *before* persisting, so you are never left with a font that only exists in
`vconsole.conf`. It prints the A/B commands as it finishes — `setfont
/usr/share/consolefonts/meshterm8.psf.gz` for 53×40, the same with `meshterm.psf.gz` for
53×26 — and whichever you prefer goes in `vconsole.conf` by hand.

The same script owns the sixteen colour slots. A **default run restores the stock kernel VT
palette** and removes any previous remap: it disables and deletes the `meshterm-vtrgb`
boot unit, `/etc/vtrgb` and the applier, programmes the stock table with `setvtrgb`, and
then writes the sixteen `OSC P` sequences to the tty as well. Both, and not just the
escape-sequence reset, because a previous `setvtrgb` overwrote the kernel's *default*
colormap — so a reset alone would faithfully restore the custom colours you were trying to
get rid of. `MESHTERM_CUSTOM_PALETTE=1` installs the archived custom remap instead, as
`/etc/vtrgb` plus a oneshot that re-applies it after `systemd-vconsole-setup`; a repo test
pins that remap to the palette table in `meshterm/ui/theme.py`. There is no uninstall of
the fonts themselves — delete the two `.psf.gz` files and the `FONT=` line.

### Joining a network by hand

[`calculinux-wifi-set.sh`](../scripts/calculinux-wifi-set.sh) is how a network *becomes*
known, which is the half the boot kick cannot do. It lists known networks and a fresh scan,
prompts for an SSID and a passphrase with the echo off, writes iwd's PSK file for it, and
connects now. It re-execs itself under `sudo` if you are not root.

```bash
sh calculinux-wifi-set.sh
```

It uses `iwctl` where the boot kick deliberately uses raw `iw`, and that is not a
contradiction: the kick's problem is a cold-boot firmware race with iwd's scanner, and this
is an interactive join on a machine that is already up.

### Re-running, and undoing

Re-running is safe and is the normal way to update: the packages, the user, the checkout,
the venv and the PATH line are all checked first, the two helper scripts and their units are
rewritten with identical content, `pip install -e .` runs again cheaply, and the fonts are
rebuilt. Two things to know. Re-running under a **different `DEPLOY_USER` creates a second
user and a second checkout** rather than moving anything, so a device provisioned under one
name must keep being given that name. And there is **no uninstall** of any of it: to undo,
disable `wifi-kick.service` and `time-sync.service`, delete the two helper scripts, drop the
`FONT=` line from `/etc/vconsole.conf`, and remove the user.

---

## A real radio for the PicoCalc: XIAO nRF52840 over UART

The PicoCalc has no radio and no USB host to hang a companion off, so the radio is soldered
to the header: a Seeed XIAO nRF52840 with a Wio-SX1262, running MeshCore's companion
firmware on its own second serial port, wired to the Lyra's UART1. MeshTerm then opens
`/dev/ttyS1` like any other serial companion — no bridge process, no BLE, no SD slot
sacrificed.

```
MeshTerm ──serial──> /dev/ttyS1 ──UART1 (GP4/GP5)──> XIAO Serial1 (D6/D7) ──> SX1262 ──))) mesh
(on the Lyra)         115200 8N1     matrix-IO mux        companion firmware
```

Three steps on two machines: **build** the firmware and **flash** the XIAO from your dev
box, then **set up the Lyra** as root.

### Parts and wiring

- A **Seeed XIAO nRF52840**, plain or Sense, stacked with the **Wio-SX1262** LoRa module.
  The SX1262 uses XIAO pins D1–D5 and D8–D10 and is already wired inside the kit.
- A **Luckfox Lyra** in Pico form factor (RK3506, SD boot) running Calculinux in a
  ClockworkPi PicoCalc.
- Four wires, TX and RX crossed.

| XIAO pad | → | Lyra header | Physical pin | Carries |
| --- | --- | --- | --- | --- |
| **D7** (Serial1 RX) | → | **GP4** | pin **6** | Lyra UART1 **TX** → XIAO RX |
| **D6** (Serial1 TX) | → | **GP5** | pin **7** | XIAO **TX** → Lyra UART1 RX |
| **GND** | → | **GND** | pin **8** | common ground |
| **3V3** | → | **3V3 OUT** | pin **36** | power, so it runs without USB |

> **The Lyra does not use Raspberry Pi Pico GPIO numbering.** The 40-pin header is
> physically Pico-compatible and each pad's function is Luckfox's own RK3506 mapping: GP4 is
> `gpio0-0` and GP5 is `gpio0-1`. Don't read a Pico pinout for anything else here. Every
> signal is 3.3 V, so no level shifter, and there is **no 5 V rail** on this board — power
> the XIAO from 3V3 only.

### Build the firmware

On your dev machine, with `git` and PlatformIO (`pip install platformio`):

```bash
cd scripts/xiao-radio
sh build-firmware.sh
```

It clones MeshCore into `./_meshcore-build` (override with `MESHCORE_DIR`), checks out the
pinned commit **`e9edfc8e`** on the `dev` branch (override with `MESHCORE_COMMIT`), applies
[`meshcore-uart1.patch`](../scripts/xiao-radio/meshcore-uart1.patch), builds the
`Xiao_nrf52_companion_radio_serial` environment, and converts the result into
`meshcore-xiao-radio.uf2` beside the script. The `.uf2` is a build product and is not
committed.

The patch is two hunks, and both are required:

- **Teach the existing companion-over-UART path to build on nRF52.** MeshCore has had the
  `SERIAL_RX`/`SERIAL_TX` companion interface and per-board `*_companion_radio_serial`
  environments for a while — the ESP32-S3 twin of this board uses the *same* D6/D7 pads —
  but the shared code declares `HardwareSerial companion_serial(1)`, and on the Adafruit
  nRF52 core `HardwareSerial` is abstract with no numbered constructor. `Serial1` is the
  concrete `Uart` that core always defines and `Uart::setPins(rx, tx)` takes the same
  arguments in the same order, so one reference fixes the build and the call site is
  untouched.
- **Move I²C off D6/D7.** The base `Xiao_nrf52` environment maps the I²C bus onto exactly
  those two pads, and both `XiaoNrf52Board::begin()` and `sensors.begin()` start `Wire` on
  them — so the I²C peripheral seizes the UART pins and the link stays dead with perfect
  wiring and a correct build. The new environment moves I²C to the internal IMU pins 16 and
  17. This was the single reason the link stayed silent through every other fix.

Both changes are upstream-shaped and have been submitted to MeshCore. **They are not merged
yet**: as of 2026-09-12, upstream `dev` still carries the bare `HardwareSerial` declaration
with no nRF52 branch, and `variants/xiao_nrf52/platformio.ini` still has no
`companion_radio_serial` environment. So the patch step stays for now, and when it lands the
patch and the `git apply` line can both be deleted. To track upstream instead of the pin,
run with `MESHCORE_COMMIT=dev`; if the patch no longer applies, its two hunks are small
enough to re-apply by hand. Only the pinned commit is known to build.

### Flash the XIAO

Plug the **XIAO's own USB-C** into your computer — it can stay wired to the Lyra — and:

```bash
python flash.py
```

It pulses the application port at 1200 baud, the Arduino-style touch that drops the board
into its bootloader, then looks for a UF2 drive and copies the firmware onto it. It only
ever touches Seeed's USB vendor id, never Adafruit's, so another nRF52840 board on the same
bench is left alone. If the touch doesn't take, **double-tap the XIAO's reset button** and
run it again.

Two things it refuses to guess. A drive letter is not an identity — when one board leaves
the USB bus another can inherit its letter — so it reads `INFO_UF2.TXT` and declines a
volume whose model is not a XIAO, and declines **SoftDevice S140 6.1.1**, which wants the
application at `0x26000` while this firmware links for S140 v7 at `0x27000` (it would flash
and then not boot). `--force` overrides both, if you know better than it does.

**If no UF2 drive appears at all, that is not necessarily a fault.** On the bench where this
was developed the bootloader came up as a serial port with no mass-storage interface
whatsoever, so there was nothing to copy a file onto. `flash.py` recognises the bootloader's
serial port — on the plain XIAO and on the Sense — and prints the serial-DFU command to run
from your MeshCore checkout instead:

```bash
pio run -e Xiao_nrf52_companion_radio_serial -t upload --upload-port <bootloader port>
```

That is the route that worked: it ends in `Device programmed.` and the XIAO reboots itself
into the radio firmware. Afterwards the XIAO's USB serial port goes **silent to companion
frames** — the companion now lives on D6/D7, not on USB. That silence is correct.

### Set up the Lyra

Copy `scripts/` to the Lyra and, as root:

```bash
sh scripts/xiao-radio/lyra-setup.sh          # MT_USER=meshterm by default
```

It does three things. It installs [`uart1-mux.py`](../scripts/xiao-radio/uart1-mux.py) as
`/usr/local/bin/uart1-radio-mux.py` behind a oneshot `uart1-radio-mux.service` that runs on
every boot; it adds `$MT_USER` to `dialout` (a fresh login is needed before that counts);
and, **only if the file is absent**, it writes a MeshTerm profile:

```toml
default_profile = "picocalc"
[profiles.picocalc]
port = "/dev/ttyS1"
baudrate = 115200
transport = "serial"
default_tx_power = 22
description = "XIAO nRF52840 + SX1262 via Lyra UART1 on GP4/GP5"
```

If a `config.toml` is already there it prints those lines for you to paste rather than
touching your configuration. Because `default_profile` is set, a bare `meshterm` connects
straight to the radio.

The mux service is the interesting half. **Calculinux enables the UART1 controller but wires
it to no pad**, which is why `/dev/ttyS1` exists and yet nothing ever comes out of it. The
RK3506's matrix IO can carry UART1 on almost any pad, so `uart1-mux.py` makes three 32-bit
writes through a one-page `/dev/mem` mapping: two RMIO signal-select words that put UART1-TX
on `gpio0-0` and UART1-RX on `gpio0-1`, and one PMU IOC word that switches those two pins
into matrix mode. The writes are idempotent, which is why a oneshot at boot is enough, and
they need root and a kernel that permits `/dev/mem` writes to that range. Choosing those two
pads is what saves the front SD-card slot: UART1's *default* pads belong to SPI1, which
drives the slot, so the mux avoids them entirely.

> The register addresses, the signal ids and the claim that `gpio0-0`/`gpio0-1` are header
> GP4/GP5 come from the maintainer's live probing of one board. They are **not verified
> against an RK3506 reference manual**, and neither is the assumption that this kernel allows
> those `/dev/mem` writes. See [What is verified and what is
> not](#what-is-verified-and-what-is-not).

### Run it, and check the link

```bash
meshterm
```

Your node should come up with whatever name the firmware adverts — the device page and the
dashboard header both show it. If you want to prove the wire itself before blaming anything
else, route the mux, open `/dev/ttyS1` at 115200 as root and send an `APP_START` frame
(`3c 0e 00 01`, seven zero bytes, then a name); a live radio answers with a framed `05`,
`SELF_INFO`. The frame's command byte and reply code are the companion protocol's; the
`<`/`>` plus little-endian length framing around them is from the firmware and is worth
confirming against it rather than taking from here.

### Troubleshooting

| Symptom | Check |
| --- | --- |
| `meshterm` can't open the port | Is `MT_USER` in `dialout`? (`groups`) — it needs a fresh login after setup. |
| Port opens but nothing is there | Re-flash with the **`_serial`** environment, not `_ble` or `_usb`; only that one defines `SERIAL_RX`/`SERIAL_TX`. The XIAO's USB serial should be *silent* to companion frames. |
| Still silent, wiring confirmed | Confirm the firmware carries the **I²C remap** (`PIN_WIRE_SCL=16`, `PIN_WIRE_SDA=17`). This is the failure that looks like bad wiring and isn't. |
| Flashed fine, board never comes back | Wrong board, or the wrong SoftDevice — `INFO_UF2.TXT` must say a XIAO and **S140 v7**, not 6.1.1. `flash.py` refuses both without `--force`. |
| `flash.py` finds no UF2 drive | Expected on a CDC-only bootloader. Use the `pio … -t upload --upload-port` line it prints. |
| Nothing on `/dev/ttyS1` after a reboot | `systemctl status uart1-radio-mux` first, then `journalctl -b -u uart1-radio-mux` — the poke must run every boot, and a oneshot that failed says why. |
| MeshTerm says the radio is unplugged | Use a MeshTerm build with the soldered-UART liveness fix: pyserial's port enumeration doesn't list platform-bus ports like `/dev/ttyS1`, so liveness stats the device node instead. It is in this repo. |

---

## The SPI radio bridge

On some boards the LoRa chip hangs straight off the host's SPI bus — the ClockworkPi
uConsole with the hackergadgets AIO, Waveshare LoRa HATs, and similar. There is **no
companion microcontroller and no firmware to flash**, so there is no node for MeshTerm to
talk to: the mesh node itself has to run in software on the host, through the `pymc_core`
library (which on the uConsole arrives with the `meshcore-uconsole` package, alongside its
own `meshcore-console` GUI).

[`scripts/meshterm-spi-bridge`](../scripts/meshterm-spi-bridge) fills that gap. It runs the
software node and puts the **standard companion protocol in front of it on a local TCP
port**, so

```bash
meshterm --tcp 127.0.0.1:5000
```

connects exactly as if a companion were plugged in. One file is both halves: a setup menu
that runs under any Python 3, and the bridge process itself, which the menu re-execs under
whichever interpreter has the radio library. That split is deliberate — the library lives in
a packaged virtual environment, and you should never need to know that.

It runs on the radio host as **your normal user, never root**: the node identity and the
service are per-user.

### Before you start: the runtime rename

> **On a current uConsole install this bridge does not start, and the fix is a port nobody
> has done yet.** It was written against **`pymc_core` 1.0.12** and every one of its
> interfaces was checked against that release. Since then the library was renamed: the
> `pyMC_core` project became `openhop_core`, PyPI's `pymc-core` stopped at 1.0.12 in May
> 2026, and the current `meshcore-uconsole` package depends on `openhop-core` instead. That
> virtual environment has no `pymc_core` module at all, and the helper module the bridge
> imports from now exports a differently named entry point. What you will see is the
> bridge's own preflight reporting the runtime as **not found**, and nothing running.
>
> That is the whole of what has been verified: the rename, the package dependency, and that
> the preflight therefore fails. Whether the bridge's three compatibility shims are still
> needed under the new library, and what else moved inside it, has **not** been checked. If
> you are on an older, pre-rename install — `pymc-core` 1.0.x — everything below applies as
> written.

### Setting it up

```bash
./meshterm-spi-bridge          # the interactive setup menu
./meshterm-spi-bridge --check  # run the prerequisite checks and exit
./meshterm-spi-bridge --run    # run the bridge now, in this terminal
```

The preflight is worth running first. It finds the interpreter that has the radio library,
checks that a `/dev/spidev*` node and `/dev/gpiochip0` are readable and writable by you —
the uConsole needs `dtoverlay=spi1-1cs` in `/boot/firmware/config.txt` for the first, and
membership of the `spi` and `gpio` groups for both — and tells you if the GUI is holding the
radio or if something is already listening on the port.

The menu then offers to **run the bridge in the foreground** (Ctrl+C returns to the menu),
to **install it as a service**, to **uninstall** it, or to show its status. Installing copies
the script to `~/.local/bin/` and writes a `systemd --user` unit that restarts on failure and
starts with your session; it also asks systemd to enable lingering for your user, which is
what makes the service come up at boot rather than at your first login, and prints the `sudo`
command if it cannot. Either way it offers to add a MeshTerm profile for you:

```toml
[profiles.bridge]
host = "127.0.0.1"
tcp_port = 5000
description = "Local SPI radio via meshterm-spi-bridge"
```

It sets no `default_profile`, so a bare `meshterm` offers the bridge in the picker rather
than connecting to it behind your back. Uninstalling removes the service and the copied
script and nothing else: it never touches the `meshcore-console` or radio-library packages,
and it deliberately leaves your identity and contact files, and the lingering setting, where
they are.

### Identity, and one program at a time

**Only one program may use the radio at a time.** Run *either* the `meshcore-console` GUI or
this bridge, never both — the preflight warns you when the GUI is up, and refuses to start a
second bridge over a running service.

Which node you are depends on what is installed. Where the `meshcore-console` stack is
present the bridge loads that stack's identity key, so **MeshTerm is the same node as the
GUI** and your contacts and your public key follow you between them. On a library-only
install it mints its own key the first time and persists it under
`~/.local/share/meshterm-spi-bridge/`, so the node is stable across restarts. Contacts are
snapshotted to a file in that same directory on every change, because the library's contact
store is memory-only — without that, direct messages to a contact you added before a restart
came back "not found".

### Hardware knobs

The defaults match the hackergadgets uConsole AIO: SPI bus 1, chip-select 0, reset on 25,
busy on 24, IRQ on 26, 910.525 MHz, 22 dBm, SF7, 62.5 kHz, coding rate 4/5. For another SPI
board, export what differs before launching.

| Variable | What it sets |
| --- | --- |
| `MESHCORE_BUS_ID`, `MESHCORE_CS_ID`, `MESHCORE_CS_PIN` | which SPI bus, device and chip-select pin the radio is on |
| `MESHCORE_RESET_PIN`, `MESHCORE_BUSY_PIN`, `MESHCORE_IRQ_PIN` | the three control lines |
| `MESHCORE_FREQUENCY`, `MESHCORE_TX_POWER` | frequency in Hz, power in dBm |
| `MESHCORE_SPREADING_FACTOR`, `MESHCORE_BANDWIDTH`, `MESHCORE_CODING_RATE` | the modem preset — all three must match the mesh you are joining |
| `MESHTERM_PYMC_PYTHON` | force a specific interpreter instead of letting the bridge discover one |

Where the `meshcore-console` stack is installed, the bridge builds the radio through *its*
hardware configuration, which reads a longer list of variables — transmit/receive enable
pins, the GPIO chip, the Waveshare and DIO options, and the enable pins an AIO v2 needs to
power its radio at all. Those come from that package's documentation. On a library-only
install the table above is the whole of it, so an AIO v2 without that package present may
not power its radio.

### What works through the bridge

The bridge exposes the companion protocol as the radio library implements it, plus two
pushes of its own. In MeshTerm's terms: messaging, contacts, channels, adverts, device
configuration, traces and the live packet feed all work. Three things do not, because the
library has no handler for them — **reboot and factory reset have no effect, and a device
PIN cannot be set**. MeshTerm reports the device model as `pyMC-spi-bridge`, which is a
useful way to tell a bridged node from a real companion in the logs.

Traces and the live feed reach MeshTerm through compatibility shims the bridge installs on
the library: it subscribes to raw packets to feed the live feed, reassembles a completed
trace reply and pushes it, and relaxes an acknowledgement length check that newer firmware
trips by appending bytes after the CRC. None of that is MeshTerm's protocol being bent —
the library simply never registered those two pushes — and upstream wiring would make the
shims redundant. Their **on-air behaviour was validated on one bench**, against the radios
that were in range of it.

---

## The unfinished UART-to-TCP pump

[`calculinux-radio-bridge.sh`](../scripts/calculinux-radio-bridge.sh) is a work in progress
and is not a supported route. It was written to put the UART-attached XIAO radio on a TCP
port so other machines could reach it, before the direct-serial path above existed, and it
is superseded by `lyra-setup.sh` for on-device use. As it stands its own header describes a
pin-routing phase that the body does not contain, on a theory of which pads carry UART1 that
`uart1-mux.py` later replaced; it binds to every interface by default, unauthenticated; and
it writes a competing default profile into the same `config.toml` that `lyra-setup.sh`
writes. Read it if you want the shape of a UART-to-TCP pump — a single-client pump is the
right shape, since companion frames from two clients would interleave — but don't run it
expecting a finished tool.

---

## What is verified and what is not

Most of what this manual explains is checked in code: the platform spec, the font and
palette contracts (a test parses the font script itself and pins it to MeshTerm's glyph
inventory), the profile keys the scripts write, the soldered-UART liveness fix, and every
interface the SPI bridge calls in `pymc_core` 1.0.12. The upstream firmware state is checked
too: the pinned MeshCore commit exists, the patch's two hunks are still needed on `dev`
today, and the nRF52840 UF2 family id and both XIAO bootloader ids match Adafruit's board
files.

The rest is one maintainer's bench, reported honestly rather than dressed up. None of the
following is confirmable from code in this repository, and a second device may disagree:

- **The RK3506 register map** in `uart1-mux.py` — the two register blocks, the UART1 signal
  ids, matrix mode, and that `gpio0-0`/`gpio0-1` are header GP4 and GP5. Probed live on one
  board, not read from a reference manual.
- **That the Calculinux kernel permits those `/dev/mem` writes**, and that the mux oneshot
  really does run at every boot.
- **The PicoCalc header pin numbers** 6, 7, 8 and 36 for GP4, GP5, GND and 3V3 on the Lyra
  carrier. The Pico-side numbering is right; the carrier's wiring is a bench observation.
- **The Calculinux specifics the scripts assert**: the stripped Python and the package names
  that fix it, which overlays are writable, that `/tmp` is a small RAM tmpfs, the rtl8xxxu
  cold-boot race with iwd, the opkg feed's occasional 404, the absolute `ip` and `iw` paths,
  that the stock Terminus console font is installed, and that `kbd` brings `setvtrgb`.
- **That this board has no RTC.** Only the script says so, and everything about the clock
  phase follows from it.
- **The XIAO bootloader that exposed no mass storage**, and the S140 v7 / `0x27000`
  requirement — public references agree, but neither is a primary source.
- **Whether the firmware patch still applies at MeshCore `dev` HEAD.** It is unmerged; only
  the pinned commit is known to build.
- **MeshCore's serial framing** around the raw `APP_START` check above. The command byte and
  the `SELF_INFO` reply code are verified against the protocol documentation; the framing
  bytes are not.
- **Everything on-air about the SPI bridge** — the per-hop trace semantics it reassembles and
  the acknowledgement bytes "newer firmware" appends. The code is internally consistent with
  the library's API; the wire behaviour was seen on one mesh.
- **Whether the renamed radio library still needs the bridge's three shims**, and where the
  current package installs itself.
- **The framebuffer console's 512-glyph cap.** Well known, taken as given, not re-measured
  here.

If you have the hardware and any of it is wrong on your bench, that is worth an issue — the
scripts encode a set of discoveries, and a second device is the only way to learn which of
them were about the hardware and which were about one unit.
