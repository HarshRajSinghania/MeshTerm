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

- `ui/menus.py` — `exit_rows`, `menu_rows`, `lane_row`, `section_heading`,
  `confirm_discard`, `fit_cells`.
- `ui/markdown.py` — `render_markdown` (THE prose renderer: a page of writing, drawn in
  the language below).
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
| Back | leave the current screen/list — Esc's word, and a row's only where leaving is a *choice* (see below) |
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

### Navigation — the stack

Navigation is a **strict stack**: entering a screen or a dialog pushes one frame, Esc pops
exactly one, and the frame you land back on is the one you left — same object, so its
cursor, sort, scroll and typed filter are simply still there. Exceptions are added
deliberately, one at a time, and say why in the code.

- A screen that **owns a loop** is a hub, and a hub *stays pushed* for the whole visit:
  `async with session.stay(screen) as visit:` / `while True: x = await visit.result()`.
  One push, one pop, however many rounds. Never pop-and-re-push a screen to give a dialog
  a backdrop — a screen that never left the stack already is one. `run_screen` remains the
  one-shot form: push, await, pop, for a dialog or a prompt.
- A **sub-view nests**; nothing flattens the stack to spare the reader a climb. Two peers
  may open each other (trace ↔ trophy case) and that cycle is fine — ^W is the climb.
