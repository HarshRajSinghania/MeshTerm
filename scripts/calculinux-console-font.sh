#!/bin/sh
# calculinux-console-font.sh -- build, install, and persist the "meshterm" console fonts
# and the 16-slot palette.
#
# MeshTerm runs on a Luckfox Lyra inside a ClockworkPi PicoCalc under Calculinux, drawing
# to the bare ILI9488 framebuffer console (fbcon). That console loads a single PSF font,
# and every font Calculinux ships is a classic 256-glyph VGA font -- so out of the box the
# panel cannot draw what MeshTerm's UI leans on hardest: braille charts (U+2800-U+28FF),
# the node/status marks, the P3 compact-icon marks, rounded panel corners, or the list
# cursor. This script synthesizes TWO 512-glyph PSF2 fonts:
#
#   * meshterm.psf.gz   6x12 (Terminus base)     -> 53x26 -- the installed default
#   * meshterm8.psf.gz  6x8  (kernel font_6x8)   -> 53x40 -- the A/B candidate
#
# Flip live with `setfont /usr/share/consolefonts/meshterm8.psf.gz` (and back with
# meshterm.psf.gz); persist the winner via FONT= in /etc/vconsole.conf.
#
# Why 512 and not more: fbcon caps a font at 512 glyphs. Braille alone is 256 and the base
# set is another 256 -- the budget is already full, so every extra mark is drawn into a
# DONOR slot (a pictograph MeshTerm never emits) whose codepoint is repointed. Choosing a
# donor means knowing its slot's FULL codepoint list: bases alias lookalikes onto one glyph
# (donating pi once erased Cyrillic pe), so a donor miss aborts the build and a keeper list
# is verified after it. The rounded corners and the midline ellipsis cost no glyph: they
# are aliased onto existing bitmaps.
#
# The 6x8 base is the Linux kernel's own font_6x8 (lib/fonts/font_6x8.c, GPL-2.0),
# embedded below -- CP437 coverage, so unlike the Terminus base it has NO Cyrillic;
# its keeper list drops the Cyrillic canary accordingly.
#
# Everything else is pure geometry, generated on-device; only the stock Terminus font and
# python3 are required. Run as root on the Lyra (serial console is fine):
#
#     sh calculinux-console-font.sh
#
# Idempotent -- safe to re-run after a MeshTerm update or a font tweak. Also installs
# /etc/vtrgb + the meshterm-vtrgb boot oneshot (the 16-slot palette; see
# meshterm/ui/theme._VT_SLOTS -- a repo test pins the two to each other).
set -eu

FONT_NAME=meshterm
CONSOLEFONTS=/usr/share/consolefonts
BASE="$CONSOLEFONTS/ter-u12n.psf.gz"          # stock Terminus 6x12, 256 glyphs
OUT="$CONSOLEFONTS/$FONT_NAME.psf.gz"          # the 6x12 default
OUT8="$CONSOLEFONTS/${FONT_NAME}8.psf.gz"      # the 6x8 A/B candidate
VCONSOLE=/etc/vconsole.conf

# --- preflight: fail early with a plain reason, never half-apply -----------------------
[ "$(id -u)" = 0 ] || { echo "error: run as root (writes $CONSOLEFONTS and $VCONSOLE)" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 1; }
command -v setfont >/dev/null 2>&1 || { echo "error: setfont not found (install kbd tools)" >&2; exit 1; }
[ -f "$BASE" ] || { echo "error: base font not found: $BASE" >&2; exit 1; }

# --- build both fonts ------------------------------------------------------------------
# The generator is inlined (quoted heredoc, so the shell expands nothing) and reads its
# paths from the environment. It is the single source of truth for the synthesized glyphs.
echo "building $OUT (6x12) and $OUT8 (6x8) ..."
BASE="$BASE" OUT="$OUT" OUT8="$OUT8" python3 - <<'PYEOF'
import base64, os, struct, gzip

BASE = os.environ["BASE"]
OUT = os.environ["OUT"]
OUT8 = os.environ["OUT8"]
PSF2_MAGIC = 0x864AB572

