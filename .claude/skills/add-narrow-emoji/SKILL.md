---
name: add-narrow-emoji
description: Fix a MeshTerm row whose layout an emoji breaks — a right border pulled a column short, or a glyph overrunning its lane — because the terminal draws it in a different number of cells than Rich/prompt_toolkit measure. Works in both directions: a glyph drawn narrower than measured joins the narrow allowlist, one drawn wider joins the wide set. Classifies the emoji (lone codepoint, country flag, VS16 sequence, or ZWJ/other cluster), says whether it is already listed, and adds it the correct way. Use whenever an emoji "fucks up the layout", notches a panel border, or smears a row.
---

# Adding an emoji to MeshTerm's terminal-aligned width sets

MeshTerm aligns Rich's and prompt_toolkit's glyph widths with how the terminal
*actually draws* an emoji, so a row frames flush instead of notching its right border a
column short or overrunning its lane. The machinery lives in
[emoji_width.py](../../..//meshterm/ui/tui/emoji_width.py); the design is in that module's
docstring and in the `glyph-width-alignment` memory. Background: the terminal's
narrow/wide split is **per-glyph and font-driven** — there is no rule and no way to probe
it (a cursor probe reads the PTY, not the renderer) — so the exceptions are two
hand-curated sets, extended one confirmed glyph at a time. This skill does that one
addition correctly.

## When to run

An emoji in a row breaks its layout, in either of two ways:

- **Border pulled in** (a blank sits between the border and the frame edge), or a smear to
  the right of the glyph — the terminal draws it in *fewer* cells than the authorities
  reserve. → the narrow allowlist (`_DEFAULT_NARROW_LONE`).
- **The row overruns**, pushing what follows a column right — the terminal draws it in
  *more* cells than the authorities reserve. → the wide set (`_DEFAULT_WIDE_BASE`). In
  practice this only happens to a **VS16 sequence** the narrow verdict got wrong; a *bare*
  codepoint both authorities call one cell is measured correctly and must be left alone.

Step 1 tells you which; it also tells you when the glyph is **already listed**, which is
the commonest answer to "please add this one".

## Steps

1. **Classify the emoji.** Run, from the repo root:

   The interpreter below is `$DEV_VENV_PYTHON` from `.dev.env` at the repo root (gitignored;
`.dev.env.example` shows the shape) — read it first.

```
   $DEV_VENV_PYTHON .claude/skills/add-narrow-emoji/classify.py <emoji>
   ```

   It prints the codepoints, how each authority measures it now (`Rich now`, `pt now`)
   and after the fix (`Rich fixed`, `pt fixed`), the class, and the action. `pt now` is
   the number that matters — prompt_toolkit places the panel border.

2. **Act on the class.** The machinery runs in *both* directions — a glyph drawn narrower
   than measured joins `_DEFAULT_NARROW_LONE`, one drawn wider joins `_DEFAULT_WIDE_BASE`
   — and the classifier names which, including "already listed", the commonest answer:
   - **already listed** (either set): nothing to add. If a row still misaligns, some
     *other* glyph on it is the culprit — classify that one.
   - **lone wide codepoint** (e.g. `👋`, `👍`): the narrow-allowlist case. Only add it **if
     the terminal draws it in one cell** — confirm visually first. Append the character to
     `_DEFAULT_NARROW_LONE` in [emoji_width.py](../../..//meshterm/ui/tui/emoji_width.py):
     `_DEFAULT_NARROW_LONE = "👋👍"`. (For a throwaway session test instead of a code
     change, set `MESHTERM_NARROW_EMOJI="👋👍"` in the environment.)
   - **lone narrow codepoint, bare** (e.g. `🛣` U+1F6E3, `🕸` U+1F578): **no change**, and
     resist the pull to "fix" it. Both authorities say one because the codepoint is outside
     Emoji_Presentation, and with no VS16 asking for emoji presentation the font draws a
     one-cell *text* glyph — they are right. Forcing it into `_DEFAULT_WIDE_BASE` reserves a
     cell the terminal never draws and pulls the row's border a column **in**; that is
     exactly what listing `🛣` did to the Trophy case, while `🕸` — same class, one board
     down, never listed — framed flush throughout. Both authorities already agreeing is not
     a bug to correct.
   - **VS16 emoji-presentation sequence** (e.g. `☀️`): **handled by default** (the variation
     selector is skipped, so the base is measured alone). But the renderer is not uniform —
     if *this* one paints two cells wide, add its **base** codepoint to `_DEFAULT_WIDE_BASE`,
     as `🛩️` (U+1F6E9) needed. Keying on the base makes the bare form ride along.
   - **flag (Regional Indicator pair)** (e.g. `🇨🇦`): **already handled** as a whole
     category — no entry. Confirm `pt fixed` is 2 and stop. Never list an individual flag:
     indicators are shared, so it would half-fix that flag's neighbours and skip the rest.
   - **ZWJ sequence / keycap / skin-tone / other cluster** (e.g. `👨‍👩‍👧`, `1️⃣`, `👋🏽`):
     **not covered** by the lone/flag mechanism — resizing its parts individually would
     misalign it. Do not force it into either set; note it for bespoke handling.

3. **Extend the tests.** In [tests/test_emoji_width.py](../../..//tests/test_emoji_width.py),
   add the new glyph to `test_rich_cell_len_narrows_only_allowlisted_lone_emoji` and
   `test_pt_cache_narrows_only_allowlisted_lone_emoji` (both authorities → one cell for a
   narrow addition, two for a wide one), and to the width-1 calibrate test. Keep a
   known-wide emoji (`📡`) asserted at two, so a future over-broad change is caught.

4. **Run the tests:**

   ```
   $DEV_VENV_PYTHON -m pytest tests/test_emoji_width.py -q
   ```

5. **Confirm visually.** The fix can't be reproduced headlessly — a non-interactive run
   keeps Rich's defaults, and CI can't see the renderer. Ask the person on the affected
   terminal to send a chat message with the glyph and confirm the border is now flush and
   nothing shifted. Only then is the addition trustworthy.

6. **Commit** with a message naming the glyph and why (see the repo's commit style).

## Guardrails

- **Never narrow a glyph the terminal draws two wide** (a menu icon like `📡`, `💬`), and
  never widen one it draws in one cell. Each set only helps glyphs mismeasured in its own
  direction; a wrong entry *introduces* the misalignment it was meant to remove. When
  unsure, leave it out.
- **One glyph, confirmed, at a time.** An unlisted emoji is no worse than before (the
  `erase_down` full-repaint still prevents a narrow one's smear); a wrongly-listed one
  breaks a border. Bias to omission.
- **Do not touch Rich/prompt_toolkit width globally.** A blanket emoji remap has been tried
  and reverted twice — it wrecks the menu icons the terminal genuinely draws two wide. The
  curated allowlist and the flag category are the only sanctioned levers.
