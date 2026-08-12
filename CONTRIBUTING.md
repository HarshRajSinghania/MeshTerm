# Contributing to MeshTerm

Thanks for wanting to work on MeshTerm. A few things to know before you open a pull
request.

## Licensing — what happens to your contribution

MeshTerm is licensed under the [Apache License, Version 2.0](LICENSE). By opening a
pull request, you agree to license your contribution under those same terms — and,
going a step beyond what Apache-2.0 alone requires, you additionally grant Jean-Pierre
Martineau (the project's copyright holder) the right to relicense your contribution as
part of the MeshTerm project, should the project's license ever change in the future.
You keep copyright over what you write; you're not signing it away, just granting these
usage rights alongside it. No separate form, bot, or signature is needed — opening the
PR is how you agree.

If that's not something you're comfortable granting, say so on the PR before it's
merged and we'll talk about it, but by default that's the deal for anything merged into
this repository.

## Before you start

- Read [CLAUDE.md](CLAUDE.md). It isn't just instructions for AI assistants — it's the
  actual style guide for this codebase: the terminology (node vs. contact, heard vs.
  seen, etc.), UX standards for screens/dialogs/menus, and where the reusable building
  blocks live in code (`ui/menus.py`, `ui/markdown.py`, `ui/widgets.py`, `ui/theme.py`).
  New code should read like it always belonged here.
- For anything bigger than a small fix, open an issue first to talk it through before
  writing a lot of code — saves both of us a rewrite.

## Making a change

1. Fork the repo and branch off `main`.
2. Set up a dev install:
   ```bash
   pip install -e ".[dev]"
   ```
3. Before opening the PR, make sure these all pass:
   ```bash
   python -m pytest -q       # tests, including tests/test_gallery.py — the dual-platform
                              # readability gate (screens must stay readable at 72 cols
                              # regular / 53 cols PicoCalc; every PicoCalc case is a hard gate)
   ruff check .               # lint
   ruff format --check .      # formatting
   ```
4. Keep commits focused; write imperative-mood messages and explain *why* when it isn't
   obvious from the diff.
5. Open the PR against `main` with a clear description of what changed and why.

## Code style

- Match the surrounding file: naming, comment density, idiom.
- Follow the lexicon and UX standards in [CLAUDE.md](CLAUDE.md) — one term per concept;
  don't introduce a synonym for something that already has a name.
- New screens, dialogs, and rows go through the existing helpers rather than hand-rolled
  layout — CLAUDE.md names the enforcement points.

## Reporting bugs / requesting features

Open a GitHub issue. For bugs, include what you expected, what happened, and enough to
reproduce it — device/platform, MeshCore firmware version if relevant, and steps.

---

Questions about any of this, licensing included, are welcome as a GitHub issue or
discussion before you put work in.
