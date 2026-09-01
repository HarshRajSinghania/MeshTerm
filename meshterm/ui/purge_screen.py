"""The Contacts sweep: rank the whole contact table, archive the weakest off the device.

A companion's contact table is finite and a busy mesh fills it with whatever adverts
arrived — mostly nodes heard once, in passing, four hops out — until there is no room left
to discover anyone new. This is the flow that gets that headroom back, in three steps over
the pushed Contacts list:

1. **Pick a target**, from a ladder in two sections. *By standing* rungs keep the
   strongest share of the table and archive the rest; *long silent* rungs sweep by
   last-heard age — the plain, predictable operation a ranking can't express ("everything I
   haven't heard in a year"). Both carry the real number of contacts they would archive,
   computed from the actual ranking rather than from arithmetic on the table size, because
   protected contacts are never victims.
2. **Read the list.** Every contact the sweep would take, weakest first, with its
   percentile and the two or three facts that put it there — for the age rungs too, since
   knowing how the contacts an age threshold caught actually *rank* is exactly the check
   that threshold cannot do for itself. Ending in the Apply/Back pair that
   :func:`~meshterm.ui.menus.exit_rows` draws for staged changes, because that is what this
   is: a choice with a cost on both sides, not an exit.
3. **Confirm by typing.** The bulk-deletion gate, red like every other.

**Only the percentile is ever shown.** A raw score means nothing without the distribution
it came from, and would need a legend the moment the weights were ever retuned; a contact's
rank against your other contacts explains itself. See :mod:`~meshterm.core.contact_score`
for the scoring itself, which is pure and lives in core.

**Archived, not deleted.** The sweep removes each contact from the *device* — freeing the
slot, which is the whole point — and records it in the cross-session store with an archive
stamp (see :meth:`~meshterm.core.contact_store.ContactStore.archive`). Nothing else moves:
the node's reception history, its position on the map, its Time Machine record and its
direct-message transcript are all keyed by node id rather than by a contact row and are
untouched. Because the store keeps the full public key, one write puts a contact back —
which is exactly what the chat screen already offers when a send is rejected for a
recipient the device no longer holds
(:class:`~meshterm.core.connection.ContactNotOnDeviceError`), so an archived contact you
message anyway is restored in passing rather than being a dead end.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Optional

from rich.text import Text

from ..core.connection import ContactNotOnDeviceError
from ..core.contact_score import (
    ScoredContact,
    rank_contacts,
    sweep_candidates,
)
from ..core.models import Contact
from .menus import Lane, column_header, fit_cells, menu_rows, section_heading
from .theme import name_style
from .tui import Choice, Separator
from .tui.screen import CANCEL

if TYPE_CHECKING:
    from ..context import AppContext

#: The keep-the-strongest rungs the target picker offers, as a share of the *sweepable*
#: contacts. Percentages rather than absolute counts because the score itself is only ever
#: read as a percentile: a rung and the lane it filters against then speak one language, and
#: the ladder means the same thing on a table of thirty and a table of three hundred without
#: a single value needing to be retuned. Coarse at the aggressive end — the difference
#: between keeping 90% and 75% is a tidy-up, between 50% and 25% a decision.
KEEP_RUNGS: tuple[int, ...] = (90, 75, 60, 50, 25)

#: The sweep's own tail sentinels, distinct from any :class:`ScoredContact`.
_APPLY = ("apply",)
_BACK = ("back",)

#: A day in seconds, for the age ladder.
_DAY = 86400

#: The last-heard rungs, ``(label, min_age_seconds)``. A contact qualifies when its age is
#: *at least* that long; a never-heard contact is excluded here and swept only by
#: :data:`_NEVER`, so an age rung can't quietly take a contact that simply hasn't adverted
#: yet. The same ladder the age-only purge offered before this flow absorbed it.
AGE_RUNGS: tuple[tuple[str, int], ...] = (
    ("Not heard in 1 week", 7 * _DAY),
    ("Not heard in 1 month", 30 * _DAY),
    ("Not heard in 3 months", 90 * _DAY),
    ("Not heard in 6 months", 180 * _DAY),
    ("Not heard in 1 year", 365 * _DAY),
)

#: The age-rung value meaning the never-heard bucket (contacts with no advert time at all).
_NEVER = -1

#: Percentile lane width, plus its gap — three digits and two cells of air.
_PCTL_W = 5

#: Name lane width in the preview, plus its gap. Wide enough for the 20-odd bytes a
#: MeshCore advert name runs to, and narrow enough to leave the reasons lane readable at
#: the PicoCalc's 53 columns.
_NAME_W = 22


def _pctl_style(percentile: int) -> str:
    """The style a percentile is drawn in — the same quality reading ``snr_style`` gives SNR.

    Three bands rather than a gradient: the heat ramp is spoken for (it colours *ages*, and
    a second gradient in a neighbouring lane would read as the same scale), and a percentile
    in a purge preview only ever answers one question — is this contact near the bottom?
    """
    if percentile >= 66:
        return "ok"
    if percentile >= 33:
        return "warn"
    return "err"


def _pctl_cell(percentile: int) -> Text:
    """One right-aligned percentile lane: the number alone, coloured by band."""
    return Text(f"{percentile:>3}  ", style=_pctl_style(percentile))


def _preview_header(width: int) -> str:
    """The preview's pinned column header, fitted to the terminal."""
    return column_header(
        [
            Lane("PCTL", _PCTL_W),
            Lane("NAME", _NAME_W),
            Lane(("WHY IT RANKS LOW", "WHY")),
        ],
        width,
    )


