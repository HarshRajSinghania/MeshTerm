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

[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/v0.2.4...HEAD
[0.2.4]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.4
[0.2.3]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.3
[0.2.2]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.2
[0.2.1]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.1
[0.2.0]: https://github.com/jpmartineau/MeshTerm/tree/v0.2.0
