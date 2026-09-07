# Changelog

Notable changes to MeshTerm, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the caveat
SemVer itself makes for a leading zero: while the major version is `0`, a **minor** bump is
allowed to change behaviour, not just add to it.

## [Unreleased]

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

### Fixed

- The three commands `CONTRIBUTING.md` asks contributors to run all pass:
  `python -m pytest -q`, `ruff check .`, and `ruff format --check .`. The lint gate had been
  reporting 2222 errors and the format gate 154 files, so a first contributor could not tell
  their own diff from the background.
- Two `TYPE_CHECKING` imports the test suite referenced but never imported, on paths that
  happened never to run.

[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jpmartineau/MeshTerm/releases/tag/v0.2.0