# --- braille: 2-wide dot grid; the codepoint's low byte says which dots lit ------------
COLS = [[1, 2], [4, 5]]
BIT = {0: (0, 0), 1: (0, 1), 2: (0, 2), 6: (0, 3),
       3: (1, 0), 4: (1, 1), 5: (1, 2), 7: (1, 3)}

#: Dot-row bands per cell height: 3px pitch at 6x12, 2px at 6x8.
BANDS12 = [[0, 1], [3, 4], [6, 7], [9, 10]]
BANDS8 = [[0, 1], [2, 3], [4, 5], [6, 7]]


def braille_glyph(value, bands, height):
    rows = [0] * height
    for bit in range(8):
        if value >> bit & 1:
            col, row = BIT[bit]
            for x in COLS[col]:
                for y in bands[row]:
                    rows[y] |= 0x80 >> x
    return bytes(rows)


# --- marks + cursors, drawn as pixel art ('#' lit) -------------------------------------
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
    # --- P3 marks: the compact icon language (see meshterm/ui/theme._GLYPH_MAP and
    # --- meshterm/ui/fontset.py, which must mirror every codepoint drawn here) --------
    0x2026: art([  # HORIZONTAL ELLIPSIS -- "opens further prompts", truncation
        "......", "......", "......", "......", "......", "......",
        "......", "......", "#.#.#.", "#.#.#.", "......", "......"]),
    0x26A0: art([  # WARNING SIGN -- the warn status mark
        "......", "..##..", ".#..#.", ".#..#.", "#....#", "#.##.#",
        "#.##.#", "#....#", "#.##.#", "######", "......", "......"]),
    0x232B: art([  # ERASE TO THE LEFT -- the backspace key in footer hints
        "......", "......", "......", "..####", ".#...#", ".##.##",
        "#..#.#", ".##.##", ".#...#", "..####", "......", "......"]),
    0x21E7: art([  # UPWARDS WHITE ARROW -- the shift key in footer hints
        "......", "......", "..##..", ".#..#.", "#....#", "##..##",
        ".#..#.", ".#..#.", ".#..#.", ".####.", "......", "......"]),
    0x2699: art([  # GEAR -- parameter/config
        "......", "......", "..##..", ".####.", "######", "##..##",
        "##..##", "######", ".####.", "..##..", "......", "......"]),
    0x21BB: art([  # CLOCKWISE OPEN CIRCLE ARROW -- re-read/refresh
        "......", "....#.", ".#####", "#...#.", "#.....", "#.....",
        "#.....", "#....#", ".####.", "......", "......", "......"]),
    0x25F7: art([  # WHITE CIRCLE UPPER RIGHT QUADRANT -- the clock face (sync/time)
        "......", "......", ".####.", "#..#.#", "#..#.#", "#..###",
        "#....#", "#....#", ".####.", "......", "......", "......"]),
    0x2316: art([  # POSITION INDICATOR -- the crosshair (trace/map position)
        "......", "..##..", "..##..", "......", "......", "#.##.#",
        "#.##.#", "......", "......", "..##..", "..##..", "......"]),
    0x26BF: art([  # SQUARED KEY (drawn as a padlock) -- private channel
        "......", "......", ".####.", ".#..#.", "######", "######",
        "##..##", "##..##", "######", "######", "......", "......"]),
}

