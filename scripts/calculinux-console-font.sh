#!/bin/sh
# calculinux-console-font.sh -- build, install, and persist the "meshterm" console font.
#
# MeshTerm runs on a Luckfox Lyra inside a ClockworkPi PicoCalc under Calculinux, drawing
# to the bare ILI9488 framebuffer console (fbcon). That console loads a single PSF font,
# and every font Calculinux ships is a classic 256-glyph VGA font -- so out of the box the
# panel cannot draw the four things MeshTerm's UI leans on hardest:
#
#   * braille charts        U+2800-U+28FF  (256 codepoints -- a whole block on their own)
#   * node / status marks   * filled circle, fisheye, star, em-dash, check, cross, play
#   * rounded panel corners  Rich's ROUNDED box emits U+256D/E/F U+2570, absent from CP437
#   * the list cursor        U+276F "heavy angle" + U+25B8 small triangle (reorder state)
#
# Missing glyphs render blank, so charts vanish, node rows lose their icons, every window
# frame loses its corners, and the highlighted menu row shows a two-space gap where its
# pointer should be. This script synthesizes all of them and installs one 512-glyph PSF2.
#
# Why 512 and not more: fbcon caps a font at 512 glyphs. Braille alone is 256, and the base
# Terminus set is another 256 -- that already fills the budget exactly. So the marks and
# cursors do NOT grow the table: each is drawn into a DONOR slot (a useless CP437 pictograph
# -- smiley, card suit, music note -- that MeshTerm never emits), whose old codepoint is
# dropped from the Unicode map and repointed at the new glyph. The rounded corners cost no
# glyph at all: a 1-2px curve is meaningless at 6x12, so each rounded corner is aliased onto
# the existing square-corner glyph (an extra codepoint on one entry -- the frame just closes).
#
# Everything is pure geometry, generated on-device, so nothing but the stock Terminus font
# and python3 is required. Run it as root on the Lyra (over the serial console is fine):
#
#     sh calculinux-console-font.sh
#
# It is idempotent -- safe to re-run after a MeshTerm update or a font tweak.
#
# Scope: this covers ONLY the console-font configuration. The rest of the Calculinux bring-up
# (opkg python3-modules/pip, the venv + `pip install -e .`, the Wi-Fi boot-scan kick) is a
# one-time deploy done separately and is not repeated here.
set -eu

FONT_NAME=meshterm
CONSOLEFONTS=/usr/share/consolefonts
BASE="$CONSOLEFONTS/ter-u12n.psf.gz"          # stock Terminus 6x12, 256 glyphs
OUT="$CONSOLEFONTS/$FONT_NAME.psf.gz"          # what we build
VCONSOLE=/etc/vconsole.conf

# --- preflight: fail early with a plain reason, never half-apply -----------------------
[ "$(id -u)" = 0 ] || { echo "error: run as root (writes $CONSOLEFONTS and $VCONSOLE)" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 1; }
command -v setfont >/dev/null 2>&1 || { echo "error: setfont not found (install kbd tools)" >&2; exit 1; }
[ -f "$BASE" ] || { echo "error: base font not found: $BASE" >&2; exit 1; }

# --- build meshterm.psf.gz from the stock Terminus font --------------------------------
# The generator is inlined (quoted heredoc, so the shell expands nothing) and reads its
# paths from the environment. It is the single source of truth for the synthesized glyphs.
echo "building $OUT from $(basename "$BASE") ..."
BASE="$BASE" OUT="$OUT" python3 - <<'PYEOF'
import os, struct, gzip

BASE = os.environ["BASE"]
OUT = os.environ["OUT"]
PSF2_MAGIC = 0x864AB572

# --- braille: 2-wide x 4-tall dot grid; the codepoint's low byte says which dots lit ---
COLS = [[1, 2], [4, 5]]
ROWS = [[0, 1], [3, 4], [6, 7], [9, 10]]
BIT = {0: (0, 0), 1: (0, 1), 2: (0, 2), 6: (0, 3),
       3: (1, 0), 4: (1, 1), 5: (1, 2), 7: (1, 3)}


def braille_glyph(value):
    rows = [0] * 12
    for bit in range(8):
        if value >> bit & 1:
            col, row = BIT[bit]
            for x in COLS[col]:
                for y in ROWS[row]:
                    rows[y] |= 0x80 >> x
    return bytes(rows)


# --- MeshTerm marks + cursors, drawn as 6x12 pixel art ('#' lit) -----------------------
def art(rows):
    return bytes(sum(0x80 >> x for x, ch in enumerate(r) if ch == "#") for r in rows)


