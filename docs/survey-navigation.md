# Navigation survey — every exception to "Esc pops the stack"

> **A historical record.** This was a snapshot of the code on the date below, kept
> because a project that writes down its own problems is easier to trust than one
> that doesn't. Some of what's here has since been fixed and some hasn't; nothing in
> it is a to-do list. Read it as "what this looked like then", not "what is wrong now".

Written 2026-08-22 against `main` @ `4b29248`. A catalogue, not a change: nothing here had
been fixed at the time of writing. Each finding says where it is, what it does, and what
makes it a question.

**Resolved since:** findings **E** and **§10** — 2026-08-29. Exit rows were retired
app-wide: `back_rows()` is gone, `exit_rows()` draws nothing while clean, and every
caller's three-way Back test collapsed to `is CANCEL` / `is None`. What is kept is stated
in CLAUDE.md.

**Resolved 2026-08-30 — A, B, C, and the shortcut that made B possible.** Navigation is
a strict stack now; the rules are in CLAUDE.md under *Navigation — the stack*.

- **A1/A2/A3.** A2 is the rule, and it grew a second half. A hub stays pushed for the
  whole visit (`session.stay` / `Visit.result`), so the *object* — not a `default=`
  restore — carries the cursor, the sort, the scroll and the filter. Where the rows are
  genuinely data (the editors, the outbox, the watchtower) they are swapped in place with
  `SelectScreen.replace_items`, which follows the highlighted row by value and clamps to
  its position when that row is gone. A1's vanishing-row problem is answered by that
  clamp, everywhere, rather than by watchtower's one hand-written case. A screen is
  rebuilt only when the rows it was holding a place in no longer exist — a purge, a
  removal, a record count that moved — and each such site says so.
- **B1/B2/B3.** They no longer flatten. Both hand-built escapes existed because a deep
  stack had no way out; **^W** is the way out, so trace ↔ trophy case and node detail's
  peers all nest, and Esc from any of them is one pop back onto the screen that opened
  them. The trace screen takes an `open_trophy_case` callback instead of resolving a
  sentinel its opener had to interpret, which is also what lets the two open each other.
- **C.** All five backdrop re-pushes are gone, and so are the six hand-rolled
  push/await/pop loops the survey paired them with — the same need, spelled one way.
  Repeater admin's was the worst of them: a second, identical `SelectScreen` drawn as a
  static stand-in for the real one. `admin_node_visit` keeps the real list up instead.
- **F.** Still the one place the stack is cleared rather than unwound, and it now also
  disarms a pending ^W: a disconnect has already abandoned the flow that unwind was for.

**Resolved 2026-08-30 (second pass) — D, plus H and I, found by re-running the survey.**
D is closed by peeling *everywhere*: the select list and the path composer now clear a
standing filter on the first Esc and leave on the second, as the map and the walk always
did, and the footer swaps its trailing verb to `Esc clear` while that is true. Two
exceptions the first survey missed are closed with it — see **H** and **I** below.

**Still open: G** (`popup` declared by hand). It is not navigation-visible: one tool
floats over the menu, the rest replace it, and the result presenter separately infers the
same thing from content.

The point of the survey is that MeshTerm's navigation is *almost* uniform. The exceptions
are few enough to list exhaustively, which means they can be decided one at a time rather
than discovered one at a time.

---

## 0. Exceptions H and I — found 2026-08-30

Two more shapes, neither in the original catalogue, both closed the day they were found.

**H. A picker gathered in `prompt_params` pops before the screen it feeds.** A tool's
`prompt_params` is a *one-shot*: whatever it opens resolves and pops before `run` is
called. Three tools picked something there and then opened a screen, so the screen sat
directly on the main menu and Esc from it skipped the list it had just been chosen from.

| Site | What Esc did | Now |
|---|---|---|
| Trace target | sweep → **main menu**, skipping the target list | picker stays; Esc lands on the row it was launched from |
| TX optimize | sweep → **main menu**; and the target list → **main menu**, not the node list | both pickers stay, nested; Esc walks the flow back one list at a time |
| Chat | landed on a picker, but a rebuilt one (filter lost, cursor by `default=`) | one picker for the visit, rows swapped in place |

