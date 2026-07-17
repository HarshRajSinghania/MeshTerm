---
name: add-narrow-emoji
description: Fix a ragged/short right border on a MeshTerm chat row that contains an emoji the terminal draws narrower than it measures. Classifies the emoji (lone codepoint, country flag, VS16 sequence, or ZWJ/other cluster) and adds it to the terminal-aligned width handling the correct way — a lone codepoint joins the allowlist; flags and VS16 are already handled; ZWJ clusters need a maintainer. Use when a chat message with an emoji notches the panel's right border.
---

# Adding a narrow emoji to MeshTerm's border-alignment allowlist

MeshTerm aligns Rich's and prompt_toolkit's glyph widths with how the terminal
*actually draws* an emoji, so a chat row frames flush instead of notching its right
border a column (or more) short. The machinery lives in
[meshterm/ui/tui/emoji_width.py](../../..//meshterm/ui/tui/emoji_width.py); the design
is in that module's docstring and in the `glyph-width-alignment` memory. Background:
the terminal's narrow/wide split is **per-glyph and font-driven** — there is no rule and
no way to probe it — so lone emoji are a hand-curated allowlist, extended one glyph at a
time as they are confirmed. This skill does that one addition correctly.

## When to run

A chat message containing an emoji renders with its right border pulled in (a blank sits
between the border and the frame edge), or a smear to the right of the glyph. That is the
symptom of the terminal drawing the glyph in fewer cells than Rich/prompt_toolkit reserve.

## Steps

1. **Classify the emoji.** Run, from the repo root:

   ```
   $DEV_VENV_PYTHON .claude\skills\add-narrow-emoji\classify.py <emoji>
   ```

   It prints the codepoints, how each authority measures it now (`Rich now`, `pt now`)
   and after the fix (`Rich fixed`, `pt fixed`), the class, and the action. `pt now` is
   the number that matters — prompt_toolkit places the panel border.

2. **Act on the class:**
   - **lone wide codepoint** (e.g. `👋`, `👍`): the allowlist case. Only add it **if the
     terminal draws it in one cell** — confirm visually first. Append the character to
     `_DEFAULT_NARROW_LONE` in [emoji_width.py](../../..//meshterm/ui/tui/emoji_width.py):
     `_DEFAULT_NARROW_LONE = "👋👍"`. (For a throwaway session test instead of a code
     change, set `MESHTERM_NARROW_EMOJI="👋👍"` in the environment.)
   - **flag (Regional Indicator pair)** (e.g. `🇨🇦`): **already handled** as a whole
     category — no entry. Confirm `pt fixed` is 2 and stop. Never list an individual flag:
     indicators are shared, so it would half-fix that flag's neighbours and skip the rest.
   - **VS16 emoji-presentation sequence** (e.g. `☀️`): **already handled** (the variation
     selector is skipped). No entry.
   - **ZWJ sequence / keycap / skin-tone / other cluster** (e.g. `👨‍👩‍👧`, `1️⃣`, `👋🏽`):
     **not covered** by the lone/flag mechanism — narrowing its parts individually would
     misalign it. Do not force it into the allowlist; note it for bespoke handling.

3. **Extend the tests.** In [tests/test_emoji_width.py](../../..//tests/test_emoji_width.py),
   add the new glyph to `test_rich_cell_len_narrows_only_allowlisted_lone_emoji` and
   `test_pt_cache_narrows_only_allowlisted_lone_emoji` (both authorities → one cell), and to
   the width-1 calibrate test. Keep a known-wide emoji (`📡`) asserted at two, so a future
   over-broad change is caught.

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

- **Never narrow a glyph the terminal draws two wide** (a menu icon like `📡`, `💬`). The
  fix only helps a glyph drawn *narrower* than measured; narrowing a correctly-wide glyph
  introduces the notch instead of removing it. When unsure, leave it out.
- **One glyph, confirmed, at a time.** An unlisted narrow emoji is no worse than before
  (the `erase_down` full-repaint still prevents its smear); a wrongly-listed one breaks a
  border. Bias to omission.
- **Do not touch Rich/prompt_toolkit width globally.** A blanket emoji remap has been tried
  and reverted twice — it wrecks the menu icons the terminal genuinely draws two wide. The
  curated allowlist and the flag category are the only sanctioned levers.