- A list whose **content is data** (an editor's staged values, a queue that lost a row)
  refreshes in place with `SelectScreen.replace_items`, which follows the highlighted row
  by value and keeps the filter. Rebuilding the screen is for when the rows it was holding
  a place in are genuinely gone (a purge, a delete) — and then say so.
- **An entry chain is a stack too.** A flow that asks two or more things in a row runs
  through `menus.run_steps`: each step gets the answers so far (to label itself, and to
  offer its own previous answer as its default) and returns `None` to step back, so Esc
  undoes one step instead of the whole flow. A trailing Cancel/verb confirm is still a
  decision, not a step — its Cancel abandons.
- **Esc peels before it leaves.** A screen carrying a find-as-you-type filter treats the
  typed query as the most recent thing the reader entered: the first Esc clears it and the
  screen stays, the second leaves. One rule on all four (`SelectScreen` and everything
  built on it, the path composer, the map, the mesh walk), and the footer says `Esc clear`
  for as long as it is true. Chat is the same shape with a different peelable thing — its
  first Esc unpicks a selected message. It used to be a 2–2 split, which put opposite
  outcomes behind the same keystroke on the same affordance.
- **^W** unwinds every frame back to the main menu, **^Q** quits from anywhere. Both are
  answered in `TuiSession._dispatch` and neither is ever advertised — a global verb has no
  screen to belong to, and the F-key lane has only three free slots per screen. They are
  stated once, on the About page. `test_navigation` walks every string literal in the
  package to keep them out of the UI. ^W arms **every frame on the stack**, not just the
  top (`request_pop_all`), stopping at anything modal: a screen opened from a key handler
  — the packet viewer, the chat's delivery paths, a trace path flow — runs in a task no
  navigation frame awaits, so an unwind sent only to the top died there while the hub
  below sat on a `visit.result()` nothing would resolve. Such a flow is launched through
  `session.run_detached`, which absorbs the duplicate `PopToMenu` its task cannot carry;
  the stack is what the unwind actually travels down.
- `modal` is *owning the keyboard* (a prompt, progress, a busy splash); `floating` is only
  *drawn as a box*. They are not the same flag: a select list floats and is not modal, the
  busy splash is modal and does not float. ^W declines to unwind past anything modal.
- A caller tests one thing for "the user left": `CANCEL` (or the `None` that `ui.select`
  folds it to). `POP_ALL` never reaches a caller — the navigation boundary turns it into
  `PopToMenu`, which derives from `BaseException` so it crosses the app's `except Exception`
  tool guards; only the menu loop catches it.

### Screens and lists

- **No screen carries an exit row.** Esc leaves — it is on both platforms' keyboards and
  every footer hint says so — so a row that only repeated it was two lines out of every
  screen (a row in thirteen on the PicoCalc's 26) buying nothing. Lists end on their last
  content row; a hand-drawn action list ends on its last verb. The gallery enforces it:
  no rendered line may be the bare word **Back**.
- The exception is where leaving is a **choice** rather than an exit, which is the same
  rule that keeps a dialog's Cancel button. `exit_rows(staged, …)` draws nothing while
  clean and, once changes are staged, one blank `Separator(" ")` line then
  `✓ Apply n staged changes` over `✗ Back — discard staged changes` — Apply has no key of
  its own, so it needs a visible counterpart naming what the other way out costs. Same
  shape in `ReorderScreen`. A **Quit** row is likewise kept (main menu, device splash):
  it *initiates* the app's terminal action behind a confirm, and on the splash it is the
  only statement that the app can be left at all.
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

### Written pages

A screen that is *prose* (the three About pages) is **markdown**, not composed rows: the
text lives in `meshterm/assets/pages/*.md` and `ui/markdown.py` draws it in the language
above — `#` the page's own name in brand, `##` a section in the body accent (`## Title ·
note` gives it the muted aside), `###` a sub-heading indented with its prose, paragraphs
and lists hanging as blocks, quotes and fences behind a rail that survives wrapping,
links showing where they go (nothing is clickable on a framebuffer console). Two
paragraphs are page frame rather than body and sit flush and muted: the **standfirst**
under the `#` title and the **colophon**, the last paragraph under a closing `---`. Live
package facts arrive as `{version}` / `{author}` / `{copyright}` placeholders, filled as
the page opens. The `##` headings are the page's landmarks — they pin and the section
jumps step by them — so a written page earns `Sect ↑`/`Sect ↓` on the F-key lane exactly
as a grouped list does. Filling a page in is editing its `.md`; no Python follows.

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
  picker unchanged · `Esc quit` only at the main menu. While a find-as-you-type filter
  is standing the verb becomes `Esc clear`, because that is what the press does then.
- Enter verb by action: `Enter open` when the row pushes a screen or dialog ·
  `Enter select` when it picks a value or action row · `Enter set` in a value picker.
  A more specific committing verb (`Enter adopt path`, `Enter add`) is fine; a synonym
  of the generic three (`pick`, `choose`, `commit`) is not. The filter atom is always
  `type to filter`.
- A hint is drawn **once per frame**, and which surface draws it is the footer's call.
  Where the footer is the hint line (regular) it carries the *top* screen's hint, so a
  floating dialog's border stays silent — the same sentence in the border and at the
  bottom of the terminal was one of them wasted — and its clip arrows fall back to the
  base frame's `↑↓ more`. Where the footer is the F-key lane (picocalc) there is no hint
  line, so the border is the only place Enter/Esc/the arrows can be named and it keeps
  the hint, less every atom whose keys are all chips on the lane one row below
  (`fkeys.strip_lane_atoms` via `frame._dialog_hint`, resolved **per paint** — a screen
  rewrites its hint as its content changes and reads its lane fresh every frame). The
  chromeless splash has no footer row of any kind and keeps its hint in its own border on
  both platforms.
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
  app-wide `★` (`pathline.SELF_GLYPH`, taken straight from `marks.SELF_MARK`), never our
  name: the one node the reader never has to be told, and the cells belong to the hops
  that differ from row to row. As a chip it is the map's yellow star on neutral dark
  grey, padded like every other chip.
- Chip seams are **one** interlocked chevron (previous fill on next). Two chips of the
  same fill are the exception the interlock can't draw: the point drops its background so
  the page cuts the wedge. An elision breaks the ribbon rather than joining it — bare `⋯`
  on the page between a closing point and the next chip's notch, no fill, no padding.
- Concept icons: 📡 advert · 🕒 clock/sync · 🔄 reboot · 💾 backup · 📂 restore ·
  🔑 channel/credential key · 🔐 identity/auth secret · 🗑 clear/delete · ✎ compose/edit ·
  ⚙ parameter · `#` count · ▶ run · ⚡ explore/probe · ★ best/winner · ⭐ watch ·
  📤 send now · 📨 courier/queue · 💬 chat · 🔔 notify · 🔕 mute (notifications off) ·
  📱 QR · 🔗 link · ↻ re-read · ↕ reorder · ⇄ reverse (flip a path's direction) ·
  🏆 trophy case/record · ⌨ command line · 📖 read/about · 💰 support/donate ·
  🚪 quit. Packet-class icons (feed/viewer
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
- **White is what "you picked this" looks like**, and nothing else in the app is allowed
  to say it. The cursor row — the row `❯` points at, in a select list or in a screen
  drawing its own rows — wears `cursor` (white), never `brand`: the wordmark's teal is the
  app's identity, not a selection, and teal/cyan is itself a node hue, so a cyan-keyed
  node used to vanish into its own highlight. White is outside the node spectrum, so it
  can never collide with an identity. The same white lights the **active sort column's**
  heading and triangle (`contactlist._SORT_ACTIVE`, `widgets._sort_header`) — the same
  claim one axis over — and the path composer's insertion-slot chip, so the row you pick
  and the slot it lands in read as one gesture. It rides *under* the row's spans, so every
  lane keeps the colour it set — an age's heat, an SNR reading, a red badge — with the one
  exception the highlight exists for: **a node's key-derived hue folds to the cursor white
  on the cursor row** (`theme.is_identity_style` → `render._whiten_identities`, applied
  once at the render boundary for any Text whose *base* style is `cursor`), so the
  highlighted row reads as one thing instead of as a name arguing with its own selection.
  Only a keyed hue folds: `node.unknown`'s grey stays grey, because the highlight must not
  claim to know a node we can't place. A **path line is spared** wherever it sits on the
  row — there a hue is not decoration on a name, it is what tells one hop from the next and
  what a route graph's one-byte labels are matched by — so `pathline` stamps its own extent
  (`PATH_INK`, a style that draws nothing) and the fold skips what lies inside it. That is
  what makes the arrow form say what the chip form always said, its fills having never been
  in the fold's vocabulary. Reverse-video chips — a dialog's committing button,
  a running action's Abort, the arrow-mode composer slot, the editor cursor — are the
  `selected` fill, a **grey** block (`muted`'s slate; light grey slot 7 on the VT, the one
  grey a reverse can put behind text there, and the same fill the F-key lane uses). A chip
  marks where a press lands; it is chrome, and it does not get a hue.
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
