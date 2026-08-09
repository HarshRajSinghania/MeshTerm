# MeshTerm — working notes for Claude

MeshTerm is a full-screen terminal companion for MeshCore LoRa mesh devices. The UI is a
prompt_toolkit session rendering Rich content (`meshterm/ui/`), tools register into a
menu (`meshterm/tools/`), services run in the background (`meshterm/services/`), and
everything heard is recorded to SQLite (`meshterm/persistence/`).

Run the tests with `python -m pytest -q`. Screens must stay readable at each platform's
`readable_cols` — 72 regular, 53 PicoCalc (see **Platforms** below); the dual-platform
gallery test (`tests/test_gallery.py`) is the enforcement point, and every picocalc case
is a hard gate.

## Git workflow

Commit directly on `main` — do not create topic branches unless explicitly asked (this
is a solo repo; branches just create divergence to merge back later). Commit freely, but
never `git push` unless explicitly asked in that moment.

## UX standards

These are binding. Every new screen, dialog, row, or hint follows them; when you touch an
old one that doesn't, bring it along. The enforcement points live in code — build through
them instead of hand-rolling:

- `ui/menus.py` — `back_rows`, `exit_rows`, `menu_rows`, `lane_row`, `section_heading`,
  `confirm_discard`, `fit_cells`.
- `ui/widgets.py` — `highlighted_hash` (THE key widget — shows a key, lights its hash),
  `format_ago` (prose ages),
  `_format_age` (column ages), `channel_glyph`, `_NODE_GLYPHS`, heard-age heat colouring.
- `ui/theme.py` — `name_style`/`node_style` (per-node hues, hash-derived), `snr_style`,
  the `you` white.

### Lexicon — one term per concept

| Term | Meaning |
|---|---|
| node | any device on the mesh — a radio broadcasting packets; roles: client, repeater, room server. The umbrella term |
| contact | a node your device knows: **discovered** (heard broadcasting, not yet added) or **added** (in the contact list, messageable). Every contact is a node; not every node is a contact |
| heard | received from ("last heard", "first heard") — never "seen" in UX text |
| key | the full fixed-length value — a node's public key, a channel secret |
| hash | the short derived id — a key's first path-hash-mode bytes (the slice `highlighted_hash` lights), a channel hash, a path hop |
| path | an ordered hop spec you compose or force (`a1,3d,…`) |
| route | the concrete node sequence a trace walked or will walk |
| via | prefix for a packet/message's relay chain |
| Back | leave the current screen/list (the only exit word on rows) |
| Quit | leave the app (main menu, device splash) — nowhere else |

Node vs contact — the boundary: **node** is the hardware/participant sense — the map,
mesh walk, heard-nodes, relay hops, graph vertices, and node *types* (client/repeater/room/
sensor) all speak "node". **Contact** is the saved-identity sense — the Contacts screen,
the courier recipient, anything you *address*. The reception/persistence layer
(`observations.node`, `HeardNode`, `heard_nodes()`, `trace_hops.node`) stays "node"; the
sortable list you pick from is `contactlist.py` (`ContactListScreen`/`ContactRow`/
`ContactsSort`/`contacts_table`). The `contacts` tool lists the device's added contacts.

Relative ages: `format_ago` for prose ("now", "5m ago", "never" — never "now ago"),
`_format_age` for aligned columns ("now", "5m").

### Screens and lists

- Every select list ends with the exit group from `back_rows()` / `exit_rows()`: exactly
  one blank `Separator(" ")` line, then the bare word **Back** (no arrow, no icon).
  Editors with staged changes show `✓ Apply n staged changes` over
  `✗ Back — discard staged changes`.
- Grouped-list section headings use `section_heading("Label")` → `── Label ──` accent.
  That is also what makes a heading *sticky* (it pins to the top row while its section
  scrolls, and the ^PgUp/^PgDn jumps step by it), so build them through it — a hand-rolled
  `Separator` is no landmark. Prose written *directly under* a heading (a description of
  what the section holds) is its preamble and pins with it, in order, as each row scrolls
  off; prose after the section's first row labels nothing and never pins.