# The same 18 marks redrawn for the 6x8 cell (first-draft art; the P5 tweak round and
# JP's eyeball pass refine whichever font wins the A/B).
MARKS8 = {
    0x25CF: art([  # BLACK CIRCLE
        "......", "..##..", ".####.", "######", "######", ".####.", "..##..", "......"]),
    0x25C9: art([  # FISHEYE
        "......", "..##..", ".#..#.", "#.##.#", "#.##.#", ".#..#.", "..##..", "......"]),
    0x2605: art([  # BLACK STAR
        "......", "..#...", "..#...", "######", ".####.", "..##..", ".#..#.", "......"]),
    0x2014: art([  # EM DASH
        "......", "......", "......", "######", "######", "......", "......", "......"]),
    0x2713: art([  # CHECK MARK
        "......", "......", ".....#", "....#.", "#..#..", ".##...", ".#....", "......"]),
    0x2717: art([  # BALLOT X
        "......", "#...#.", ".#.#..", "..#...", ".#.#..", "#...#.", "......", "......"]),
    0x25B6: art([  # RIGHT-POINTING TRIANGLE
        "#.....", "##....", "###...", "####..", "####..", "###...", "##....", "#....."]),
    0x276F: art([  # HEAVY RIGHT ANGLE QUOTE -- the list cursor
        "##....", ".##...", "..##..", "...##.", "..##..", ".##...", "##....", "......"]),
    0x25B8: art([  # SMALL RIGHT-POINTING TRIANGLE -- reorder cursor
        "......", "......", ".#....", ".##...", ".###..", ".##...", ".#....", "......"]),
    0x2026: art([  # HORIZONTAL ELLIPSIS
        "......", "......", "......", "......", "......", "#.#.#.", "#.#.#.", "......"]),
    0x26A0: art([  # WARNING SIGN
        "..##..", ".#..#.", "#.##.#", "#.##.#", "#....#", "#.##.#", "######", "......"]),
    0x232B: art([  # ERASE TO THE LEFT
        "......", "..####", ".##.##", "#..#.#", ".##.##", "..####", "......", "......"]),
    0x21E7: art([  # UPWARDS WHITE ARROW -- shift
        "..##..", ".#..#.", "#....#", "##..##", ".#..#.", ".#..#.", ".####.", "......"]),
    0x2699: art([  # GEAR
        "......", "..##..", "######", "##..##", "##..##", "######", "..##..", "......"]),
    0x21BB: art([  # CLOCKWISE OPEN CIRCLE ARROW -- refresh
        "....#.", ".#####", "#...#.", "#.....", "#.....", "#....#", ".####.", "......"]),
    0x25F7: art([  # CLOCK FACE
        "......", ".####.", "#..#.#", "#..###", "#....#", "#....#", ".####.", "......"]),
    0x2316: art([  # POSITION INDICATOR -- crosshair
        "..##..", "......", "#.##.#", "#.##.#", "......", "..##..", "......", "......"]),
    0x26BF: art([  # SQUARED KEY -- padlock
        ".####.", ".#..#.", "######", "##..##", "##..##", "######", "######", "......"]),
}

# Donor codepoints whose glyph slots we may repurpose (glyphs MeshTerm never draws).
# Order matters: MARKS consume donors front to back, one each. See the header comment
# for the shared-slot trap; the tail donors double as MAP TARGETS in meshterm/ui/theme
# (advert / message / data / packet / control marks) and are last-resort spares only.
# NOTE 266C: in the Terminus base it shares 266B's slot (a freebie, never a donor of its
# own -- listing it would abort); it is NOT in this list for that reason.
DONORS = [0x263A, 0x263B, 0x2665, 0x2666, 0x2663, 0x2660, 0x25D8, 0x25D9, 0x266A,
          0x266B, 0x203C, 0x2640, 0x2642, 0x2320, 0x2321, 0x00F7, 0x2552, 0x2558,
          0x2559, 0x255B, 0x255E, 0x255F, 0x2561, 0x2567, 0x2568, 0x256A,
          0x263C, 0x00B6, 0x00A7, 0x25AC, 0x21A8]

# Rounded panel corners aliased onto the existing square corners (no new bitmap), and the
# midline ellipsis onto the baseline mark.
ALIASES = {
    0x256D: 0x250C,  # rounded top-left     -> square top-left
    0x256E: 0x2510,  # rounded top-right    -> square top-right
    0x256F: 0x2518,  # rounded bottom-right -> square bottom-right
    0x2570: 0x2514,  # rounded bottom-left  -> square bottom-left
    0x22EF: 0x2026,  # midline ellipsis     -> ellipsis mark
}

