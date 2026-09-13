# Changelog

Notable changes to MeshTerm, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the caveat
SemVer itself makes for a leading zero: while the major version is `0`, a **minor** bump is
allowed to change behaviour, not just add to it.

<!--
  At 0.9.0 — the first public release — everything below collapses into a single entry
  reading "first public release", with the headline features under it. Nobody arriving at
  a project on its launch day wants a changelog of the fortnight before it: the versions
  under 0.9.0 were never released anywhere, and their entries are notes to ourselves about
  getting ready. Keep the dates and the tags; replace the prose.
-->

## [Unreleased]

### Added

- **The SPI radio bridge runs on `openhop_core`.** The library the uConsole's software
  node runs on was renamed from `pymc_core` in 2026, and the current `meshcore-uconsole`
  package depends on the new name. `meshterm-spi-bridge` now drives either runtime,
  preferring the newer, and two of its three compatibility shims stand down under it —
  openhop_core accepts long ACK payloads and pushes completed trace replies itself; only
  the raw RX-log push is still the bridge's. On a bookworm uConsole, where the trixie
  package cannot run, the bridge takes a venv of its own with `openhop-core[hardware]`
  and still uses the GUI's identity, so it stays the same node. Verified on the device:
  radio up, 159 contacts restored, `info` and `contacts` answered over TCP.

- **The PicoCalc picks its console font, and MeshTerm hands the console back.** A *Console
  font* row under Display on the Preferences page offers `6x12 (53x26)` — the font the
  device boots in — and `6x8 (53x40)`, fourteen more rows of the same glyph inventory in a
  shorter cell. Picking one is the preview: the console loads it there and then, and the
  page repaints at the new height (the kernel raises SIGWINCH on a font change), so the
  choice is looked at before it is kept. Apply keeps it; leaving and discarding puts the
  saved font back. The 6×8 base has no Cyrillic, so a node named in Cyrillic draws as
  boxes on it.

  A virtual terminal has one font for the whole console, so switching MeshTerm's switches
  the shell's. It saves whatever font was loaded before the first paint and reloads it on
  the way out — from the session's teardown and from an `atexit` hook, so quitting, an
  unwind and a crash all leave the console as they found it. Off the handheld, off a real
  VT, or with no `setfont`, none of it happens and the log says which. The row is drawn on
  the PicoCalc only, while the value round-trips through `preferences.yaml` everywhere.

  The 6×8 font's build moved into `scripts/calculinux-console-font-6x8.sh`, on its own
  because its base bitmap is the Linux kernel's `font_6x8` and therefore GPL-2.0 — marked
  as such, with the licence beside it, and reading the donor, alias and keeper tables out
  of the 6×12 script rather than keeping a second copy of them. Running the 6×12 script
  still builds both.

## [0.3.2] — 2026-09-11

### Added

- **Device config holds every setting the companion firmware has, and the device actions
  with them.** Clock sync, backup and restore, the identity key, reboot and factory reset
  were a menu entry of their own; they are an Actions section on the Device config page
  now, and the page takes the repeater admin page's shape — full-screen, titled with the
  node's name and a staged count, with Apply sending in place one value at a time and
  leaving a value the device refused staged beside its reason.

  The settings were checked against the firmware itself rather than the library that
  talks to it. New are client repeat, the auto-add hop limit and a row for each custom
  variable the device reports, and the firmware's own bounds apply: a PIN of 0 or six
  digits, path-hash modes 0–2, 150–2500 MHz, 7–500 kHz, TX power from −9 dBm.

- **Both editor pages pick a location on the map.** A "Pick location on map…" row sits
  above separate latitude and longitude rows, opens the map on the position as staged — or
  as the device last reported it — and stages both coordinates at once. Device config's
  single Location row and its Pick / Type / Clear dialog are gone.

### Changed

- **Repeater admin speaks the firmware's own CLI, every setting of it.** The catalog had
  been written from convention, and the firmware disagreed: the delays are floats (0.5
  showed as 0, and typing it back was refused), bandwidth, spreading factor and coding rate
  are one `radio` setting rather than three keys, the guest password is readable, and
  `path.hash.mode` and twenty-odd others were missing. It is transcribed from the firmware
  source now. Read settings leaves every row answered — a value, `empty`, or `n/a` where
  the node's firmware lacks the setting — and `^R` (F3 on the PicoCalc) re-reads the
  highlighted row alone.

