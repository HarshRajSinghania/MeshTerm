# MeshTerm — working notes for Claude

MeshTerm is a full-screen terminal companion for MeshCore LoRa mesh devices. The UI is a
prompt_toolkit session rendering Rich content (`meshterm/ui/`), tools register into a
menu (`meshterm/tools/`), services run in the background (`meshterm/services/`), and
everything heard is recorded to SQLite (`meshterm/persistence/`).

Run the tests with `python -m pytest -q`. Screens must stay readable at 72 columns.

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
atlas, heard-nodes, relay hops, graph vertices, and node *types* (client/repeater/room/
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
- Label + description command rows go through `menu_rows` (two cell-aligned lanes,
  description muted). Editor rows (setting/value/description) go through `lane_row`.
- A row that opens further prompts ends with `…`; a row that acts immediately doesn't.
- Empty states are lowercase muted, optionally `— explanation`, never parenthesized.
- Body section headings inside screens: accent title, optional muted `  ·  note`.

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
  A surface holding only a name resolves it first (`make_name_key_resolver`); a name
  no known node carries stays muted — colour is reserved for keyed identities, never
  seeded from a name's characters. Our own node
  is the pure-white `you` style; a context colouring (chart quality) may still win.
  Recency heat colours heard/first-heard ages, never names. A key lane (via
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