# Codepoints that must survive both builds: the theme's base-font map targets.
KEEP_COMMON = [0x263C, 0x00B6, 0x00A7, 0x25AC, 0x21A8]


def build(glyphs, entries, charsize, height, marks, bands, keep_extra, out_path):
    """Append braille, draw marks into donors, alias, verify, and write one PSF2."""
    glyphs = [bytearray(g) for g in glyphs]
    entries = list(entries)
    glyphs += [bytearray(braille_glyph(v, bands, height)) for v in range(256)]
    entries += [chr(0x2800 + i).encode("utf-8") for i in range(256)]

    def slot_of(cp):
        needle = chr(cp).encode("utf-8")
        for i, e in enumerate(entries):
            if needle in e:
                return i
        return None

    donors = list(DONORS)
    for cp, bitmap in marks.items():
        if not donors:
            raise SystemExit("out of donor slots for U+%04X" % cp)
        donor = donors.pop(0)
        slot = slot_of(donor)
        if slot is None:
            raise SystemExit(
                "donor U+%04X (for mark U+%04X) not found in the base font -- "
                "fix DONORS instead of skipping" % (donor, cp))
        padded = bytearray(bitmap)
        padded += bytes(charsize - len(padded))
        glyphs[slot] = padded
        entries[slot] = chr(cp).encode("utf-8")

    for new_cp, existing_cp in ALIASES.items():
        slot = slot_of(existing_cp)
        if slot is None:
            raise SystemExit("no glyph for U+%04X to alias U+%04X onto" % (existing_cp, new_cp))
        entries[slot] += chr(new_cp).encode("utf-8")

    for cp in list(marks) + list(ALIASES) + KEEP_COMMON + keep_extra:
        if slot_of(cp) is None:
            raise SystemExit("build ate U+%04X -- a donor slot carried it; fix DONORS" % cp)

    header = struct.pack("<IIIIIIII", PSF2_MAGIC, 0, 32, 1, 512, charsize, height, 6)
    out = header + b"".join(bytes(g) for g in glyphs) + b"".join(e + b"\xff" for e in entries)
    gzip.open(out_path, "wb").write(out)
    print("  wrote %s: 512 glyphs (6x%d), +%d marks, +%d aliases"
          % (out_path, height, len(marks), len(ALIASES)))


# --- 6x12: the Terminus base -----------------------------------------------------------
base = gzip.open(BASE, "rb").read()
magic, ver, hsize, flags, length, charsize, h, w = struct.unpack("<IIIIIIII", base[:32])
assert magic == PSF2_MAGIC and (length, charsize, h, w) == (256, 12, 12, 6), \
    "base is not the expected Terminus 6x12 PSF2 (%r)" % ((length, charsize, h, w),)
glyphs12 = [base[32 + i * charsize:32 + (i + 1) * charsize] for i in range(256)]
entries12 = base[32 + 256 * charsize:].split(b"\xff")[:256]
# Cyrillic pe + pi: the shared-slot canaries (donating pi once erased pe).
build(glyphs12, entries12, 12, 12, MARKS, BANDS12, [0x043F, 0x03C0], OUT)

# --- 6x8: the kernel's font_6x8 (CP437 coverage -- no Cyrillic) ------------------------
FONT8 = base64.b64decode("""
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
""")
assert len(FONT8) == 2048
glyphs8 = [FONT8[i * 8:(i + 1) * 8] for i in range(256)]
# CP437's graphics mapping: the control range 0x00-0x1F holds pictographs, 0x7F a house;
# the rest decodes through Python's cp437 codec.
CP437_LOW = [
    0x0000, 0x263A, 0x263B, 0x2665, 0x2666, 0x2663, 0x2660, 0x2022,
    0x25D8, 0x25CB, 0x25D9, 0x2642, 0x2640, 0x266A, 0x266B, 0x263C,
    0x25BA, 0x25C4, 0x2195, 0x203C, 0x00B6, 0x00A7, 0x25AC, 0x21A8,
    0x2191, 0x2193, 0x2192, 0x2190, 0x221F, 0x2194, 0x25B2, 0x25BC,
]
entries8 = []
for i in range(256):
    if i < 0x20:
        cp = CP437_LOW[i]
    elif i == 0x7F:
        cp = 0x2302
    else:
        cp = ord(bytes([i]).decode("cp437"))
    entries8.append(chr(cp).encode("utf-8") if cp else b"")