- Repeater admin fills the frame like Device config, instead of floating over the node
  picker it was opened from.

- **A packet's relay chain is drawn as the middle of a route.** The `via` field names the
  repeaters that forwarded a frame and neither the node that sent it nor the one that
  heard it, yet its ribbon opened and closed square — the mark for *the route began and
  ended here*. Both ends wear the chevron now, as the TX sweep's composed relays do, and
  arrow mode says the same with a leading and a trailing `→`. Under the chain, the note
  about which node the reception described gives way to the hop count.

### Fixed

- **Retuning a companion's radio switched off its relaying.** The firmware reads the radio
  command's trailing client-repeat byte as *off* when it is missing, and nothing sent it.
  Every radio edit restates it now.

- The location picker opens at once. It waited on the radio for the contact list first,
  and a busy companion held it shut for twenty-odd seconds only to open with no contacts
  anyway; it opens on what is cached now and adds the rest when the radio answers.

- A repeater setting its firmware lacks reads `n/a`, rather than the `?` that says it was
  never asked.

- Every icon-led list starts its labels in the same cell. Some icons draw one cell wide and
  most emoji two, so a row led by `↻`, `⌨` or `🗑` started its words a column early; each
  list pads its icons out to the widest one now.

- Neither editor page slides its rows sideways any more — a long row ends at the edge.

## [0.3.1] — 2026-09-09

### Added

- **Devices you never want to connect to can be hidden from the splash.** A machine with a
  debug probe, a programmer and a USB adapter soldered into it listed all three on every
  start, in front of the one radio you came for. `h` drops the highlighted device and
  remembers it, `⇧H` brings every hidden one back, and both keys are named in the footer
  only where they would act.

  Hiding is a listing choice and nothing else — a hidden device is still remembered, still
  resolvable by `--port`, and shows itself again the moment it is connected to. The
  highlight stays where the row was rather than jumping back to the remembered default, so
  a run of adapters clears with a run of presses.

### Changed

- **The chat picker's conversation lane is measured against the names the list actually
  holds**, instead of a flat 22 cells. Every cell past the longest name was padding in
  front of a short one, taken straight out of the last-message preview — the only run on
  the row with something to say, and on the PicoCalc's 53 columns it left the message 12
  cells. The preview keeps what it saves: 31 cells to 37 on a 72-column terminal, 12 to 18
  on the console. What the cap costs is the tail of a long name, so `←→` now slides the
  message under the pinned columns to read one past its own edge.

- **Channels shows the wait instead of announcing the result.** Every action banked a
  "created X" note that surfaced only once the manager closed — by which time the refreshed
  list had already shown the result — and the notes accumulated: at three changes in one
  visit the outcome outgrew the acknowledgement popup and came back as the full-frame
  result window instead, so the same visit reported itself two different ways depending on
  how much had been done in it. Every device action now reports while it runs, under a
  modal busy card, and the count stays in the run log.

- Renaming a channel no longer closes the page it renamed. The channel is still there and
  that is still its page, so the new name and key are read back into the title and the
  rows, and the reader stays put. Only clearing it closes it, which is the one case where
  what the page was about is gone.

- `Esc` on the device splash says **bye** rather than quit — the splash is the door, and
  nothing has been started there to quit out of.

### Fixed

- **macOS and Linux were drawing the whole app in 256 colours.** prompt_toolkit picks the
  colour depth per output class, and its two classes disagree: the Windows one returns
  truecolor outright, the VT100 one returns 8-bit for every `TERM` but `linux`. Nothing
  ever passed a depth, so the same build drawing the same theme was 24-bit on Windows and
  quantized to the 216-colour cube everywhere else — silently, which is why it went
  unnoticed. The app looked fine, just flatter.

  It costs exactly what this palette is made of. The seven heat steps and the per-node hue
  wheel are close pastels chosen to be told apart; snapped to the cube, neighbours collide
  and an identity stops being distinguishable by hue. `COLORTERM` is not consulted by
  prompt_toolkit at all, so a terminal announcing truecolor the conventional way was still
  handed 256.

  The resolver only ever raises the verdict. A terminal that cannot be shown to do better
  keeps precisely the depth it had, because guessing 24-bit at a terminal without it costs
  not a duller palette but the colour entirely. A 16-slot console is never promoted — the
  theme addresses its palette by index, and RGB has nowhere to land there.