def _victim_row(scored: ScoredContact) -> Text:
    """One preview line: percentile, the contact's name in its own hue, then why it ranks low.

    The name keeps its key-derived colour like everywhere else in the app — this is a list
    of nodes, and a reader picking one out of thirty rows should not have to read it letter
    by letter because the screen it is on happens to be about deleting things.
    """
    contact = scored.contact
    row = _pctl_cell(scored.percentile)
    name = contact.name or contact.key_prefix or "?"
    # Through ``fit_cells``, so an over-long name ends on an ellipsis that says it was
    # shortened — and so the lane is measured in display cells, which is the only measure
    # that keeps the column straight once a name carries a wide glyph.
    row.append(
        fit_cells(name, _NAME_W - 2) + "  ",
        style=name_style(name, contact.public_key or contact.key_prefix),
    )
    row.append(" · ".join(scored.reasons), style="muted")
    return row


def _rung_desc(archived: int, cutoff: Optional[int]) -> str:
    """A rung's description lane: what it archives, and how far up the ranking it reaches.

    The cutoff is the *highest* percentile among the contacts the rung would take — so an
    age rung reaching into contacts that rank perfectly well says so on its own row, which
    is the thing an age threshold can never tell the reader by itself.

    Phrased as "the weakest *n*%" rather than as an ordinal percentile. Two reasons, and the
    second is the one that settled it: the phrase means the same thing to a reader who has
    never met a percentile, and — after the label lane — a 53-column console leaves this
    lane 27 cells, which "up to the 11th percentile" overran by twenty. A rung whose only
    interesting half is clipped away on the platform that needs it most is a rung that
    doesn't say anything.
    """
    if not archived:
        return "nothing to archive"
    head = f"{archived} contact{'' if archived == 1 else 's'}"
    return head if cutoff is None else f"{head} · weakest {cutoff}%"


