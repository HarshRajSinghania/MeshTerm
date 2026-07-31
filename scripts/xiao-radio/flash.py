#!/usr/bin/env python3
"""Flash the built .uf2 onto the XIAO nRF52840 via its UF2 bootloader.

Runs on your DEV MACHINE with the XIAO's own USB-C plugged into it. Works on
Windows / Linux / macOS.

The XIAO enters bootloader mode either by a physical **double-tap of its reset button**
(it mounts as the ``XIAO-SENSE`` drive) or, if it is running app firmware that exposes a
serial port, by the automatic 1200-baud "touch" this script attempts first.

Usage:
    python flash.py                       # flash ./meshcore-xiao-radio.uf2
    python flash.py path/to/firmware.uf2  # flash a specific file
"""
import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def find_uf2_drive():
    """Return the mount path of a UF2 bootloader volume, or None."""
    candidates = []
    if os.name == "nt":
        import string

        candidates = ["%s:\\" % d for d in string.ascii_uppercase]
    else:
        for base in ("/media", "/run/media", "/mnt", "/Volumes"):
            candidates += glob.glob(os.path.join(base, "*"))
            candidates += glob.glob(os.path.join(base, "*", "*"))
    for c in candidates:
        try:
            if os.path.exists(os.path.join(c, "INFO_UF2.TXT")):
                return c
        except OSError:
            continue
    return None


def touch_1200():
    """Best-effort: pulse any Seeed XIAO app serial port at 1200 baud to reset to bootloader."""
    try:
        import serial
        import serial.tools.list_ports as lp
    except Exception:
        return
    for p in lp.comports():
        if "2886" in (p.hwid or ""):  # Seeed Studio USB VID
            try:
                serial.Serial(p.device, 1200).close()
                print("   sent 1200-baud bootloader touch to %s" % p.device)
                time.sleep(0.3)
            except Exception:
                pass


def main():
    uf2 = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "meshcore-xiao-radio.uf2")
    if not os.path.exists(uf2):
        sys.exit("ERROR: firmware not found: %s\n(run build-firmware.sh first)" % uf2)

    print(">> looking for XIAO bootloader drive ...")
    drive = find_uf2_drive()
    if not drive:
        touch_1200()
        for _ in range(30):
            drive = find_uf2_drive()
            if drive:
                break
            time.sleep(1)
    if not drive:
        sys.exit(
            "ERROR: no UF2 bootloader drive found.\n"
            "Double-tap the XIAO's reset button (it mounts as XIAO-SENSE), then re-run."
        )

    print(">> flashing %s -> %s" % (os.path.basename(uf2), drive))
    with open(uf2, "rb") as src:
        data = src.read()
    with open(os.path.join(drive, "firmware.uf2"), "wb") as dst:
        dst.write(data)
        dst.flush()
        os.fsync(dst.fileno())

    # the bootloader reboots into the app once written; the drive disappears
    for _ in range(10):
        if not os.path.exists(os.path.join(drive, "INFO_UF2.TXT")):
            print("DONE. XIAO flashed and rebooting into the radio firmware.")
            return
        time.sleep(1)
    print("DONE (copy complete). If it didn't reboot on its own, tap reset once.")


if __name__ == "__main__":
    main()