- **Adding a channel could write straight over one that was already there.** The free-slot
  probe stops for two very different reasons and returned the same short list either way:
  off the end of the configured slots it is the truth, but a read that simply failed left
  whatever it had — and that was cached for the session, an empty one reading exactly like
  a device with no channels, a truncated one making the next free slot look free with a
  channel sitting in it. The probe now says whether it finished, an unfinished one is
  answered but not kept, and every add confirms the slot is empty before anything is
  written to it.

- Keys tapped during a slow channel write were landing in the filter of the list
  underneath, which then came back showing nothing. The busy card is modal, so the press
  dies on the card instead.

- **Channels at 53 columns.** The column header was a hand-built string with no idea of the
  render width: it ran to 54 cells and wrapped, costing a content row out of twenty-six and
  drawing the pinned landmark twice. The command rows hand-padded their icons, so the
  one-cell marks started their labels a column left of the two-cell ones. The empty-message
  count was `·`, which in that same row is also the status separator and what the console
  folds 🔕 to — a muted channel with no messages drew it twice, two cells apart, meaning
  different things; it is `○` now, the app's empty mark. And the detail title was a 42-cell
  parenthesised blob that filled the PicoCalc's whole title bar, leaving nowhere to say Esc
  leaves.

- Every channel edit paid up to 64 device round-trips before the screen could redraw. The
  chat service's slot map was walking the radio itself, unbounded, skipping empty slots all
  the way to the 64-slot cap on firmware that never rejects an index — on top of the
  manager's own re-read. It builds from the probe that was being made anyway.

- A channel write that landed and then raised never invalidated what it had made stale, so
  a slot list and a chat slot map outlived a layout that had already moved. A reorder that
  drops partway now says so at once, since the reader is about to be looking straight at
  the half-applied layout, and the standard Public channel can no longer be added twice by
  a second press already on its way.

- Two of the Trophy case's seven discipline marks started their titles a column left of
  their siblings: 🛣 and 🕸 sit outside Emoji_Presentation, so Rich and wcwidth both measure
  them at one cell where the other five measure two. The marks now pad out to the widest of
  the seven, measured against what the platform will actually draw rather than written
  down.

- A route that has finished no longer ends on the chevron that means "the path runs on past
  the edge" — it ends square, and the cell that frees up goes back to the route.

- **A fully charged pack redrew the activity sparkline twice a minute.** The charger cuts
  out at full, the pack settles back to 99, the charger restarts, and the companion reports
  that flip every poll for as long as the thing is plugged in. The gauge is pinned to the
  header's right edge and the pulse takes whatever is left, so the fourth cell `100%`
  needed came off the sparkline and redrew the whole activity history at a new scale, on a
  device sitting still. A full pack reads `100` now, with no sign: three cells, exactly as
  the `99%` it keeps flipping back to, and the one reading whose `%` can be inferred.

- On the PicoCalc every channel row in the chat picker sat a cell to the left of every
  direct row, with the header over neither — the marker lane padded to a two-cell channel
  glyph that the console font draws in one.

- The device splash's own header wrapped at 53 columns, and its DEVICE lane sized itself to
  the longest name — which for a USB adapter is a forty-character product string, pushing
  HARDWARE off the edge entirely. The name lane yields now, and `←→` reads the rest of a
  long model string with the lanes in front of it pinned.

## [0.3.0] — 2026-09-08

The command line was rebuilt around the fact that it has **two readers**, and that trying
to serve both with one stream had been making it worse at each.

### Added

- **`--json`, on every command.** It used to be honoured by two of twenty and silently
  ignored by the rest, so `meshterm --json contacts | jq` failed while MeshTerm reported
  success. It now covers all 49 subcommands: an array for a listing, an object for a set of
  facts, one compact line per document, and no envelope — `contacts --json | jq '.[].node.name'`
  reads the way it looks.

  Values are typed (`20`, not `"20"`), absent is `null` and never an omitted key,
  timestamps are UTC RFC 3339 to the second so string comparison is time comparison, and a
  numeric field carries its unit in its key (`snr_db`, `uptime_s`, `rtt_ms`). A key is never
  truncated — a short id is a `hash`, because a truncated key cannot go back into `--to`.
  `monitor` and `chat listen` emit one document per record as they arrive, all the same
  shape.

  `--json` changes the rendering and never the report: the same exit status, the same
  records. An empty result prints its empty document **and still exits `5`**.

