# Changelog

Notable changes to MeshTerm, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with
one caveat SemVer makes for a leading zero: while the major version is still `0`, a
**minor** bump is allowed to change how things behave, not just add to them.

## [Unreleased]

### Added

- **Diagnostics — everything a bug report opens with, in one block.** A new page under
  *This app*, and a new `meshterm diagnostics` command. It states which MeshTerm this is
  and how it was installed, the OS and its build, the terminal and its size, every verdict
  resolved once at boot and shown on no screen (icons, powerline, the platform flavour and
  why), the radio and what its link is set to, and how much stored history there is. Those
  are the questions an issue thread otherwise asks one at a time, and the person who hit the
  bug is the one least able to answer them.

  - **Nothing in it is private.** No pairing PIN, no admin password, no channel secret, no
    private key, no position, and no contact's name or key. The mesh is described in
    **aggregate** — a row count per table and the span the observations cover — which is the
    half that explains a bug without naming anyone the reporter talks to. It is a property
    of the feature rather than a habit: `tests/test_diagnostics.py` plants real secrets in
    the stores that hold them and fails if any of them, or a field merely *named* like one,
    reaches either face.

  - **The radio is asked but never required.** A report about a companion that will not
    connect is exactly the report most worth filing, so a failure to reach it becomes
    `connected no` with the radio's own words in `error`, and every other fact still
    arrives.

  - **The table counts are discovered from the schema**, not from a list written down
    somewhere, so a table added later starts being reported the day it lands — and the
    interesting count is always the table nobody expected to be full.

  - **It is one of the app's written pages.** The block is markdown, drawn through the same
    renderer behind the About pages and saved as the same document, so what you read and
    what you attach are one source. Its `##` headings are landmarks: each pins to the top
    row while its own section scrolls under it, and `^PgUp`/`^PgDn` step section by section.
    Every value is a code span, so a Windows path's backslashes and a firmware error's
    asterisks arrive as themselves rather than as markdown.

  - **It saves, for attaching rather than pasting.** `s` writes `meshterm-diagnostics.md`
    into the config directory beside the log, and answers in a popup naming the path; a
    write that fails says so in the same popup, in red, and leaves the page readable. The
    key is advertised in the footer hint on a desktop and on the lane's free **F3** slot on
    the PicoCalc — never in the page's body, which belongs to the report.
    `meshterm diagnostics --out PATH` does the same from a shell, spelled the way
    `config export-key --out` already spells it.

### Changed

- **A report has a third face.** `ui/renderers.markdown_blocks` projects any report as
  markdown sections — the *document* face, where the other two are a record stream and a
  machine document. It is what the seam was built for: no tool was touched and no report
  changed. A block may now carry a `caption`, the heading a format that *has* headings
  draws above it; plain and JSON ignore it, and markdown makes it a `##`.

  Rows are a list rather than a table, and that was measured: a markdown table's cells are
  cropped with an ellipsis at a narrow width, and a diagnostics page whose one long value is
  cut is missing the half that mattered.

- A path gained the lane constructor it never had (`ui/fields.path`) — the machine face had
  always spelled one correctly and the plain face could not render one at all.

## [0.9.0] — 2026-09-22

**The first public release.** Everything in MeshTerm is new today, so instead of a list of
changes, here is what it does.

MeshTerm is a program you run in a terminal to work with a **MeshCore** radio — one of the
small LoRa boards people use to build long-range mesh networks. A mesh like that needs no
internet, no phone signal, and nobody in the middle: the radios pass messages along to each
other until they arrive. You plug one into your computer, start MeshTerm, and it shows you
what the mesh is doing.

### What you can do with it

- **Run it two ways, and it behaves the same either way.** Type `meshterm` on its own and
  you get a full-screen menu you drive with the arrow keys. Type a command instead —
  `meshterm contacts`, `meshterm map` — and it prints an answer and exits, which is what you
  want in a script. Add `--json` to any command and the answer comes back as data for
  another program to read.

- **It listens all the time, and writes down what it hears.** Radios on a mesh announce
  themselves. From the moment yours is connected, MeshTerm notes down every announcement it
  overhears: which radio it was, how strong the signal was, and where it said it was. All of
  it is saved on your own computer. That is why the lists and charts can show you not only
  what is happening now, but what was happening last Tuesday.

- **You can see where everyone is.** There is a street map, drawn in the terminal, with the
  radios on it. You can pan it, zoom it, and search it. There is also a Time Machine that
  winds the map back so you can watch the mesh change over days.

- **You can talk to people.** Group channels and one-to-one messages, both live. If someone
  is out of range right now, the courier holds your message and delivers it when they come
  back. Channels can be shared as a QR code someone else scans, or as a link.

- **You can measure the mesh, not just guess at it.** Trace a message's route and see which
  hop is the weak one. Sweep your transmit power from coarse to fine to find the lowest
  setting that still gets through. Walk a map of how the whole mesh actually hangs together,
  which is often not how you thought it did.

- **It talks to your radio however your radio talks.** USB cable, Bluetooth, or over the
  network. It finds the ones it can find on its own and asks you to pick. If a connection
  drops it reconnects by itself. No radio yet? `meshterm --mock` runs the whole program
  against a simulated mesh.

- **It runs on the small machines too.** There is a second layout built for handheld
  consoles — the PicoCalc's 53-column screen and the uConsole — with its own fonts, colours
  and on-screen key labels, not just the desktop screen squeezed.

- **It works with no internet.** Map tiles are kept on disk once fetched, and when there is
  no network at all the map falls back to a plain grid. Nothing about MeshTerm needs to
  phone home.

### Getting it

One file to download, with nothing else to install, for Windows, macOS (both Intel and
Apple silicon) and Linux (including 64-bit ARM, so handhelds are covered). If you would
rather use Python, `pipx install` works too. The README has the exact commands.

MeshTerm is free and open source under the Apache 2.0 licence. The name and the logo are
not — see `NOTICE` — so a fork is welcome, under its own name.

[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.9.0