async def _rank(ctx: "AppContext", contacts: list[Contact]) -> list[ScoredContact]:
    """Gather every signal these contacts have and rank them, strongest first.

    Five grouped scans of the history (:meth:`
    ~meshterm.persistence.repository.Repository.contact_signals`), one pass over the channel
    transcript for name attribution, and the two stores that carry the explicit protections
    — the Watchtower's stars and the remembered admin logins. Our own position comes from
    the device's self-info, and is simply absent on a device that advertises none, which the
    distance term reads as unknown for everybody.

    Args:
        ctx: Shared application context.
        contacts: The device's contacts (our own node is not among them).

    Returns:
        The full ranking, strongest first.
    """
    from ..core.contact_score import _node_id

    nodes = [_node_id(c) for c in contacts]
    signals = ctx.repo.contact_signals(nodes)

    # Channel attribution is by display name — a channel frame carries no sender key — so a
    # name two contacts share attributes to neither of them. Counting it for both would
    # award one node's standing to another; counting it for the first would pick by list
    # order. Both stay *unattributed*, which the score reads as unknown rather than as
    # silence, and the median fill puts them where an average contact sits.
    posts = ctx.repo.channel_post_counts()
    seen: dict[str, int] = {}
    for contact in contacts:
        folded = (contact.name or "").strip().casefold()
        if folded:
            seen[folded] = seen.get(folded, 0) + 1

    watch = getattr(ctx, "watch_store", None)
    admin = getattr(ctx, "admin_store", None)
    for contact, node in zip(contacts, nodes):
        found = signals.get(node)
        if found is None:
            continue
        folded = (contact.name or "").strip().casefold()
        unique = bool(folded) and seen.get(folded) == 1
        signals[node] = replace(
            found,
            channel_posts=posts.get(folded, 0) if unique else 0,
            channel_attributed=unique,
            watched=bool(watch is not None and watch.is_watched(node)),
            has_admin=bool(admin is not None and admin.get(contact)),
        )

    self_lat = self_lon = None
    try:
        info = await ctx.devstate.self_info()
        lat, lon = info.get("adv_lat"), info.get("adv_lon")
        # A device that has never had a position set advertises 0/0, which is a point in the
        # Atlantic rather than a location — the same reading the node detail page takes.
        if lat and lon:
            self_lat, self_lon = float(lat), float(lon)
    except Exception as exc:  # noqa: BLE001 - an unplaced device just loses one weak term
        ctx.log.debug("purge: no self position (%s); distance term stays unknown", exc)

    return rank_contacts(contacts, signals, self_lat=self_lat, self_lon=self_lon)


async def purge_contacts(ctx: "AppContext", self_key: str) -> int:
    """Run the whole sweep — rank, pick a target, preview, confirm, archive. Returns the count.

    Runs over the pushed Contacts list as its backdrop, so every step floats and Esc walks
    back out one frame at a time. Each step is a real step back: Esc from the preview
    returns to the ladder with the reader's rung still highlighted, rather than abandoning
    the flow and making them start over.

    The ranking is computed **once**, before the ladder is drawn, and reused for every rung
    and both routes — it is five scans of the history, and a rung's count would be a lie if
    it were measured against a different pass than the preview it opens.

    Args:
        ctx: Shared application context (interactive menu).
        self_key: The device's own public key (hex) — how the contact store scopes this
            device's remembered contacts.

    Returns:
        How many contacts were archived (``0`` if cancelled or nothing matched).
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # guaranteed by the Contacts screen
    session = ctx.ui.session

    async with ctx.ui.busy_overlay("Rating contacts"):
        contacts = await ctx.devstate.contacts()
        ranked = await _rank(ctx, contacts)

    sweepable = [scored for scored in ranked if not scored.protected]
    if not sweepable:
        ctx.ui.note("[muted]every contact is protected — nothing to sweep[/muted]")
        return 0

    while True:
        picked = await session.run_screen(_target_screen(ranked, sweepable))
        if picked is CANCEL or picked is None:
            return 0
        victims = victims_for(ranked, sweepable, picked)
        if not victims:
            ctx.ui.note("[muted]nothing matches that — no contacts to archive[/muted]")
            continue
        if await _preview(ctx, victims):
            return await _sweep(ctx, self_key, victims)


def _age_seconds(scored: ScoredContact) -> Optional[float]:
    """A scored contact's last-heard age in seconds, or ``None`` if it was never heard."""
    days = scored.signals.heard_age_days
    return None if days is None else days * _DAY