- **A documented exit status per outcome**, listed under every `--help`: `0` success, `1`
  failure, `2` usage error, `3` no device, `4` the device was reached but the operation
  failed, `5` nothing to report.

  `5` is the one worth knowing about. A command that ran fine and found nothing — an empty
  contact list, a trace that never came home — returns it, so a script can tell "found
  nothing" from "worked" without counting lines.

- **`--absolute`**, restoring ISO-8601 timestamps for a run, over a new `cli_time_format`
  preference. JSON is always absolute UTC regardless: a local offset is a fact about the
  machine that ran the command, not about the event.

- **A global option may be typed anywhere.** `meshterm contacts --json` and
  `meshterm --json contacts` are the same run. The first is what everyone types and it used
  to fail with a bare usage error.

- **`contacts` gains `HASH` and `LOCATION`.** The hash is the token `--path` takes; you used
  to slice it out of `KEY` by hand. `preferences show` gains `DESCRIPTION` back, and `info`
  glosses its second-valued readings (`uptime_s  93784  (1d 2h)`).

- **[`docs/cli.md`](docs/cli.md)**, a manual for the whole thing: both faces of every
  command, where MeshTerm keeps its state, what each failure looks like, and recipes. Every
  sample in it is captured from a real run rather than written.

### Changed

- **The plain face is for a person now, and says so.** It had grown the menu's manners —
  colour, boxes, section headings, glyph columns, a closing ✓ — and then briefly overcorrected
  into something only `awk` could love. Machine-readability is `--json`'s job, so the plain
  face is free to be read: no colour and no frames, still, but **alignment is the delimiter**
  and names are bare rather than `"quoted"`.

- **A time is an age** — `now`, `5m`, `3h`, `never` — because "recently?" is the question you
  typed the command to ask. An absolute instant survives where the instant *is* the fact: the
  device clock, an appointment, a live capture's own `TIME`.

- **A route is drawn and a path is typed.** A route reads
  `Yagi-Repeater (a1) → Alice (d4) → MockCompanion (00)`; a `--path` spec stays the
  comma-joined hex it has to be, because it is the one line that round-trips. Our own node is
  a hop like any other — the menu's `★` says "you already know who this is", which is true of
  the reader and false of whoever opens the file later.

- **A value read out of `show` can be typed back into `set`.** The settings dump printed an
  enum as `0 (off)` and an unset string as `(not set)`, neither of which `config set` would
  take back.

- **`ui.ack` and a tool's closing message go to stderr** rather than being dropped. stderr
  keeps the promise the dropping was made to keep — a redirect catches only the answer —
  while giving the person at the prompt back their ✓ and their count.

- **`--help` is Click's own plain help**, with no boxed panels and no markup to strip out of
  a pipe.

### Fixed

- **A node name could not be printed safely.** A node broadcasts its own name, and Rich read
  `[...]` in one as console markup: a node called `[bold]Loud` printed as `Loud` — silently
  no longer the string that identifies it — and one called `[/]Bob` raised `MarkupError` and
  took the command down, then took the error handler down with it while reporting the crash.

- **A control character in a name reached stdout.** Escaping covered the newline, the tab and
  the return and let **ESC** through, which put a live colour run into the caller's file — the
  one thing "no escape sequences" exists to prevent. BEL was dropped in silence, so the
  printed name was not the advertised one, and U+2028 ended the record like a newline. It is
  now every C0 and C1 control plus the Unicode separators.

- **Numeric columns never right-aligned.** Rich's `Text.wrap` returns early on
  `overflow="ignore"`, above the justify step, so asking for one threw the other away — while
  the standard, the code's own docstring and the manual all promised alignment.

- **`config show` printed an empty string as `""`**, which `config set` read as those two
  characters. Feeding a dump back replaced every empty setting with a pair of quote marks, and
  the next dump looked identical, so nothing ever said so. It also masked an **unreported**
  PIN behind bullets, telling the reader the radio was PIN-locked when it was not.

- **`meshterm about` drew the rule above its colophon at console width** — one 48KB line in a
  4KB page.

- **`config export-key > key.hex`** wrote a file with `[muted]` tags around the key that was
  supposed to be its whole content.