MARKS = {
    0x25CF: art([  # BLACK CIRCLE -- node / unread
        "......", "......", "..##..", ".####.", "######", "######",
        "######", "######", ".####.", "..##..", "......", "......"]),
    0x25C9: art([  # FISHEYE -- sensor
        "......", "......", "..##..", ".####.", "##..##", "#.##.#",
        "#.##.#", "##..##", ".####.", "..##..", "......", "......"]),
    0x2605: art([  # BLACK STAR -- you / best
        "......", "..##..", "..##..", "######", ".####.", "..##..",
        ".####.", "##..##", "#....#", "......", "......", "......"]),
    0x2014: art([  # EM DASH -- title separator, full width
        "......", "......", "......", "......", "......", "######",
        "######", "......", "......", "......", "......", "......"]),
    0x2713: art([  # CHECK MARK -- ok
        "......", "......", "......", ".....#", "....#.", "...#..",
        "#.#...", ".#....", "......", "......", "......", "......"]),
    0x2717: art([  # BALLOT X -- error
        "......", "......", "......", "#....#", ".#..#.", "..##..",
        "..##..", ".#..#.", "#....#", "......", "......", "......"]),
    0x25B6: art([  # RIGHT-POINTING TRIANGLE -- run / play
        "......", "#.....", "##....", "###...", "####..", "#####.",
        "#####.", "####..", "###...", "##....", "#.....", "......"]),
    0x276F: art([  # HEAVY RIGHT ANGLE QUOTE -- THE list selection cursor ("> ")
        "......", "##....", ".##...", "..##..", "...##.", "....##",
        "....##", "...##.", "..##..", ".##...", "##....", "......"]),
    0x25B8: art([  # SMALL RIGHT-POINTING TRIANGLE -- grabbed/reorder cursor
        "......", "......", "......", ".#....", ".##...", ".###..",
        ".###..", ".##...", ".#....", "......", "......", "......"]),
}

# Donor codepoints whose glyph slots we may repurpose (CP437 junk MeshTerm never uses).
DONORS = [0x263A, 0x263B, 0x2665, 0x2666, 0x2663, 0x2660, 0x25D8, 0x25D9,
          0x266A, 0x266B, 0x263C, 0x203C, 0x00B6, 0x00A7, 0x25AC, 0x21A8]

# Rounded panel corners aliased onto the existing square corners (no new bitmap).
ALIASES = {
    0x256D: 0x250C,  # rounded top-left     -> square top-left
    0x256E: 0x2510,  # rounded top-right    -> square top-right
    0x256F: 0x2518,  # rounded bottom-right -> square bottom-right
    0x2570: 0x2514,  # rounded bottom-left  -> square bottom-left
}

base = gzip.open(BASE, "rb").read()
magic, ver, hsize, flags, length, charsize, h, w = struct.unpack("<IIIIIIII", base[:32])
assert magic == PSF2_MAGIC and (length, charsize, h, w) == (256, 12, 12, 6), \
    "base is not the expected Terminus 6x12 PSF2 (%r)" % ((length, charsize, h, w),)

# Keep all 256 Terminus glyphs, append 256 procedurally-drawn braille cells.
glyphs = [bytearray(base[32 + i * charsize:32 + (i + 1) * charsize]) for i in range(256)]
glyphs += [bytearray(braille_glyph(v)) for v in range(256)]

# Unicode table: one raw entry per glyph (0xFF never appears inside UTF-8, so it splits clean).
entries = base[32 + 256 * charsize:].split(b"\xff")[:256]
entries += [chr(0x2800 + i).encode("utf-8") for i in range(256)]


def slot_of(cp):
    needle = chr(cp).encode("utf-8")
    for i, e in enumerate(entries):
        if needle in e:
            return i
    return None


# Draw each mark/cursor into a donor slot: overwrite the bitmap, repoint the codepoint.
donors = list(DONORS)
for cp, bitmap in MARKS.items():
    slot = None
    while donors and slot is None:
        slot = slot_of(donors.pop(0))
    if slot is None:
        raise SystemExit("out of donor slots for U+%04X" % cp)
    glyphs[slot] = bytearray(bitmap)
    entries[slot] = chr(cp).encode("utf-8")

# Alias rounded corners onto the square-corner glyphs (extra codepoint, same bitmap).
for new_cp, existing_cp in ALIASES.items():
    slot = slot_of(existing_cp)
    if slot is None:
        raise SystemExit("no glyph for U+%04X to alias U+%04X onto" % (existing_cp, new_cp))
    entries[slot] += chr(new_cp).encode("utf-8")

header = struct.pack("<IIIIIIII", PSF2_MAGIC, 0, 32, 1, 512, charsize, h, w)
out = header + b"".join(bytes(g) for g in glyphs) + b"".join(e + b"\xff" for e in entries)
gzip.open(OUT, "wb").write(out)
print("  wrote 512 glyphs: 256 base + 256 braille, +%d marks/cursors, +%d corner aliases"
      % (len(MARKS), len(ALIASES)))
PYEOF

# --- apply live: setfont re-renders the whole console immediately -----------------------
TTY=/dev/tty1
[ -c "$TTY" ] || TTY=/dev/tty0
echo "applying to $TTY ..."
setfont -C "$TTY" "$OUT"

# --- persist: systemd-vconsole-setup reads FONT= from vconsole.conf at every boot -------
if [ -f "$VCONSOLE" ] && grep -q '^FONT=' "$VCONSOLE"; then
    sed -i "s/^FONT=.*/FONT=$FONT_NAME/" "$VCONSOLE"
else
    echo "FONT=$FONT_NAME" >> "$VCONSOLE"
fi
echo "persisted FONT=$FONT_NAME in $VCONSOLE (loads on every boot)"
echo "done -- launch 'meshterm' to see braille charts, node glyphs, framed panels, and the > cursor."