The fix is A2's rule applied one level up: the loop that owns the picker moves into `run`,
and `prompt_params` returns only the marker for the interactive path. Trace's picker also
re-reads its `TRACED` lane after each walk (`ContactListScreen.update_rows`), because that
is the column the list is sorted by and a returning trace changes it.

*Revised 2026-09-12 — TX optimize.* Keeping both pickers pushed under the sweep was the
wrong reading of the rule for a **popup**: a popup asks, confirms or informs, and then it is
gone — it is not a place to walk back to, and two of them stacked read as two places. The
two picks are now one floating list that turns its page (`menus.run_wizard`, `… · step 1
of 2`): Esc on the second step turns back to the first with the picked node still
highlighted, Esc on the first leaves, and the box is popped before the sweep opens — so Esc
from the sweep lands on the main menu, the sweep being the whole visit. Trace's picker is a
full-screen contact list (a place), and stays as it is.

**I. An entry chain of prompts was not a stack.** Five flows asked two or more things in a
row, and Esc on any of them abandoned the whole flow rather than stepping back one — a
mistyped 32-hex channel key cost the name typed before it. `menus.run_steps` now runs a
chain as a stack; a trailing Cancel/verb confirm is deliberately left as a decision rather
than a step. Sites: `channels._join_with_key`, `channels._edit`,
`config_editor._stage_custom_var`, `courier._queue_flow`, `courier._pick_schedule`.