- **Failures were classified wrong in six places.** Nothing transmitted is not a device
  failure: an unopenable `--port` is `3` rather than `4` with a message about a connection
  there had never been, and a missing `--yes`, an unknown `--sort`/`--category`/`--profile`,
  a missing admin password, an unknown contact and a bad `--at` are all usage errors. A
  `--category` typo used to come back as `5` — indistinguishable from a real empty result —
  and an unknown `--profile` fell through to whichever radio was attached, which is the one
  wrong answer that puts a packet on the air.

- **Errors go to stderr**, in the `meshterm: what went wrong` shape, and stop folding at 80
  columns — a sentence broken across three lines is a sentence `grep` cannot find. Progress
  bars moved there too and draw nothing off a terminal.

- **An expected failure no longer dumps a traceback.** A lost serial link, or an argument a
  command rejected on its own, reads as one line and the right exit status.

### Removed

- **`meshterm map`.** A map is a picture — braille cells whose meaning is their position, and
  whose nodes are told apart by colour. Stripped of colour it would be unreadable; left
  coloured it was the one command whose output could not be piped anywhere useful. The map
  stays in the menu, and the located nodes are scriptable through `contacts`, which now
  carries their coordinates.

- **The QR codes** from `channels share`, `config share` and the About pages' scripted face.
  A QR is a second rendering of a link the output already prints, drawn for a phone pointed at
  a screen; redirected into a file it was a block of block characters wrapped around the one
  thing that was actually the answer. The menu still draws them.

## [0.2.8] — 2026-09-07

### Fixed

- **The window MeshTerm opened for itself was too small, and it showed.** The startup
  wordmark is 71 columns wide; below that the splash quietly swaps to the narrow one drawn
  for the PicoCalc — so a desktop session came up wearing the handheld's mark, with every
  menu description truncated onto the pager underneath it.

  MeshTerm was opening a *tab* in whatever Windows Terminal window happened to be around,
  inheriting its size (64 columns on the machine this was found on). It now asks for a
  window of its own, sized from the platform's own minimum plus a margin. Windows Terminal
  clamps that to what the display can show, so a small screen degrades exactly as before.

- The tab it opens is titled **MeshTerm**, not the executable's full path — which on a
  downloaded build was a line of Downloads folder.

## [0.2.7] — 2026-09-07

### Changed

- **MeshTerm moves to Windows Terminal rather than asking to.** It still says so, and
  still tells you how to stop it, but it doesn't put the question. The classic console
  can't draw a single icon whatever font it's given, so there was only one sensible
  answer — and it was being asked of someone who hadn't seen the app yet and had nothing
  to judge it by.

  Installing a **font** still asks, because that writes a file to your machine. Moving
  window doesn't. Preferences → Display → Console setup stops both.

## [0.2.6] — 2026-09-07

### Fixed

- **Accepting "reopen in Windows Terminal" killed the session** in the downloadable
  builds. A new window opened, printed `Security validation failure: parent process has
  different executable!`, and exited — while the window that made the offer had already
  closed. Worse than never offering.

  A one-file build unpacks itself and re-runs itself, the two halves coordinating through
  private environment variables. Those were being handed to the new copy, which concluded
  it was the second half of a launch it never made, checked that its parent was the same
  program, found Windows Terminal instead, and stopped. They are stripped now.

  It worked perfectly when run from source, which is the whole lesson: the packaged build
  is its own platform.

## [0.2.5] — 2026-09-07

### Fixed

- **Every icon was a box in the classic Windows console.** That console — which is what a
  double-clicked build still gets — draws no emoji at all, whatever font it is set to, so
  the menu came up as rows of replacement characters.

  MeshTerm now works out which console it is running in and, on that one, draws its icons
  through the compact single-character table it already keeps for the PicoCalc: `☼` for an
  advert, `¶` for a message, `★` for the trophy case. Nothing else about the desktop
  layout changes — same width, same colours, same accented names. Running in Windows
  Terminal or VS Code's terminal keeps the emoji, and `MESHTERM_EMOJI=1` or `=0` forces
  the matter either way. `meshterm platform` reports which it chose and why.

### Added

- **MeshTerm offers to reopen itself in Windows Terminal.** The classic Windows console —
  what you get from a double-click — can't show the emoji icons at all, whatever font it
  is given: the only two Windows fonts with emoji in them are proportional, and a console
  won't take one. A terminal is the only thing that fixes it, so MeshTerm now offers to
  move there. One keypress, nothing installed, nothing changed — and you get the app as
  it was drawn: icons, charts, path chips and all.

