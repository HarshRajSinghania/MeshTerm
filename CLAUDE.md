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
- `ui/widgets.py` — `highlighted_hash` (THE hash widget), `format_ago` (prose ages),
  `_format_age` (column ages), `channel_glyph`, `_NODE_GLYPHS`, node heat colouring.
- `ui/theme.py` — `name_style` (per-name hues), `snr_style`, the `you` white.

### Lexicon — one term per concept

| Term | Meaning |
|---|---|
| node | any mesh participant |
| contact | a node the device knows (has a key) |
| heard | received from ("last heard", "first heard") — never "seen" in UX text |
| key | a full public key / channel secret |
| hash | the displayed key prefix (shown via `highlighted_hash`) |
| path | an ordered hop spec you compose or force (`a1,3d,…`) |
| route | the concrete node sequence a trace walked or will walk |
| via | prefix for a packet/message's relay chain |
| Back | leave the current screen/list (the only exit word on rows) |
| Quit | leave the app (main menu, device splash) — nowhere else |

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
  out. Destructive dialogs pass `danger=True`; irreversible ones gate behind
  `typed_confirm`.
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
  📱 QR · 🔗 link · ↻ re-read · ↕ reorder ·
  ⌨ command line · 🚪 quit. Packet-class icons (feed/viewer lane): 📢 advert ·
  📊 telemetry · 📦 packet · 💬 message · ✅ ack.

### Colour

- Node names are always coloured: `name_style(name)` palette hue; our own node is the
  pure-white `you` style; a context colouring (recency heat, chart quality) may win.
  Bare hashes stay muted — colour is the "this is a name" signal.
- SNR always through `snr_style`; timelines oldest→now left-to-right, grey baseline = 0,
  drawn via `ui/braillechart` only.

### Layout

- Wrapped labelled rows hang under their value block (two-column grid), never column 0.
- Dialogs anchor slightly above true centre, sized for their populated state.
- Radio traffic: single transmissions or a user-chosen sample count with cooldown pacing
  — never bursts.