build(glyphs8, entries8, 8, 8, MARKS8, BANDS8, [0x03C0], OUT8)
PYEOF

# --- apply live: setfont re-renders the whole console immediately -----------------------
TTY=/dev/tty1
[ -c "$TTY" ] || TTY=/dev/tty0
echo "applying $OUT (6x12, the default) to $TTY ..."
setfont -C "$TTY" "$OUT"

# --- persist: systemd-vconsole-setup reads FONT= from vconsole.conf at every boot -------
if [ -f "$VCONSOLE" ] && grep -q '^FONT=' "$VCONSOLE"; then
    sed -i "s/^FONT=.*/FONT=$FONT_NAME/" "$VCONSOLE"
else
    echo "FONT=$FONT_NAME" >> "$VCONSOLE"
fi
echo "persisted FONT=$FONT_NAME in $VCONSOLE (loads on every boot)"
echo "A/B: 'setfont $OUT8' for 53x40, 'setfont $OUT' for 53x26; persist the winner in $VCONSOLE"

# --- palette: program the 16 console slots to MeshTerm's colours ------------------------
# The panel's VT layer is 16 fg / 8 bg palette slots -- no per-cell RGB -- so MeshTerm's
# 16-slot theme (meshterm/ui/theme.MESH_THEME_16) is designed against the slot meanings
# below, and this section programs the slots' actual RGB values. The palette file matches
# theme.vtrgb_lines() exactly (a test keeps them in sync); the boot oneshot re-applies it
# every start (same pattern as wifi-kick). setvtrgb does the work where kbd ships it; the
# fallback printf speaks the kernel VT's own OSC palette sequences, so nothing is required.
VTRGB=/etc/vtrgb
APPLIER=/usr/local/sbin/meshterm-vtrgb
UNIT=/etc/systemd/system/meshterm-vtrgb.service

echo "writing $VTRGB ..."
cat > "$VTRGB" <<'EOF'
15,239,34,245,99,51,100,203,148,248,74,251,129,165,94,255
23,68,197,158,102,65,116,213,163,113,222,191,140,180,234,255
42,68,94,11,241,85,139,225,184,113,128,36,248,252,212,255
EOF

mkdir -p "$(dirname "$APPLIER")"
cat > "$APPLIER" <<'EOF'
#!/bin/sh
# Apply the MeshTerm console palette (slots documented in meshterm/ui/theme._VT_SLOTS).
if command -v setvtrgb >/dev/null 2>&1; then
    exec setvtrgb /etc/vtrgb
fi
# No kbd setvtrgb: the kernel VT accepts its own OSC palette sequences (ESC ] P n rrggbb).
TTY=${1:-/dev/tty1}
printf '\033]P00f172a\033]P1ef4444\033]P222c55e\033]P3f59e0b\033]P46366f1\033]P5334155\033]P664748b\033]P7cbd5e1\033]P894a3b8\033]P9f87171\033]Pa4ade80\033]Pbfbbf24\033]Pc818cf8\033]Pda5b4fc\033]Pe5eead4\033]Pfffffff' > "$TTY"
EOF
chmod +x "$APPLIER"

cat > "$UNIT" <<EOF
[Unit]
Description=MeshTerm console palette (16-slot vtrgb)
After=systemd-vconsole-setup.service

[Service]
Type=oneshot
ExecStart=$APPLIER
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable meshterm-vtrgb.service >/dev/null 2>&1 || true
"$APPLIER" || echo "warning: could not apply the palette live (boot service will)"
echo "palette installed ($VTRGB + boot oneshot)"

echo "done -- launch 'meshterm' to see braille charts, node glyphs, framed panels, and the > cursor."