- Label + description command rows go through `menu_rows` (two cell-aligned lanes,
  description muted). Editor rows (setting/value/description) go through `lane_row`.
- A row that opens further prompts ends with `…`; a row that acts immediately doesn't.
- Empty states are lowercase muted, optionally `— explanation`, never parenthesized.
- Body section headings inside screens: accent title, optional muted `  ·  note`.
- A find-as-you-type screen echoes its live query as `/query` in `warn`, on its own body
  line **directly above whatever the query narrows** — `render.query_line` is the one
  definition (select list, path composer, map, mesh walk). A screen that *also* carries
  the query in its footer hint draws the body line only where the footer isn't drawn
  (`Platform.footer_fkeys`), never both: the query must be visible on every platform, and
  twice on none.

### Titles

- Sentence case, always ("Trace — YUL-Poly", "Nodes", "Path width", "Admin login").
- **No emoji in any screen or dialog title** — icons live in menu/list rows. (A terminal
  that draws an emoji narrower than Rich measures leaves a content-sized dialog's border
  short.)
- `—` (em dash) introduces the subject/qualifier: `Feature — subject`. `·` chains status
  atoms: `Map · z12 · 34 nodes · 2.1 km across`.

### Footer hints

- ≤72 cells. Sentence shape: navigation keys, then action keys, **Esc last**.
- Esc verb by surface: `Esc back` leaves a screen · `Esc close` dismisses a read-only
  floating view · `Esc cancel` abandons a prompt/dialog · `Esc keep` leaves a value
  picker unchanged · `Esc quit` only at the main menu.
- Enter verb by action: `Enter open` when the row pushes a screen or dialog ·
  `Enter select` when it picks a value or action row · `Enter set` in a value picker.
  A more specific committing verb (`Enter adopt path`, `Enter add`) is fine; a synonym
  of the generic three (`pick`, `choose`, `commit`) is not. The filter atom is always
  `type to filter`.
- Key notation: `↑↓ move` (include the verb), `^R`/`^End` for Ctrl chords, `⌫` for
  backspace, `⇧` for shift.

### Dialogs

- Modal popups over the pushed backdrop (never full-screen replacements).
- Buttons are reverse-video chips (`  Label  `, `selected` style): safe way out on the
  left, the committing verb on the right **and default**, so Enter commits and Esc backs
  out. Two escalating caution tiers theme the frame: `danger=True` (amber) for a
  disruptive choice — discard edits, reboot, show private key — and `destructive=True`
  (the reserved red) for irreversible **data loss**, so a delete confirm reads red like
  its `typed_confirm` sibling. Bulk or irreversible deletions gate behind `typed_confirm`
  (also red); a single-record delete is a red Cancel/Delete confirm.
- Confirms are Cancel/Verb button dialogs, not Yes/No. Prompt lines that ask for input
  end with a colon.
- Dialog `title` is short; the question/instruction goes in `prompt`.

### Marks and icons — one glyph per concept

- Status marks (single-width, themed): `✓` ok · `✗` err · `⚠`/`?` warn · `●` unread/
  unacked (err) · `○` acked/empty (muted). Never `✔`, `✖`, `✅`, `❌` as status.
- Node types (shared with the map): `★` you (yellow) · `●` node · `▲` repeater ·
  `■` room · `◉` sensor · `○` unknown.
- Path cuts: a path line drawn as **chips** that gets cut — a lane that ran out, a row
  scrolled past its edge — breaks the chip off on a half block in that chip's own fill
  (`▐` ends a line, `▌` opens one; `pathline.cut_mark`/`cut_to`), so half the cell is
  segment and half is bare page. An ellipsis would say a *word* was shortened; the crack
  says the segment continues. Arrow-drawn paths keep the `…` — nothing to shear. Distinct
  from `⋯`, which marks whole hops *elided* out of the middle (`PathLine.ellipsized`).
