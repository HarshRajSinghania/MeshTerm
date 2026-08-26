---
name: fix-ans-nulls
description: Replace the NUL (0x00) bytes in an ANSI art .ans file with spaces, and nothing else. Use when a .ans file exported from Moebius (or any ANSI editor) carries NULs where cells were never painted, when the splash logo renders with tofu / stray glyphs / a row shifted left, or whenever asked to blank, clean, or "black out" the nulls in ANSI art.
model: haiku
---

# Replacing the NULs in an ANSI art file

A `0x00` in the **art body** of a `.ans` file is a cell the editor never painted. It is not
a character — terminals draw it as tofu, as a stray glyph, or swallow it and shift the rest
of the row left. The fix is one byte wide: write a space there.

This skill makes **only** that substitution. Every other byte, escape sequences included,
stays exactly where it was, so the file's length never changes.

## Run

```
$DEV_VENV_PYTHON .claude\skills\fix-ans-nulls\denul.py --dry-run --width 71 meshterm/assets/splash/logo.71.ans
$DEV_VENV_PYTHON .claude\skills\fix-ans-nulls\denul.py --width 71 meshterm/assets/splash/logo.71.ans
```

- Always `--dry-run` first and read the counts.
- `--width` is only used to report a NUL's row/column in the warnings; it never changes
  which bytes are replaced. The repo's splashes are `--width 71` and `--width 53`
  (the number in the filename).
- A real run writes `<name>.ans.bak` next to each file it changes. Delete the `.bak`
  once the art is confirmed — it is not a repo artifact.
- Confirm visually afterwards: the splash is what this touches, and only a look at it
  says whether the cell reads right.

## The SAUCE record is not art — never blank its NULs

An ANSI art file's last 128 bytes are usually a **SAUCE** metadata record: title, author,
group, date, binary TInfo/Flags fields, and a NUL-padded font name (`IBM VGA`). It is
introduced by the DOS end-of-file marker `0x1A`.

Those NULs are **structure**, not unpainted cells. Blanking them corrupts the record — the
font name stops matching, the binary fields become spaces. The script cuts at that `0x1A`
and never touches anything past it; NULs found there are counted in the report as "left
alone".

This matters here, not in theory: as of 2026-08-26 **every** NUL in both
`meshterm/assets/splash/logo.71.ans` and `logo.53.ans` (24 each) lives inside the SAUCE
record. A naive `tr '\0' ' '` over these files damages them and fixes nothing. So a clean
"no NULs in the art (24 in the SAUCE/metadata tail, left alone)" is the expected result on
the current splashes, not a sign the script missed something.

## About "black" spaces

A space renders in whatever background the art last set. That is black in the ordinary case
— the default background, and what an unpainted cell should look like.

Where a NUL sits inside a span that set a non-black background, the new space inherits
*that* colour and reads as a coloured block instead. The script warns, naming each such
cell by row and column, and stops there: repainting a cell or closing a colour span is an
editing decision, not a substitution, and the whole point of this skill is that it makes
one substitution only. Take those to the artist (or to Moebius).

## Do not

- Do not run `tr`, `sed`, or a `.replace(b"\x00", b" ")` over the whole file — that is the
  SAUCE-corrupting version of this job.
- Do not "tidy" anything else while in there: no trimming trailing whitespace, no
  normalising line endings, no collapsing escape sequences. The art is hand-drawn
  (Moebius, by the author, no AI) and byte-exact.