def victims_for(
    ranked: list[ScoredContact],
    sweepable: list[ScoredContact],
    picked: tuple,
) -> list[ScoredContact]:
    """The contacts one rung would archive, weakest first.

    Weakest first on **both** routes, the age ones included: the preview is read top-down,
    and a reader who skims only its first screen should be seeing the contacts they are
    least likely to want back — not whichever ones the age filter happened to list first.

    Args:
        ranked: The full ranking, strongest first.
        sweepable: Its unprotected subset, in the same order.
        picked: The rung's value — ``("keep", share)`` or ``("age", seconds)``.

    Returns:
        The victims, weakest first.
    """
    route, value = picked
    if route == "keep":
        return sweep_candidates(ranked, keep=round(len(sweepable) * int(value) / 100))
    matched = []
    for scored in sweepable:
        age = _age_seconds(scored)
        if value == _NEVER:
            if age is None:
                matched.append(scored)
        elif age is not None and age >= value:
            matched.append(scored)
    return list(reversed(matched))


def _target_screen(ranked: list[ScoredContact], sweepable: list[ScoredContact]):
    """Build the two-section ladder, every rung carrying its own real archive count.

    Grouped through :func:`~meshterm.ui.menus.section_heading` rather than drawn as two
    lists, so the headings pin as the ladder scrolls and the section jumps step by them —
    and so the two routes read as two ways of answering one question rather than as two
    separate features that happen to share a screen.
    """
    from .tui import SelectScreen

    protected = len(ranked) - len(sweepable)

    def rung(label: str, value: tuple) -> tuple:
        victims = victims_for(ranked, sweepable, value)
        cutoff = max((v.percentile for v in victims), default=None)
        return (label, _rung_desc(len(victims), cutoff), value)

    items: list = [section_heading("By standing")]
    items += menu_rows(
        [rung(f"Keep the strongest {share}%", ("keep", share)) for share in KEEP_RUNGS]
    )
    items.append(section_heading("Long silent"))
    items += menu_rows(
        [rung(label, ("age", secs)) for label, secs in AGE_RUNGS]
        + [rung("Never heard at all", ("age", _NEVER))]
    )
    held = f" · {protected} protected" if protected else ""
    return SelectScreen(
        f"Purge contacts — {len(ranked)} known{held}",
        items,
        prompt=(
            "Contacts are ranked on how lately and often you hear them, whether you have "
            "messaged, how near they are, and how much they post. The weakest are archived "
            "off the device and kept here."
        ),
        footer_hint="↑↓ move · Enter select · Esc back",
        filterable=False,
        wrap=False,
    )


def _preview_items(victims: list[ScoredContact]) -> list:
    """The preview's rows: a pinned column header, one line per victim, then Apply/Back."""
    return [
        Separator(_preview_header, pinned=True),
        *(Choice(title=_victim_row(v), value=v) for v in victims),
        # The same shape ``exit_rows`` draws, in this flow's own words: Apply has no key of
        # its own, so it needs a visible counterpart naming what the other way out costs.
        # Not ``exit_rows`` itself — these are not *staged changes* to a set of values, and
        # the pair should say what it actually archives.
        Separator(" "),
        Choice(
            title=Text.assemble(("✓ ", "ok"), f"Archive {_count_desc(len(victims))}"),
            value=_APPLY,
        ),
        Choice(title=Text.assemble(("✗ ", "err"), "Back — keep them all"), value=_BACK),
    ]


def _preview_screen(items: list, count: int):
    """The preview screen over already-built rows."""
    from .tui import SelectScreen

    return SelectScreen(
        f"Purge contacts — {_count_desc(count)} to archive",
        items,
        prompt="Archived contacts leave the device but stay here, with their history.",
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
        default=_APPLY,
        wrap=False,
    )


async def _preview(ctx: "AppContext", victims: list[ScoredContact]) -> bool:
    """Show exactly who would go, weakest first; return whether the reader committed.

    The audit step, and the reason the sweep is safe to offer at all: archiving several
    dozen contacts on a number nobody can check is precisely the operation that deserves to
    be looked at once. The list is filterable like any other — a reader who wants to know
    whether one particular node is in it should be able to type its name.
    """
    items = _preview_items(victims)
    while True:
        chosen = await ctx.ui.session.run_screen(_preview_screen(items, len(victims)))
        if chosen is CANCEL or chosen is None or chosen == _BACK:
            return False
        if chosen == _APPLY:
            return True
        # Any other row is a contact: Enter opens its node detail page, so a reader who
        # doesn't recognise a name can go and look before archiving it. The preview stays
        # pushed underneath, so Esc from the page lands back on the row it was opened from.
        from .node_detail_screen import open_node_detail

        await open_node_detail(ctx, chosen.contact)


