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

[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.1
[0.2.0]: https://github.com/jpmartineau/MeshTerm/tree/v0.2.0