- **MeshTerm can install a font its charts will actually draw in** — for anyone who stays
  in the classic console. That console does no font fallback at all, and none of the fonts
  it offers can draw braille, which every timeline and activity graph in MeshTerm is made
  of. Neither can Hack Nerd Font, JetBrains Mono, Fira Code, Source Code Pro, or even
  DejaVu Sans Mono; the usual advice works only because most terminals quietly fall back
  to a proportional font.

  So MeshTerm now ships Cascadia Mono PL — Microsoft's console font, SIL OFL — and on
  that console offers to use it. If you already have Cascadia (every Windows 11 machine,
  and anyone with Windows Terminal) it just switches to it. If not, it offers to install
  it for you: no administrator rights, nothing downloaded, 723 KB. Saying no explains
  what saying no looks like and asks once more; a second no is remembered and never
  raised again. Preferences → Display → Console setup changes your mind about either
  offer.

## [0.2.4] — 2026-09-07

### Fixed

- **MeshTerm crashed on startup on Windows.** Building the screen raised
  `ctypes.ArgumentError: expected LP__CSBI instance instead of pointer to
  CONSOLE_SCREEN_BUFFER_INFO`, and on a double-clicked build the traceback and the window
  it was printed in were destroyed together.

  `ctypes.windll.kernel32` is a cache: every caller in a process gets the same object, and
  each function on it is a shared attribute. The emoji-width probe declared
  `GetConsoleScreenBufferInfo` as taking a pointer to *its* copy of the screen-buffer
  struct — which rewrote the signature for prompt_toolkit, which then called that same
  function with a pointer to *its* copy. Same layout, different class; ctypes refused.

  Nothing in MeshTerm reaches for `ctypes.windll` any more. Five call sites now take
  private handles from `core/win32dll.py`, so a signature declared for one purpose is
  invisible to everything else in the process.

### Changed

- The log-detail preference offers the plain level names — Error, Warning, Info, Debug —
  rather than descriptions of them. Someone being talked through a problem is told "set it
  to debug", not "set it to everything".

## [0.2.3] — 2026-09-07

### Changed

- The log is a plain text file, `~/.meshterm/meshterm.log`, one readable line per record
  with the traceback under the line that raised it. It was JSON Lines on the theory that
  something would replay it; nothing ever did, and the job it actually has is being opened
  by a person who has hit a problem.
- It rotates, at 2 MB across three files. It used to grow without limit — a real one
  reached 11 MB in ten weeks, which is a lot of disk on a handheld and an unreasonable
  thing to attach to an issue.
- **How much it keeps is a preference** — `log_level`, under a new Diagnostics group,
  defaulting to `WARNING`. The file now holds the problems rather than a narration of a
  working session: of the last twenty thousand records in a real log, twelve were a
  warning or an error.

### Added

- One interactive MeshTerm per data directory. A second one is turned away with the way
  out in the message, rather than quietly overwriting the first one's contacts and
  settings. The lock is held by the operating system, so a crash releases it — a process
  id in a file would leave a lock nobody could explain.
- Failures are written down. Every unhandled error reaches the log with its traceback and
  the terminal says where to find it; expected errors go in as warnings, because a log
  that only holds crashes cannot answer "what happened just before". The bug report form
  asks for the file.
- The window stays open after a crash when MeshTerm owns it. Double-clicking the
  executable on Windows gives the process its own console, which is destroyed the instant
  the process ends — so an error was drawn and erased together and all anyone saw was a
  flash. It now waits for a keypress, but only on that path: a run from a shell leaves its
  output on screen already, and a successful run should not make anyone press a key.
- A readable answer when there is no Windows console. Git Bash, MSYS and Cygwin are not
  Windows consoles, and prompt_toolkit's own error says so in the language of its
  internals, arriving as a traceback that reads like a broken app rather than the
  instruction it is.

### Fixed

- All ten stores write through one helper, and the file each writes to first is named
  for the process writing it. They shared a fixed `.tmp` name, so two copies of MeshTerm
  saving at the same moment took turns inside one scratch file and then each renamed
  whatever was in it over the real one. The rename was always atomic; what was being
  renamed was not.

## [0.2.2] — 2026-09-07

### Fixed

