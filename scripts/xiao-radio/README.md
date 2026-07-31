# PicoCalc mesh radio — XIAO nRF52840 over UART

Give a **PicoCalc (Luckfox Lyra, Calculinux)** a real MeshCore LoRa radio by wiring a
**Seeed XIAO nRF52840 + Wio‑SX1262** to the Lyra's hardware UART and running MeshTerm
against it. No BLE, no USB host, no SD‑slot sacrifice, and — with the firmware fixed the
right way — **no re‑soldering dead ends.**

```
 MeshTerm ──serial──> /dev/ttyS1 ──UART1 (GP4/GP5)──> XIAO Serial1 (D6/D7) ──> SX1262 ──))) mesh
 (on the Lyra)         115200 8N1      matrix‑IO mux        companion fw
```

Three steps: **build firmware → flash the XIAO → set up the Lyra.** Each has a script.

---

## 1. Hardware

### Bill of materials
- **Seeed XIAO nRF52840** (plain or **Sense**) stacked with the **Wio‑SX1262** LoRa module
  (the SX1262 uses XIAO pins D1–D5, D8–D10 — already wired inside the kit; nothing to do).
- **Luckfox Lyra** in *Pico* form factor (RK3506, SD‑boot), running **Calculinux** inside a
  **ClockworkPi PicoCalc**.
- Four wires.

### Wiring — four connections
The XIAO's free UART pins go to the PicoCalc's Pico‑style header. **Crossed** TX/RX:

| XIAO pad | → | Lyra header | Physical pin | Carries |
|---|---|---|---|---|
| **D7** (Serial1 RX) | → | **GP4** | pin **6** | Lyra UART1 **TX** → XIAO RX |
| **D6** (Serial1 TX) | → | **GP5** | pin **7** | XIAO **TX** → Lyra UART1 RX |
| **GND** | → | **GND** | pin **8** | common ground |
| **3V3** | → | **3V3 OUT** | pin **36** | power (so it runs without USB) |

> ⚠️ **The Lyra does *not* use Raspberry Pi Pico GPIO numbering.** The 40‑pin header is
> physically Pico‑compatible, but each pad's function is Luckfox's own RK3506 mapping. The
> pins above are verified: GP4 = `gpio0-0`, GP5 = `gpio0-1`. Don't trust a Pico pinout for
> anything else here.

All signals are 3.3 V — no level shifter. There is **no 5 V rail** on this board; power the
XIAO from 3V3 only.

---

## 2. Build the firmware  *(on your dev machine)*

Needs `git` and PlatformIO (`pip install platformio`).

```bash
cd scripts/xiao-radio
sh build-firmware.sh
```

This clones MeshCore, applies [`meshcore-uart1.patch`](meshcore-uart1.patch), builds the
`Xiao_nrf52_companion_radio_usb` environment, and writes **`meshcore-xiao-radio.uf2`**.

The patch makes two changes that are *both* required (see [Why](#why-it-works)):
- binds the companion protocol to **hardware UART1** (`Serial1`, D6/D7) instead of USB;
- moves the **I²C bus off D6/D7** (to internal pins 16/17) so it can't steal the UART pins.

---

## 3. Flash the XIAO  *(on your dev machine)*

Plug the **XIAO's own USB‑C** into your computer (it may stay wired to the Lyra).

```bash
python flash.py
```

It triggers the bootloader and copies the firmware. If it can't, **double‑tap the XIAO's
reset button** (it mounts as the `XIAO-SENSE` drive) and re‑run.

Sanity check: after flashing, the XIAO's USB serial port goes **silent to the companion
protocol** — because the companion now lives on D6/D7, not USB. That silence is correct.

---

## 4. Set up the Lyra  *(on the device, as root)*

Copy `scripts/` to the Lyra and run:

```bash
sh scripts/xiao-radio/lyra-setup.sh          # MT_USER=meshterm by default
```

It installs a **systemd service** that routes UART1 → GP4/GP5 on every boot, adds your user
to `dialout`, and writes a default MeshTerm serial profile for `/dev/ttyS1`.

---

## 5. Run it

```bash
meshterm
```

With the default profile in place, plain `meshterm` connects straight to the radio. You
should see your node come up (ours reports as `Johnputer-Pico`).

---

## Why it works  *(the short version)*

Everything below was the hard part; the scripts encode the answers so you don't repeat it.

- **Calculinux never wires UART1 to a pad.** The controller is enabled (so `/dev/ttyS1`
  exists) but reaches no pin. The RK3506 *matrix IO* can carry UART1 on almost any pad, so
  [`uart1-mux.py`](uart1-mux.py) pokes two register groups over `/dev/mem` to put UART1‑TX on
  `gpio0-0` (GP4) and UART1‑RX on `gpio0-1` (GP5). No SD slot is sacrificed (the stock UART1
  pads are the front SD‑card slot — we avoid them entirely).
- **MeshCore maps I²C onto D6/D7 on the XIAO.** `XiaoNrf52Board::begin()` *and*
  `sensors.begin()` call `Wire` on those exact pins, so the I²C peripheral seizes them and the
  UART goes silent — even with perfect wiring. The patch moves I²C to the internal IMU pins
  (16/17). This was the single reason the link stayed dead through every other fix.
- **MeshTerm liveness for soldered UARTs.** pyserial's `comports()` doesn't enumerate
  platform‑bus ports like `/dev/ttyS1`, so MeshTerm's liveness poll would falsely report the
  radio unplugged. MeshTerm now treats an existing `/dev` character device as present
  (`serial_port_present` in `meshterm/core/connection.py`) — so **use a MeshTerm build that
  includes that fix.**

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `meshterm` can't open the port | Is `MT_USER` in `dialout`? (`groups`) — needs a fresh login after setup. |
| Port opens but no node / silence | Re‑flash with the **`_usb`** firmware (not `_ble`); the BLE build ignores Serial1. Confirm the XIAO's USB serial is *silent* to companion frames. |
| Still silent, wiring confirmed | Confirm the firmware has the **I²C‑remap** patch (`PIN_WIRE_SCL=16`, `PIN_WIRE_SDA=17`). |
| Nothing on `/dev/ttyS1` after reboot | `systemctl status uart1-radio-mux` — the mux must run each boot. |
| Verify the raw link, as root | Route the mux, open `/dev/ttyS1` @115200, send an `APP_START` frame (`3c 0e 00 01` + 7×`00` + name); a good radio replies with a `>`‑framed `05` (SELF_INFO). |

### Updating MeshCore
`build-firmware.sh` pins a tested commit. To track upstream, run it with
`MESHCORE_COMMIT=main`; if the patch no longer applies cleanly, re‑apply the two edits in
[`meshcore-uart1.patch`](meshcore-uart1.patch) by hand — they're two hunks.

---

## Optional: reach the radio from other machines (TCP)
The direct‑serial path above is for MeshTerm **on the PicoCalc itself**. If you also want the
radio on your LAN (`meshterm --tcp <lyra-ip>:5000` from a laptop), add a UART↔TCP pump in
front of `/dev/ttyS1`. That's a separate, optional piece and is not required for on‑device use.