def _count_desc(n: int) -> str:
    """``no contacts`` / ``1 contact`` / ``n contacts`` — the sweep's counting phrase."""
    if n == 0:
        return "no contacts"
    return f"{n} contact{'' if n == 1 else 's'}"


async def _sweep(
    ctx: "AppContext", self_key: str, victims: list[ScoredContact]
) -> int:
    """Confirm, then remove each victim from the device and archive it here. Returns the count.

    The removal is a companion-local command per contact — no LoRa traffic — so it runs
    straight through under the progress dialog rather than being paced. The store write
    follows each successful removal rather than being batched at the end, so an interrupted
    sweep leaves a consistent state: every contact off the device is recorded as archived.
    """
    warning = (
        f"This archives {_count_desc(len(victims))}, removing them from this device's "
        "contact list to free space for new ones. MeshTerm keeps them — with their "
        "reception history and message transcripts — and can put any of them back."
    )
    if not await ctx.ui.typed_confirm(warning, "archive", title="Purge contacts"):
        return 0

    device = await ctx.device()
    dev_pub = (self_key or "").lower().removeprefix("0x")
    store = ctx.contact_store
    stamp = int(time.time())
    archived = failed = 0
    with ctx.ui.progress("Purge contacts") as progress:
        task = progress.add_task("Archiving", total=len(victims))
        for scored in victims:
            contact = scored.contact
            try:
                await device.remove_contact(contact)
            except ContactNotOnDeviceError:
                # Already absent from the radio: the sweep's device-side work is done, and
                # archiving it here is what makes the row go away. Not a failure.
                ctx.log.debug("purge: %s was not on the device; archiving ours", contact.name)
            except Exception as exc:  # noqa: BLE001 - one bad removal mustn't abort the sweep
                failed += 1
                ctx.log.debug("purge: could not remove %s: %s", contact.name, exc)
                continue
            finally:
                progress.advance(task)
            if store is not None and dev_pub:
                store.archive(dev_pub, contact, when=stamp)
            archived += 1
    ctx.devstate.invalidate_contacts()

    if archived and not failed:
        outcome = Text(f"✓ archived {_count_desc(archived)}", style="ok")
    elif archived:
        outcome = Text(
            f"archived {_count_desc(archived)}; {failed} could not be removed", style="warn"
        )
    else:
        outcome = Text("no contacts could be archived", style="err")
    await ctx.ui.session.message_dialog(outcome, title="Purge contacts")
    return archived


def archived_rows(ctx: "AppContext", self_key: str) -> list:
    """The Contacts list's ``Archived`` section: the contacts swept off this device.

    Drawn always rather than behind a toggle. Silent archiving is how a reader loses track
    of what they archived — and the section is also the only route back: Enter on a row
    opens the node's detail page, where ``Restore to device`` puts it back.

    Args:
        ctx: Shared application context.
        self_key: The device's own public key (hex).

    Returns:
        The rows to append after the live contacts, or nothing when none are archived.
    """
    store = ctx.contact_store
    dev_pub = (self_key or "").lower().removeprefix("0x")
    if store is None or not dev_pub:
        return []
    archived = store.archived(dev_pub)
    if not archived:
        return []
    rows: list = [section_heading("Archived")]
    for remembered in archived:
        contact = remembered.to_contact()
        label = Text(
            remembered.name,
            style=name_style(remembered.name, remembered.public_key),
        )
        label.append(f"  ·  archived {_ago(remembered.archived_at)}", style="muted")
        rows.append(Choice(title=label, value=contact))
    return rows


def _ago(stamp: Optional[int]) -> str:
    """How long ago an archive stamp was, in the prose form ``format_ago`` speaks."""
    from .widgets import format_ago

    if not stamp:
        return "some time ago"
    return format_ago(max(0.0, time.time() - stamp))