- **The downloadable builds were missing every tool.** They started, printed help and drew
  the specimen card, and had a menu with nothing in it but Quit — no contacts, no map, no
  trace, no chat. Tools are found at runtime with `pkgutil.iter_modules`, which finds
  nothing inside a frozen bundle, so none of them registered. The packaging now collects
  them, and the smoke tests run a tool-provided subcommand instead of only `--help`, which
  a gutted build passes perfectly well.

### Added

- `MESHTERM_HOME` points config and data somewhere other than `~/.meshterm`. Every copy of
  MeshTerm shared one directory, so trying a downloaded build meant letting it open the
  same history database as the one you use — the valuable half of an install, with no way
  to keep a second copy away from it. Also useful for a portable install, or two radios
  kept apart.

## [0.2.1] — 2026-09-07

### Fixed

- The formatter is pinned. `ruff>=0.4` meant CI installed a newer ruff than a laptop had,
  and the two disagreed about whether the tree was clean — 0.16 formats Python blocks
  inside Markdown and 0.15 doesn't, so the first CI run went red on a file that was fine
  locally. An unpinned formatter isn't a gate.
- `docs/` is out of ruff's reach. The survey documents quote code as it looked on a past
  date, in fragments that were never whole programs; a formatter rewriting a quotation
  edits the record.
- The installer builds only run for `main`. Every Dependabot branch that bumped an action
  touched the workflow file, matched its path filter, and started a five-platform build.

### Changed

- The distribution is named `mesh-term`. PyPI prohibits `meshterm` (pypi/support#12162),
  and the name you install is the only thing that changes: the import package and the
  command are both still `meshterm`.
- `pyproject.toml` carries the metadata a stranger sees on a package page — project URLs,
  trove classifiers, and the licence as a PEP 639 expression.

### Added

- Continuous integration. Lint once, tests across seven OS and Python combinations, and a
  packaging job that installs the built wheel into a clean environment and runs the command
  from outside the source tree.
- A release workflow with no automatic trigger, which publishes nothing until a human runs
  it on purpose and types the version.
- A security policy, a code of conduct, issue and pull request templates, weekly
  dependency updates, and the funding links the app's own support page already offers.
- A Releases page. Pushing a version tag builds the binaries and attaches them, with a
  combined `SHA256SUMS` and release notes taken from this file. It stays inside GitHub
  and is not the PyPI publish, which remains a separate and deliberate act.
- Standalone builds. A PyInstaller spec and a workflow that builds a single runnable
  file for Windows, macOS (Intel and Apple silicon) and Linux (x64 and ARM64), checks
  each one runs from outside the source tree, and writes a checksum beside it. Nothing
  is published; the files are workflow artifacts until someone attaches them to a
  release.

## [0.2.0] — 2026-09-07

The first version to carry a changelog. Everything before it is in the commit log and is
deliberately not reconstructed here.

### Added

- This file.

### Changed

- The project description now says what MeshTerm is, how it reaches a radio, and which
  systems it runs on, instead of naming a category. It is the PyPI summary and the package
  docstring, which no longer disagree.
- The version is written in exactly one place, `meshterm/__init__.py`. The distribution
  metadata reads it from there rather than repeating it, so the number on the splash screen
  and the number in a built wheel cannot drift apart.
- The basemap user agent identifies the real repository and reports the running version.
  It previously sent a bare `https://github.com/` and a hardcoded `0.1` to OpenFreeMap,
  which is the only handle a tile operator has on a client.
- `macOS` and `Bluetooth` are spelled one way across the project. `BLE` stays where it is
  an identifier — the transport name, the config keys, the CLI.
- `meshterm --help` opens with the same description as the package summary and the
  PyPI page, instead of a fourth wording of its own.

### Fixed

- The three commands `CONTRIBUTING.md` asks contributors to run all pass:
  `python -m pytest -q`, `ruff check .`, and `ruff format --check .`. The lint gate had been
  reporting 2222 errors and the format gate 154 files, so a first contributor could not tell
  their own diff from the background.
- Two `TYPE_CHECKING` imports the test suite referenced but never imported, on paths that
  happened never to run.

[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.3.2
[0.3.1]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.3.1
[0.3.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.3.0
[0.2.8]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.8
[0.2.7]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.7
[0.2.6]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.6
[0.2.5]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.5
[0.2.4]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.4
[0.2.3]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.3
[0.2.2]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.2
[0.2.1]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.1
[0.2.0]: https://github.com/jpmartineau/MeshTerm/tree/v0.2.0
