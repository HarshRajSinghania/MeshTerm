---
name: release
description: Cut a MeshTerm release — pick the version, write the changelog entry from the commits since the last tag, bump `__version__`, commit, tag, and push so the GitHub release builds. Use when JP says to release, cut a release, ship a version, tag a version, or bump the version, and when asked what would go into the next release.
---

# Cutting a MeshTerm release

A release is a **rename and a tag**, not a week of git archaeology — that is the whole
point of keeping an `[Unreleased]` section. The one part that takes real work is writing
the entry, because the changelog is prose someone reads, not a list of commit subjects.

Pushing the tag fires [github-release.yml](../../../.github/workflows/github-release.yml),
which calls `installers.yml`, builds all five platforms, attaches them with a combined
`SHA256SUMS`, and takes the release notes **from that version's changelog section**. So the
changelog is not documentation of the release; it *is* the release notes. An empty section
publishes "No changelog entry for X.Y.Z."

## Two things this skill never does

- **Never runs `release.yml`.** That is the **PyPI** publish, and it is deliberately hard
  to fire: no automatic trigger, a typed version that must match the build, and a `pypi`
  environment gate. PyPI has no rename and no true delete. It runs only when JP says so in
  that moment, from the Actions tab, by hand. The name `meshterm` is on PyPI's prohibited
  list anyway — the project publishes as `mesh-term`.
- **Never picks the version silently when it isn't a patch.** Default is a **patch** bump
  (`0.3.0` → `0.3.1`). Anything else — minor, major — is JP's call, and he says which part.
  Bumping one part zeroes the ones after it. While the major is `0`, a **minor** bump is
  allowed to change behaviour, not just add to it.

## 1. Preflight

```
python .claude/skills/release/release_check.py
```

It prints the current version, the last tag, every commit since it, the state of the
`[Unreleased]` section, any changelog link refs that are missing, and whether the tree is
clean and in sync with `origin/main`. Read it before touching anything — it is also the
answer to "what would go into the next release?" on its own.

Then the gates `CONTRIBUTING.md` asks contributors to pass:

```
python -m pytest -q && ruff check . && ruff format --check .
```

A release does not go out on a red suite. If something fails, stop and say so.

## 2. Decide the version

Patch unless JP says otherwise. Say the number you arrived at before you write it anywhere,
so a wrong assumption costs a sentence instead of a tag.

## 3. Write the entry

This is the work. Read the commits since the last tag (`git log --stat v<last>..HEAD`) and
write **what changed for someone using MeshTerm**, in the changelog's existing voice:

- **Categories are Keep a Changelog's** — `### Added`, `### Changed`, `### Fixed`,
  `### Removed` — in that order, and only the ones that have entries.
- **A bullet opens with a bolded sentence stating the user-visible fact**, then a paragraph
  saying what was actually wrong and what it now does. Read the `0.2.6` and `0.2.8` entries
  for the register: the failure is described from the reader's side first, the mechanism
  second, and the lesson last if there is one.
- **Small entries are one line and no bold.** Not every commit earns a paragraph, and
  several commits often collapse into one bullet — group by what the reader experienced,
  not by what the diff touched.
- **A commit that changes nothing a user can see does not appear.** Refactors that move
  code behind an unchanged surface, test-only commits, skill and tooling commits: leave
  them out. The changelog is not a shadow git log.
- Wrap at the file's width (~95 columns) and match its em-dash-and-clause rhythm.

Insert the section directly under the HTML comment at the top of `CHANGELOG.md`:

```markdown
## [Unreleased]

## [X.Y.Z] — YYYY-MM-DD
```

A fresh empty `[Unreleased]` heading goes back in above it — that is what makes the *next*
release a rename. Then add the link refs at the bottom, repointing `[Unreleased]` at the
new tag:

```
[Unreleased]: https://github.com/jpmartineau/MeshTerm/compare/vX.Y.Z...HEAD
[X.Y.Z]: https://github.com/jpmartineau/MeshTerm/releases/tag/vX.Y.Z
```

The date is the real date — check it rather than copying the one above.

## 4. Bump the version

`__version__` in [meshterm/__init__.py](../../../meshterm/__init__.py) is **the only place a
version is written**. `[project] version` in `pyproject.toml` is `dynamic` and hatchling
reads the attribute, so the number in a built wheel cannot drift from the number in the
source. Do not add a second one.

## 5. Commit, tag, push

```
git commit -am "MeshTerm X.Y.Z"
git tag -a vX.Y.Z -m "MeshTerm X.Y.Z"
```

Commit on `main` — never a topic branch. Then **confirm with JP before pushing**, because
the push is the act that builds and publishes the release:

```
git push origin main && git push origin vX.Y.Z
```

This is the one workflow where pushing is expected. It is still asked for, once, in that
moment — the standing rule against unasked pushes is not suspended by the skill, it is
satisfied by the answer.

## 6. Watch it land

```
gh run watch --exit-status
gh release view vX.Y.Z
```

Five platform builds plus the release job, so it takes a few minutes. Report the release
URL and whether the notes came out of the changelog or fell back to "No changelog entry".
If a build fails, the tag is already public: fix forward with a new patch version rather
than deleting and re-pushing a tag people may have fetched.

## At 0.9.0 the changelog resets

The first public release collapses everything below it into a single entry reading "first
public release" with the headline features under it. Every version under 0.9.0 was released
nowhere, and its entries are notes to ourselves about getting ready — nobody arriving on
launch day wants a changelog of the fortnight before it. **Keep the dates and the tags;
replace the prose.** The same note is an HTML comment at the top of `CHANGELOG.md`, where
whoever cuts it will be looking.