Also closed in the same pass: the channel detail was the last screen rebuilt per round
(A1's `default=cursor`, now `stay` + `replace_items`), which left `_menu_round` with no
session path at all and shrank it to the sessionless select-and-dispatch it always was
underneath — the last of §5's hand-rolled loops.

---

## 1. The machinery, in one screenful

Three primitives, all in [`ui/tui/`](../meshterm/ui/tui/):

| Call | What it does |
|---|---|
| `session.push(screen)` | screen becomes the top of the stack and is drawn |
| `session.pop(screen)` | that screen leaves the stack |
| `screen.resolve(value)` | the screen's future completes; the awaiting caller decides what happens next |

`session.run_screen(screen)` is push + await + pop as one call — the normal way to show a
screen. `Screen.handle("escape")` resolves with the `CANCEL` sentinel
([`screen.py:243`](../meshterm/ui/tui/screen.py#L243)); every screen inherits that unless it
overrides `handle`.

So the app has no "navigator". A screen never decides where the user goes — it resolves a
value and its *caller* decides. Every exception below is a caller deciding something other
than "pop and carry on".

---

## 2. What "normal" looks like

The reference flow is the main menu ([`menu.py:466-559`](../meshterm/ui/menu.py#L466)):

1. build the row list
2. `push` the menu
3. await a choice
4. `pop` the menu in a `finally`
5. run whatever was chosen
6. loop — rebuilding the menu, **re-highlighting the row just used** (`default=last_selection`)

Point 6 is the part worth naming: *returning from a thing lands you on the thing you
returned from.* It is the behaviour every list should have, and most do.

---

## 3. Exception A — cursor position on re-entry

**The single largest inconsistency in the app.** Three groups, three behaviours:

### A1. Restores the cursor — the intended behaviour (7 sites)

| Screen | Where | How |
|---|---|---|
| Main menu | [`menu.py:502`](../meshterm/ui/menu.py#L502) | `default=last_selection` |
| Channels (manager) | [`channels.py:214`](../meshterm/ui/channels.py#L214) | `default=highlight` |
| Channels (one channel) | [`channels.py:728`](../meshterm/ui/channels.py#L728) | `default=cursor` |
| Device config editor | [`config_editor.py:161`](../meshterm/ui/config_editor.py#L161) | `default=cursor` |
| Device actions | [`config_editor.py:688`](../meshterm/ui/config_editor.py#L688) | `default=cursor` |
| Repeater admin | [`repeater_admin.py:172`](../meshterm/ui/repeater_admin.py#L172) | `default=cursor` |
| Courier outbox | [`courier_screen.py:120`](../meshterm/ui/courier_screen.py#L120) | `default=cursor` |
| Watchtower | [`watchtower_screen.py:137`](../meshterm/ui/watchtower_screen.py#L137) | `cursor = choice` |

Watchtower has the most careful version of it — it *drops* the restore when the chosen row
is about to vanish ([`watchtower_screen.py:145-146`](../meshterm/ui/watchtower_screen.py#L145)):

```python
elif choice == _CLEAR:
    store.clear_acked()
    cursor = None  # the row itself disappears
```

That refinement exists in exactly one place. Anywhere else a row can disappear, the restore
either silently fails to match (harmless — falls back to the top) or lands on whatever row
slid into the vacated position (not harmless).

### A2. Reuses the screen object, so it keeps everything (4 sites)

Contacts states the principle outright
([`contacts_screen.py:200-201`](../meshterm/ui/contacts_screen.py#L200)):

> One screen for the whole visit: re-running it keeps the highlight (and any sort or
> filter) on the row the user just opened a detail for, rather than snapping to the top.

Also chat, live feed, dashboard. Strictly better than A1 — it preserves sort and filter too,
not just the cursor — and it costs less code.

Contacts also shows the honest exception: after a purge changes the data, it deliberately
*does* rebuild ([`contacts_screen.py:217-219`](../meshterm/ui/contacts_screen.py#L217)), and
says why.

### A3. Rebuilds and loses your place (3 sites) — **the actual finding**

| Screen | Where | What is lost |
|---|---|---|
| Trophy case | [`records_screen.py:851`](../meshterm/ui/records_screen.py#L851) | cursor — a fresh `SelectScreen` per round, no `default=` |
| Time Machine picker | [`timemachine_screen.py:1323`](../meshterm/ui/timemachine_screen.py#L1323) | cursor (sort *is* carried) |
| Node detail | [`node_detail_screen.py:1360`](../meshterm/ui/node_detail_screen.py#L1360) | cursor **and the open tab** |

Node detail is the sharpest case. `NodeDetailScreen.__init__` sets `self._tab_index = 0`
([`node_detail_screen.py:446`](../meshterm/ui/node_detail_screen.py#L446)) and the opener
constructs a new one every iteration. So: open a contact → Tab to **Routes** → pick a route
→ Enter to trace it → come back → **you are on Info, at the top**. The route you were
working through is two keypresses away again, every time.

All three are a one-line fix in the A2 direction (hoist the construction out of the loop).

> **Question for JP:** is A2 the rule? If so these three are bugs. If a *deliberate* reset
> is wanted anywhere, Contacts' purge branch is the model for how to say so.

---

## 4. Exception B — hand-offs that flatten the stack instead of nesting

Two places let one screen hand off to a *peer* screen, and both deliberately unwind first so
the user does not end up deep in a stack they have to climb.

**B1. Trace → Trophy case.** A trace that sets a record raises a dialog offering the trophy
case. Choosing it resolves the sentinel `OPEN_TROPHY_CASE`
([`trace_screen.py:128`](../meshterm/ui/trace_screen.py#L128)), the trace screen comes down,
and only then does the caller open records
([`trace_screen.py:2193-2199`](../meshterm/ui/trace_screen.py#L2193)):

> the trace screen (and every prompt over it) is already down, so the trophy case opens
> over a clean stack and Esc from it unwinds straight to the main menu — never back into
> this session.

**B2. Trophy case → Trace.** The mirror image, and the comment is even more explicit
([`records_screen.py:905-909`](../meshterm/ui/records_screen.py#L905)):

> the browser does not reopen behind it: when the trace screen closes, this whole flow
> returns, landing the user on the main menu instead of a trophy-case → trace →
> trophy-case stack that takes many Escs to climb out of.

These two are the same rule discovered twice: **a hand-off between peers flattens; only a
sub-view nests.** It is not written down anywhere.

**B3. Node detail → Trace / Map / Time Machine / Share** ([`node_detail_screen.py:1372-1397`](../meshterm/ui/node_detail_screen.py#L1372))
follows the same rule *without saying so*, and that is where §3's A3 comes from. Because
`run_screen` pops on resolve, node detail is already down when the peer opens — so the peer
gets a clean stack, exactly as B1 and B2 arrange by hand. But unlike B1/B2, node detail then
**comes back**, by constructing a fresh screen at the top of the loop.

So the three sites agree on flattening and differ on what happens afterwards:

| | flattens before the peer | returns afterwards | keeps its state |
|---|---|---|---|
| B1 Trace → Trophy case | yes, deliberately | no — lands on the menu | n/a |
| B2 Trophy case → Trace | yes, deliberately | no — lands on the menu | n/a |
| B3 Node detail → peers | yes, as a side effect of `run_screen` | **yes** | **no** |

B3 is the only one that re-enters, and re-entering through a constructor is what costs the
tab and the cursor. That makes A3-on-node-detail a *consequence* of this shape, not an
independent bug: fixing it means either hoisting the screen out of the loop (keep the object,
keep the state) or nesting properly (keep it pushed while the peer runs).

> **Question for JP:** "flatten before a peer opens" looks like the settled rule — three for
> three. What is unsettled is whether a hub screen should *nest* instead, so it never has to
> be rebuilt. Node detail is the only hub, so this is really one decision about one screen.

---

## 5. Exception C — the backdrop re-push

`run_screen` pops the screen when it resolves. But a dialog raised straight afterwards needs
something behind it, or it draws over a blank frame. So five sites pop, then immediately
re-push the same screen as a static backdrop:

| Site | Screen re-pushed as backdrop |
|---|---|
| [`records_screen.py:867`](../meshterm/ui/records_screen.py#L867) | trophy case, behind the discipline picker / delete confirms |
| [`contacts_screen.py:210`](../meshterm/ui/contacts_screen.py#L210) | contacts, behind the purge picker |
| [`repeater_admin.py:108`](../meshterm/ui/repeater_admin.py#L108) | node picker, behind the login prompt |
| [`menu.py:910-911`](../meshterm/ui/menu.py#L910) | base screen, behind the disconnect dialog |
| [`tx_screen.py:885`](../meshterm/ui/tx_screen.py#L885) | flight screen |

Records has the fullest explanation ([`records_screen.py:862-866`](../meshterm/ui/records_screen.py#L862)).

This is a **workaround for an API shape**, repeated five times. The screens that avoid it
(channels, config editor, repeater admin's own menu, courier, watchtower, main menu) all
drive `push`/`await future`/`pop` by hand instead of calling `run_screen`, precisely so the
screen stays up while sub-prompts float.

> **Question for JP:** would a `session.run_screen(screen, keep_pushed=True)` — or a
> `session.backdrop(screen)` context manager — retire both the five re-pushes and the six
> hand-rolled push/await/pop loops? They are the same need spelled two ways.

---

## 6. Exception D — what Esc peels before it leaves

Four screens carry a live find-as-you-type filter and draw it through the one
`render.query_line`. **Esc means two different things across them:**

| Screen | Esc with a filter active | ⌫ with a filter active |
|---|---|---|
| Map | clears the filter, stays ([`map_screen.py:920-925`](../meshterm/ui/map_screen.py#L920)) | — |
| Mesh walk | clears the filter, stays ([`walk_screen.py:430-436`](../meshterm/ui/walk_screen.py#L430)) | deletes a char, then pops the trail ([`walk_screen.py:449-456`](../meshterm/ui/walk_screen.py#L449)) |
| **Select list** | **leaves the screen, filter and all** ([`select.py:773-774`](../meshterm/ui/tui/select.py#L773)) | deletes a char |
| **Path composer** | **leaves the screen, filter and all** ([`path_composer.py:717-718`](../meshterm/ui/path_composer.py#L717)) | deletes a char, then pops a hop |

So on the map, typing `hub` then Esc shows you the map again. On a contact list, typing
`hub` then Esc drops you back to the menu. Same affordance, same glyphs, opposite outcome —
and the select list is by far the most-used of the four.

Chat is a fifth member of the family with a different peelable thing
([`chat.py:624-629`](../meshterm/ui/chat.py#L624)):

```python
if self._selected is not None:
    self._clear_selection()  # first Esc unpicks; next leaves the chat
```

so chat peels, and its comment reads as though peeling were the house rule.

**Score: 3 screens peel, 2 do not.** Worth noting the peel is *not* free — on a screen where
Esc peels, leaving a filtered list takes two Escs, which is the cost JP is weighing on exit
rows elsewhere.

> **Question for JP:** peel everywhere, or nowhere? The current split is not defensible as
> a distinction between spatial and list screens, because the path composer is a list and
> does not peel, while chat is not a filter and does.

---

## 7. Exception E — six spellings of "the user pressed Back"

`back_rows(value)` takes the value its Back row resolves with. Call sites, verbatim:

| Value passed | Sites |
|---|---|
| *(nothing — `None`)* | [`admin_picker.py:49`](../meshterm/ui/admin_picker.py#L49), [`:77`](../meshterm/ui/admin_picker.py#L77), [`config_editor.py:617`](../meshterm/ui/config_editor.py#L617), [`:790`](../meshterm/ui/config_editor.py#L790), [`:1025`](../meshterm/ui/config_editor.py#L1025), [`courier_screen.py:242`](../meshterm/ui/courier_screen.py#L242), [`watchtower_screen.py:225`](../meshterm/ui/watchtower_screen.py#L225), [`:362`](../meshterm/ui/watchtower_screen.py#L362) |
| explicit `None` | [`tx_optimize.py:439`](../meshterm/tools/tx_optimize.py#L439), [`contacts_screen.py:263`](../meshterm/ui/contacts_screen.py#L263) |
| `"__back__"` | [`tools/chat.py:187`](../meshterm/tools/chat.py#L187), [`records_screen.py:790`](../meshterm/ui/records_screen.py#L790) |
| `_BACK` | [`channels.py:619`](../meshterm/ui/channels.py#L619), [`:683`](../meshterm/ui/channels.py#L683), [`contacts_screen.py:152`](../meshterm/ui/contacts_screen.py#L152) |
| `_CANCEL` | [`config_editor.py:736`](../meshterm/ui/config_editor.py#L736) |
| a tuple shaped like the screen's other values | [`records_screen.py:850`](../meshterm/ui/records_screen.py#L850) `("back", None, 0, None)`, [`trace_screen.py:2012`](../meshterm/ui/trace_screen.py#L2012) `("back", None)` |

The cost lands on the callers, which have to test every way at once:

```python
# contacts_screen.py:204
if chosen is CANCEL or chosen is None or chosen == _BACK:
# records_screen.py:858
if picked is CANCEL or picked is None or picked[0] == "back":
```

Note `config_editor` names its Back sentinel `_CANCEL` — the one word CLAUDE.md reserves for
dialogs, used here for a row that says "Back".

> **Suggestion:** `back_rows()` could default its value to `CANCEL` itself, so a Back row and
> Esc resolve *identically* and every caller's three-way test collapses to `is CANCEL`. The
> tuple-shaped ones exist only because their screens type their values as tuples; those could
> take `CANCEL` too.

**Resolved 2026-08-29** — by removal rather than by defaulting: with no Back row there is
no Back value, and every caller now tests `is CANCEL` / `is None` alone. All six spellings
are gone, `channels._BACK` and `contacts_screen._BACK` with them.

This tied directly into the exit-row question — see §10.

---

## 8. Exception F — the stack reset

One site clears the whole stack: [`menu.py:824`](../meshterm/ui/menu.py#L824), when the device
link drops mid-session. The watcher cancels the menu worker, `session.reset()` drops whatever
screens it left behind, and the reconnect dialog opens over a clean frame.

This is the only place the stack is cleared rather than unwound. It relies on every screen
being pop-safe in a `finally`, which is documented at
[`session.py:388-395`](../meshterm/ui/tui/session.py#L388). Correct as written — noted here
because it is the one code path where a screen can vanish without its caller resolving it,
and anything holding state across a `run_screen` boundary would silently lose it.

---

## 9. Exception G — one tool floats, twenty-two replace

`Tool.popup` ([`tools/base.py:67`](../meshterm/tools/base.py#L67)) decides whether the menu
stays pushed while the tool runs. **Exactly one tool sets it:** `advert`
([`tools/advert.py:33`](../meshterm/tools/advert.py#L33)).

Every other tool pops the menu, runs full-screen, and the menu is rebuilt afterwards. Which
is right for a screen — but several tools are *also* dialog-sized in practice, and the result
presenter already makes exactly this call at the other end
([`surface.py:603-610`](../meshterm/ui/surface.py#L603)): a short text-only result floats as
a dialog, anything bigger opens a window.

So the app decides "float or replace" twice, by two different mechanisms, one hand-declared
per tool and one measured from the content.

> **Question for JP:** should `popup` be inferred the way the result presentation is, or is
> one declared flag on one tool the whole of the need?

---

## 10. Where the exit-row question actually lands

> **Resolved 2026-08-29.** Acted on in full, along the middle position this section
> proposed — wider, in the end, than the 19 `back_rows()` sites: the four hand-drawn Back
> rows went too (`ReorderScreen`'s clean-state row, the live feed's pinned foot row, node
> detail's tail action, and the `trace`/`tx` screens' action-list rows). Kept: the
> `exit_rows` discard pair, `ReorderScreen`'s dirty-state pair, the two **Quit** rows, and
> the new-record dialog's `Close` button — each for the reason named below. The gallery
> now asserts the *absence* of an exit row on every screen, on both platforms. What
> follows is the survey as written.

JP's note — *"remove the cancel and close actions at the bottom of pages; Esc is quicker and
available on all platforms; keep it where there is a confirm or cancel situation"* — was
acted on for the one unambiguous case (the path composer's `Cancel` row, which literally ran
`super().handle("escape")`; removed in `4b29248`). The rest of the territory:

**Already clean.** The three About pages have no exit row at all — prose, Esc, done. Titles
carry no emoji. Footer hints are 100% compliant on width, Esc-last, and verb choice (56
literal hints audited).

**Rows that are exactly Esc, still standing:**

- `back_rows()` — the app-wide **Back** row, 19 sites. Two of them carry the comment
  `# a visible exit beside Esc` ([`courier_screen.py:242`](../meshterm/ui/courier_screen.py#L242),
  [`watchtower_screen.py:225`](../meshterm/ui/watchtower_screen.py#L225)) — i.e. the row exists
  *because* it duplicates Esc, which is precisely the premise now in question.
- `ReorderScreen`'s Back row ([`select.py:931`](../meshterm/ui/tui/select.py#L931)) —
  `super().handle("escape")  # Back resolves CANCEL, same as Esc`.
- Live feed's Back row, reached by scrolling past the oldest packet
  ([`livefeed_screen.py:41-44`](../meshterm/ui/livefeed_screen.py#L41)).

Removing **Back** app-wide is a bigger decision than the path composer's Cancel, because:

- it is written into CLAUDE.md as a binding standard ("the only exit word on rows");
- `exit_rows()` builds on it — an editor with staged changes shows `✓ Apply n staged changes`
  over `✗ Back — discard staged changes`, where Back is *not* redundant: it names a
  consequence Esc does not;
- on PicoCalc, Esc is a real key, so the platform argument does not add anything there.

**A middle position worth considering:** keep Back exactly where it says something Esc does
not (the `exit_rows` discard case), drop it where it says nothing (the plain `back_rows`
case). That is roughly 3 sites kept, 16 dropped — and §7's suggestion (Back resolves `CANCEL`)
would make the drop mechanical rather than a per-caller edit.

**Buttons that are exactly Esc:** only one — the `Close` button on the new-record dialog
([`trace_screen.py:639`](../meshterm/ui/trace_screen.py#L639)). Left alone deliberately: it
is the *default*, so Enter dismisses safely, and this dialog appears unbidden after a trace.
Dropping it would leave `Trophy case` as the default and make a stray Enter navigate away.
Every other `Cancel` in the app is one half of a real two-way choice.

---

## 11. Summary — the shortlist

| # | Finding | Sites | Weight | Status |
|---|---|---|---|---|
| H | A picker gathered in `prompt_params` pops before the screen it feeds | 3 | **high** — Esc skipped a whole list | **done** 08-30 |
| I | An entry chain of prompts abandons instead of stepping back | 5 | medium — loses typing, never surprises | **done** 08-30 |
| A3 | Screen rebuilt per round, losing cursor (and node detail's tab) | 3 | **high** — felt on every visit | **done** 08-30 |
| D | Esc peels the filter on 3 screens, leaves on 2 | 5 | **high** — same keys, opposite result | **done** 08-30 |
| E | Six spellings of the Back value; callers test three at once | 19 | medium — invisible to users, costly in code | done 08-29 |
| C | Pop-then-re-push backdrop workaround | 5 | medium — an API gap, repeated | **done** 08-30 |
| B | Peer hand-offs all flatten, but the rule is unwritten and node detail pays for it | 3 | medium | **done** 08-30 |
| G | `popup` declared once by hand while the result presenter infers it | 1 | low | open |
| A1 | Cursor restore doesn't handle a vanishing row (except watchtower) | 7 | low | **done** 08-30 |
| F | `session.reset()` on disconnect — correct, noted for completeness | 1 | none | kept |