- Path endpoints: a path line runs between the nodes it actually went between — the true
  origin and destination, never the first and last *relay*. Our own end is always the
  app-wide `★` (`pathline.SELF_GLYPH`, or `path_line(bare_self=True)`), never our name:
  the one node the reader never has to be told, and the cells belong to the hops that
  differ from row to row.
- Concept icons: 📡 advert · 🕒 clock/sync · 🔄 reboot · 💾 backup · 📂 restore ·
  🔑 channel/credential key · 🔐 identity/auth secret · 🗑 clear/delete · ✎ compose/edit ·
  ⚙ parameter · `#` count · ▶ run · ⚡ explore/probe · ★ best/winner · ⭐ watch ·
  📤 send now · 📨 courier/queue · 💬 chat · 🔔 notify · 🔕 mute (notifications off) ·
  📱 QR · 🔗 link · ↻ re-read · ↕ reorder · ⇄ reverse (flip a path's direction) ·
  🏆 trophy case/record · ⌨ command line · 🚪 quit. Packet-class icons (feed/viewer
  lane): 📢 advert · 📊 telemetry · 📦 packet · 💬 message · ✅ ack. Raw payload
  classes (`PAYLOAD_ICONS`): 📻 channel text · 💽 channel data · 📩 direct message
  (overheard) · 📥 request · 📮 response · 🎭 anon request · 🧭 path · 🎯 trace ·
  🧩 multipart · 🧰 control · ❔ unknown — raw advert/ack reuse 📢/✅.

### Colour

- Node names are always coloured: `name_style(name, key)` palette hue, derived from the
  node's key (its first byte — any known prefix agrees) so a rename keeps the colour.
  One rule on **every** platform; only the resolution changes (see **Platforms**).
  A surface holding only a name resolves it first (`make_name_key_resolver`); a node no
  key can place — an unresolved sender, a bare hash standing in as a name, an `○` ring —
  takes `node.unknown`, THE light grey for an unidentified node, everywhere and on both
  platforms. Colour is reserved for keyed identities, never seeded from a name's
  characters. `node.unknown` is deliberately not `muted`: muted is chrome and may sit a
  step darker, while an unidentified node is content you can still act on. Our own node
  is the pure-white `you` style; a context colouring (chart quality) may still win.
  Recency heat colours heard/first-heard ages, never names — seven steps on plain human
  boundaries (`widgets._HEAT_STEPS`), each `heat.*` style named for how old the node it
  colours is: white under 5 minutes, then yellow, light red, brown, red, light grey, and
  one cold grey shared by "over a year" and "never heard". A key lane (via
  `highlighted_hash`) lights its hash in the same key-derived hue; the rest of the key,
  and any key standing in as a name, stays muted. UX text says "key" for the lane and
  "hash" only for the short derived id — never "hash" for a truncated key.
- SNR always through `snr_style`; timelines oldest→now left-to-right, grey baseline = 0,
  drawn via `ui/braillechart` only.

### Layout

- Wrapped labelled rows hang under their value block (two-column grid), never column 0.
- Dialogs anchor slightly above true centre, sized for their populated state.
- Radio traffic: single transmissions or a user-chosen sample count with cooldown pacing
  — never bursts.

### Platforms

One codebase, two flavours: **regular** (desktop/ssh, 72 cols, truecolor, emoji) and
**picocalc** (the PicoCalc's 53×26/53×40 framebuffer console, 16 palette slots, a
512-glyph font, no emoji). A frozen `Platform` spec (`meshterm/platforms.py`) resolves
once at boot; consumers bind at platform-switch time via `platforms.on_platform` — never
branch on the platform per frame, and never `from meshterm.platforms import PLATFORM`.

- **Never emit a raw emoji or bare hex colour into picocalc output.** Icons go through
  `theme.glyph()` (the compact map); everything else is caught by the render-boundary
  fold (`theme.fold_text`, applied in `tui/render.render_to_ansi` and
  `MapCanvas.to_ansi_lines`) — but the fold is the safety net, not the design.
- The glyph contract is `ui/fontset.py` — the installed console font's exact codepoint
  inventory, device-verified. A character outside it is a test failure, not a tofu box
  found on-device. The font itself is built by `scripts/calculinux-console-font.sh`;
  the two files move in the same commit.
- The 16-slot palette is `theme._VT_SLOTS` (programmed via `/etc/vtrgb`; same script).
  `MESH_THEME_16` speaks `color(0..15)` only; backgrounds stop at slot 7. Both themes
  define identical style names.
- **Bold is brightness on the VT**: `bold` on a 0–7 foreground *is* slot N+8, so a
  dim-slot style must state its intent — `not bold` (keep the declared colour) or `bold`
  (the promotion is the point, only `title.muted`), never silent. Rich merges a row's
  base style into every span, so a silent one changes colour inside a selected row. Same
  trap outside the theme: `MapCanvas` drops emphasis entirely where bold is brightness,
  since its colours arrive quantized and it can't know which bank they landed in, and the
  fold's quantizer (`theme._nearest_slot_sgr`) states intent for it — a dim slot leaves as
  `22;3N`, never a bare `3N`, because `9N` is *how* the console spells bright and adjacent
  art spans (the wordmark's bevels, a raster's neighbouring cells) reset nothing between
  them, so a bare one inherits the intensity bit and lands a bank too high mid-row.
- On picocalc, the node hue and the heat gradient **quantize** — same rule, coarser
  resolution: `node_style` snaps the key's hue to its sixth of the wheel
  (`theme._NODE_SLOT_HEXES`, the six chromatic bright slots) and heat to the `heat.*`
  steps. Nothing loses its colour for being on the console. Marks whose hue is *fixed*
  rather than derived go through a theme name so the slot is chosen deliberately (the
  node types' `type.*`; a raster resolves the same entry via `theme.mark_rgb`) — a naive
  downsample greys the repeater's violet. Footer hints are replaced by the **F-key lane**
  (`ui/tui/fkeys.py`): five `FPair` slots per screen, F1–F5 primary and F6–F10 each
  slot's *opposite number* (physical Shift+F1..F5), labels ≤6 cells. `DEFAULT_LANE` claims
  only **F4/F5 — the pager**, which has no physical key at all; **the jump to either end
  rides the Shift half of the very pager heading for it** (F10 Top behind F5 Page ↑, F9
  Bottom behind F4 Page ↓), because Home and End *are* on this keyboard and only ever
  wanted a chip for consistency. That leaves **F1–F3 free on every screen** for its own
  verbs, and `EMPTY_LANE` for a screen with none at all (every dialog). **A chip names an
  action, never a key** — `Page ↑`, not `PgUp`; `Latest` on a transcript; `Region` on the
  map, whose Home reframes and whose paging zooms. **A directional pair rises toward its
  outer key**: where two adjacent chips are opposite ends of one axis, the *up · in ·
  more* end takes the slot nearer the lane's edge — F5 on the right-hand pair (F4/F5's
  paging, the map's zoom: `Zoom -` then `Zoom +`, a rocker), F1 on a left-hand pair (a
  select list's `Sect ↑` then `Sect ↓`) — and a slot's Shift companion follows its own
  slot's direction. The lane *is* the footer here, so it obeys the hint line's rule
  — never advertise a key that would do nothing. Empty and dim are different claims: leave
  the slot **empty** when the action isn't a thing on this screen (Retry in a channel), and
  clear `enabled`/`opp_enabled` to draw it **dim** (label kept, fill dropped) when it's a
  thing that just isn't available this paint. Dimming is presentational — `handle` stays
  the authority and no-ops. The lane is also the *only* affordance advertisement here, so
  anything the desktop reaches by a chord or a bare letter (a list's `^PgUp/^PgDn` section
  jumps, the Time Machine's `w`, the map's `^U`, a row's `Del`) earns a slot — otherwise
  it is undiscoverable on the device.
- `meshterm specimen` prints the whole visual language through the real funnels — the
  acceptance card on-device, a preview under `--platform picocalc` on the desktop.
- Dev loop: `meshterm --mock --platform picocalc` in a 53×40 window; the gallery and
  `tests/test_theme16.py` carry the contracts.
