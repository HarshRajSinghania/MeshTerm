"""Rating contacts by how much they are worth a slot in the device's contact table.

A companion's contact table is finite, and a mesh fills it with whatever adverts happen to
arrive — so a radio left running long enough ends up holding mostly nodes heard once, in
passing, from four hops away, while there is no room left to *discover* anyone new. The
Contacts screen's bulk sweep (see :func:`~meshterm.ui.purge_screen.purge_contacts`) is how
that table gets its headroom back, and this module is the judgement it runs on: one score
per contact, so the sweep can take the weakest and leave the ones you would actually miss.

**The score is additive, never multiplicative.** Six terms, each normalised to ``0..1`` and
weighted (:class:`ScoreWeights`), summed. A product would let a single zero annihilate an
otherwise strong contact — a node that never advertised a position would score nothing at
all however much you talk to it — which is exactly the failure mode a "quality" heuristic
must not have.

**An unknown scores neutral, not zero.** This is the load-bearing rule. Roughly half the
contacts on a real mesh advertise no location, plenty are never overheard as a relayed
packet so have no hop count, and a channel poster whose name matches no contact cannot be
attributed at all. If "we don't know" resolved to ``0.0``, every one of those would sink to
the bottom *together*, and the sweep would purge by how much metadata a node happens to
broadcast rather than by how much it is worth. So a term that cannot be computed returns
``None`` and is filled with the **population median** of the contacts that could compute it
(:func:`_fill_unknowns`) — the contact lands exactly where an average peer would on that
axis, and the decision falls to the axes that *are* known. It is also why the two least
reliable terms carry the two smallest weights.

**A newcomer gets time to earn its keep.** Every term above rewards accumulated evidence,
so a node first heard this morning is indistinguishable from one that has been quiet for a
year — both have one advert and no history. :attr:`ScoreWeights.grace` therefore adds a
bonus that decays linearly to nothing over :attr:`ScoreWeights.grace_days`, enough to lift a
newcomer clear of the sweep for a fortnight while it either becomes a real neighbour or
doesn't. A contact MeshTerm has *never* heard (added by hand, or inherited from the device
before this history began) has no arrival time to decay from and is protected outright
rather than being punished for MeshTerm's own ignorance — see :data:`PROTECT_UNOBSERVED`.

**Some contacts are never candidates.** A score is a heuristic and a heuristic must not
overrule an explicit choice, so :func:`protection_for` short-circuits four cases before any
arithmetic: a watched node, anyone you have sent a direct message to, a repeater whose
admin credentials are stored, and the unobserved contact above. They are still scored and
still ranked — they are real contacts and the percentile scale is the whole population —
but they are never swept.

**What is displayed is the percentile, never the score.** A raw ``47.3`` means nothing
without the distribution it came from; a contact's *rank against your other contacts* is
self-calibrating, needs no legend, and stays comparable when the weights change. So
:class:`ScoredContact` carries :attr:`~ScoredContact.percentile` and the UI shows only that
(see :func:`percentile_rank`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from statistics import median

from .geo import haversine_km
from .models import Contact

#: Protection reason: the node is starred in the Watchtower. An explicit pin outranks every
#: heuristic in this module — the user already said this one matters.
PROTECT_WATCHED = "watched"

#: Protection reason: you have sent this contact a direct message. Choosing to talk to
#: someone is the strongest statement of intent the app can observe, and it should not be
#: undone by a score. (Inbound-only traffic does *not* protect — anyone can message you.)
PROTECT_MESSAGED = "messaged"

#: Protection reason: a repeater or room server whose admin password is stored. Purging it
#: costs the login, which is a far larger loss than a contact slot is a gain.
PROTECT_ADMIN = "admin"

#: Protection reason: MeshTerm has never heard this contact, so every evidence term is
#: empty for a reason that says nothing about the node. It came from the device's own table
#: (or was added by hand) before this history began; sweeping it would be purging by how
#: long MeshTerm has been running rather than by anything the contact did.
PROTECT_UNOBSERVED = "unobserved"

#: The order protections are reported in when a contact qualifies for several — most
#: deliberate first, so the preview explains a row by the strongest claim on it.
PROTECTION_ORDER = (PROTECT_WATCHED, PROTECT_MESSAGED, PROTECT_ADMIN, PROTECT_UNOBSERVED)

#: How a protection reads in the UI, keyed by the constants above.
PROTECTION_LABELS = {
    PROTECT_WATCHED: "watched",
    PROTECT_MESSAGED: "you messaged",
    PROTECT_ADMIN: "admin login",
    PROTECT_UNOBSERVED: "never heard here",
}


@dataclass(frozen=True)
class ScoreWeights:
    """The weights and half-lives the score is built from.

    The six term weights sum to 100, so a score reads as "out of 100" before the grace
    bonus is added — though nothing displays it (see the module docstring: the UI shows the
    percentile). Their relative sizes encode the ranking the operator asked for: direct
    correspondence far above everything, recency next, then raw volume, then the three
    weaker signals — of which the two least reliable (hops, distance) carry the least.

    Attributes:
        dm: Weight of direct-message correspondence — the heaviest term. A conversation is
            the only signal here that required a human decision on *both* ends.
        recency: Weight of how lately the contact was heard.
        volume: Weight of how many times it has been heard.
        channel: Weight of posting in channels the device is configured for.
        hops: Weight of topological closeness (fewer relays is better).
        distance: Weight of geographic closeness — the least reliable term, since about
            half of a real mesh advertises no position at all.
        grace: Bonus points a brand-new contact starts with, decaying linearly to zero over
            :attr:`grace_days`. Large enough to clear most of the field, because the whole
            point is that a newcomer has no evidence yet.
        grace_days: How long the newcomer bonus takes to fade completely.
        recency_half_life_days: Half-life of the recency term. Two weeks: long enough to
            ride out a node that adverts weekly, short enough that a month of silence tells.
        dm_half_life_days: Half-life of *conversation* recency. Deliberately much longer
            than :attr:`recency_half_life_days` — someone you exchanged messages with in
            the spring is still someone you correspond with, while a node last overheard in
            the spring is simply gone.
        volume_saturation: The packet count at which the volume term reaches ``1.0``.
        dm_saturation: The message count at which the conversation-volume half reaches
            ``1.0``.
        channel_saturation: The post count at which the channel term reaches ``1.0``.
        hops_midpoint: Hop count at which the hops term falls to ``0.5``.
    """

    dm: float = 30.0
    recency: float = 25.0
    volume: float = 15.0
    channel: float = 12.0
    hops: float = 10.0
    distance: float = 8.0

    grace: float = 25.0
    grace_days: float = 14.0

    recency_half_life_days: float = 14.0
    dm_half_life_days: float = 90.0
    volume_saturation: float = 100.0
    dm_saturation: float = 50.0
    channel_saturation: float = 30.0
    hops_midpoint: float = 2.0


#: The default weighting, used everywhere unless a caller passes its own.
DEFAULT_WEIGHTS = ScoreWeights()


@dataclass(frozen=True)
class ContactSignals:
    """Everything the score reads about one contact, already gathered from the history.

    Assembled by :meth:`~meshterm.persistence.repository.Repository.contact_signals` in a
    handful of grouped queries rather than per contact, so scoring a table of several
    hundred costs a constant number of scans. Every optional field means *unknown* and is
    filled with the population median before it is scored — never treated as zero.

    Attributes:
        node: The contact's 12-hex canonical id (how observations key a node).
        heard_age_days: Days since the contact was last heard, or ``None`` if never.
        packets: How many transmissions MeshTerm has heard *from* this node. Counts the
            app's own reception history, so a fresh install legitimately reads zero for
            everyone — which is why the sweep shows the whole ranked preview before acting.
        dm_total: Direct messages exchanged with this contact, both directions.
        dm_outbound: How many of those we sent. Non-zero protects the contact outright.
        dm_age_days: Days since the most recent direct message either way, or ``None``.
        channel_posts: Messages this contact posted on a configured channel, attributed by
            the ``Name: `` prefix a channel message carries (the wire has no sender key).
        channel_attributed: Whether that attribution was even possible — ``False`` when the
            contact's name is shared by another contact or never appeared, so the term
            reads unknown rather than "posted nothing".
        hops: Median relay count of packets seen originating from this node, or ``None``
            when none was ever overheard as a relayed frame.
        distance_km: Great-circle distance from our own node, or ``None`` when either end
            advertises no position.
        known_days: Days since MeshTerm first heard this node, or ``None`` if never heard —
            which is what :data:`PROTECT_UNOBSERVED` keys on.
        watched: Whether the node is starred in the Watchtower.
        has_admin: Whether admin credentials are stored for it.
    """

    node: str
    heard_age_days: float | None = None
    packets: int = 0
    dm_total: int = 0
    dm_outbound: int = 0
    dm_age_days: float | None = None
    channel_posts: int = 0
    channel_attributed: bool = False
    hops: float | None = None
    distance_km: float | None = None
    known_days: float | None = None
    watched: bool = False
    has_admin: bool = False


@dataclass(frozen=True)
class ScoredContact:
    """One contact's standing among the others: its score, its percentile, and why.

    Attributes:
        contact: The contact this describes.
        signals: The evidence the score was computed from.
        score: The weighted sum plus any newcomer grace. Never displayed — see
            :attr:`percentile`, and the module docstring for why.
        percentile: Percentile rank within the scored population, ``0``–``100``. **This is
            the only number the UI shows**: it is self-calibrating (a mesh of 30 contacts
            and one of 300 both read the same way), needs no legend, and stays meaningful
            if the weights are ever retuned.
        protection: Why this contact can never be swept, or ``None`` if it can. One of the
            ``PROTECT_*`` constants.
        reasons: Short lowercase phrases naming what this contact's standing rests on —
            its weakest lanes when it is a purge candidate, so the preview row explains
            itself without the reader having to know the formula.
    """

    contact: Contact
    signals: ContactSignals
    score: float
    percentile: int
    protection: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def protected(self) -> bool:
        """Whether this contact is exempt from the sweep whatever its score."""
        return self.protection is not None

    @property
    def protection_label(self) -> str:
        """The protection's UI wording, or the empty string when unprotected."""
        return PROTECTION_LABELS.get(self.protection or "", "")


# -- the individual terms ---------------------------------------------------------------
#
# Each returns a value in 0..1, or ``None`` for "cannot be computed from this contact's
# evidence" — which the caller resolves to the population median rather than to zero.


def _decay(age_days: float | None, half_life: float) -> float | None:
    """Exponential decay from ``1.0`` at age zero, halving every ``half_life`` days.

    Args:
        age_days: How old the event is, or ``None`` if it never happened.
        half_life: Days for the value to halve.

    Returns:
        The decayed weight, or ``None`` when ``age_days`` is ``None``.
    """
    if age_days is None:
        return None
    return 0.5 ** (max(0.0, age_days) / half_life)


def _saturating(count: float, ceiling: float) -> float:
    """A logarithmic ramp from ``0`` at zero to ``1`` at ``ceiling``, clipped above it.

    Logarithmic because the interesting difference is between 1 packet and 10, not between
    90 and 100: a node heard ten times is emphatically not one tenth as established as one
    heard a hundred times, and a linear ramp would say exactly that.

    Args:
        count: The observed tally.
        ceiling: The tally at which the term reaches ``1.0``.

    Returns:
        The ramped value, in ``0..1``.
    """
    if count <= 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(ceiling))


def term_recency(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """How lately the contact was heard, decaying with a two-week half-life.

    A contact never heard at all scores ``0.0`` rather than unknown: unlike a missing
    location, "we have never received anything from this node" is real evidence about the
    node, not a gap in what it chose to broadcast. (A contact MeshTerm has never heard
    *because the history is younger than the contact* is caught earlier, by
    :data:`PROTECT_UNOBSERVED`.)
    """
    if signals.heard_age_days is None:
        return 0.0
    return _decay(signals.heard_age_days, weights.recency_half_life_days)


def term_volume(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """How much traffic MeshTerm has heard from this node, on a saturating log ramp."""
    return _saturating(signals.packets, weights.volume_saturation)


def term_dm(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """Direct correspondence: mostly how much, partly how lately.

    Split 60/40 between volume and recency so that a long exchange which has gone quiet
    still counts for most of what it was worth — a conversation is a standing relationship,
    not an event that expires. A contact with no messages at all scores ``0.0``: an absent
    conversation is evidence, not a gap.
    """
    if signals.dm_total <= 0:
        return 0.0
    volume = _saturating(signals.dm_total, weights.dm_saturation)
    recency = _decay(signals.dm_age_days, weights.dm_half_life_days) or 0.0
    return 0.6 * volume + 0.4 * recency


def term_channel(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """How much this contact posts on the channels the device is configured for.

    Returns ``None`` when the contact could not be attributed at all — a channel message
    carries no sender key, only a ``Name: `` prefix, so a contact whose name is shared with
    another contact (or which has simply never appeared as a prefix) is *unmeasured* rather
    than silent. Scoring that as zero would quietly demote everyone whose name happens to
    collide, which is a property of the name and not of the node.
    """
    if not signals.channel_attributed:
        return None
    return _saturating(signals.channel_posts, weights.channel_saturation)


def term_hops(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """Topological closeness — a hyperbolic falloff, ``1.0`` direct and ``0.5`` at the midpoint.

    Hyperbolic rather than exponential because the difference between four hops and five
    barely matters, while the difference between zero and one matters a great deal.
    """
    if signals.hops is None:
        return None
    return 1.0 / (1.0 + max(0.0, signals.hops) / weights.hops_midpoint)


def term_distance(signals: ContactSignals, midpoint_km: float | None) -> float | None:
    """Geographic closeness, scaled against *this mesh's own* median distance.

    The midpoint is the population's median known distance rather than a constant, so the
    term reads the same way on a dense downtown mesh and a sparse rural one — 50 km is far
    in the first and unremarkable in the second, and a hardcoded threshold would have to be
    wrong on at least one of them. Unknown when either end advertises no position, or when
    too few contacts do for a median to mean anything.
    """
    if signals.distance_km is None or not midpoint_km:
        return None
    return 1.0 / (1.0 + max(0.0, signals.distance_km) / midpoint_km)


def term_grace(signals: ContactSignals, weights: ScoreWeights) -> float:
    """The newcomer bonus, in points (not ``0..1``), decaying linearly to zero.

    Returns ``0.0`` for a contact with no arrival time — that case is protected outright
    (:data:`PROTECT_UNOBSERVED`) rather than granted a bonus it could keep forever.
    """
    if signals.known_days is None:
        return 0.0
    remaining = 1.0 - (max(0.0, signals.known_days) / weights.grace_days)
    return weights.grace * max(0.0, remaining)


# -- protections ------------------------------------------------------------------------


def protection_for(signals: ContactSignals) -> str | None:
    """Why this contact can never be swept, or ``None`` if the score decides its fate.

    Checked in :data:`PROTECTION_ORDER` — most deliberate claim first — so a contact that
    is both watched and messaged is explained by the star the user actually set.

    Args:
        signals: The contact's gathered evidence.

    Returns:
        One of the ``PROTECT_*`` constants, or ``None``.
    """
    claims = {
        PROTECT_WATCHED: signals.watched,
        PROTECT_MESSAGED: signals.dm_outbound > 0,
        PROTECT_ADMIN: signals.has_admin,
        PROTECT_UNOBSERVED: signals.known_days is None,
    }
    for reason in PROTECTION_ORDER:
        if claims[reason]:
            return reason
    return None


# -- the population pass ----------------------------------------------------------------


#: How many contacts must advertise a position before their median is trusted as the
#: distance term's midpoint. Below this the term is dropped for everyone (it would be
#: calibrated against one or two nodes), which the median-fill then handles as any other
#: unknown.
_MIN_LOCATED = 4

#: The term names, in the order a :class:`ScoredContact` reports its reasons.
_TERM_NAMES = ("dm", "recency", "volume", "channel", "hops", "distance")


def _fill_unknowns(column: list[float | None]) -> list[float]:
    """Replace every ``None`` in one term's column with the median of the known values.

    THE rule that keeps the sweep honest (see the module docstring): a contact that could
    not be measured on an axis lands exactly where an average peer lands on it, so the
    decision falls to the axes that *were* measurable. A column nobody could compute
    collapses to ``0.5`` throughout — neutral, and therefore inert once weighted, since it
    then shifts every contact by the same amount and changes no ordering.

    Args:
        column: One term's value per contact, with ``None`` for unknown.

    Returns:
        The same column with the gaps filled.
    """
    known = [v for v in column if v is not None]
    fill = median(known) if known else 0.5
    return [fill if v is None else v for v in column]


def _self_distance(
    contact: Contact, self_lat: float | None, self_lon: float | None
) -> float | None:
    """Great-circle km from our own node to ``contact``, or ``None`` if either end is unplaced."""
    if self_lat is None or self_lon is None or not contact.has_location:
        return None
    return haversine_km(self_lat, self_lon, float(contact.lat), float(contact.lon))


def percentile_rank(score: float, population: Sequence[float]) -> int:
    """The percentile rank of ``score`` within ``population``, as an integer ``0``–``100``.

    Uses the standard mid-rank definition — everything strictly below, plus half of
    everything equal — so a field of identical scores reads ``50`` for all of them rather
    than ``0`` or ``100``, and the median contact reads about ``50``.

    Args:
        score: The value to place.
        population: Every score in the population, ``score`` included.

    Returns:
        The percentile rank, rounded to an integer.
    """
    if not population:
        return 0
    below = sum(1 for value in population if value < score)
    equal = sum(1 for value in population if value == score)
    return int(round(100.0 * (below + 0.5 * equal) / len(population)))


def _reasons(
    signals: ContactSignals, terms: dict[str, float], weights: ScoreWeights
) -> tuple[str, ...]:
    """Name what this contact's standing rests on, weakest lane first.

    The preview has one line per contact and the reader should not have to know the formula
    to audit a sweep — so each row says, in plain words, the two things that put it where it
    is. Phrases are drawn from the *measured* signals rather than from the term values, so
    "3 packets" is the actual tally and not a normalised fraction.
    """
    phrases: list[tuple[float, str]] = []
    contribution = {
        "dm": terms["dm"] * weights.dm,
        "recency": terms["recency"] * weights.recency,
        "volume": terms["volume"] * weights.volume,
        "hops": terms["hops"] * weights.hops,
    }
    if signals.dm_total:
        phrases.append((contribution["dm"], f"{signals.dm_total} messages"))
    else:
        phrases.append((0.0, "never messaged"))
    if signals.heard_age_days is None:
        phrases.append((0.0, "never heard"))
    else:
        phrases.append((contribution["recency"], _age_phrase(signals.heard_age_days)))
    packets = signals.packets
    phrases.append((contribution["volume"], f"{packets} pkt{'' if packets == 1 else 's'}"))
    if signals.hops is not None:
        hops = signals.hops
        label = "direct" if hops < 0.5 else f"{hops:g} hops"
        phrases.append((contribution["hops"], label))
    phrases.sort(key=lambda pair: pair[0])
    return tuple(phrase for _, phrase in phrases[:3])


def _age_phrase(days: float) -> str:
    """A last-heard age in the fewest words: ``today`` / ``4d`` / ``6w`` / ``9mo`` / ``2y``."""
    if days < 1:
        return "today"
    if days < 14:
        return f"{int(days)}d"
    if days < 60:
        return f"{int(days / 7)}w"
    if days < 365:
        return f"{int(days / 30)}mo"
    return f"{days / 365:.0f}y"


def rank_contacts(
    contacts: Sequence[Contact],
    signals: dict[str, ContactSignals],
    *,
    self_lat: float | None = None,
    self_lon: float | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> list[ScoredContact]:
    """Score and rank a whole contact table, strongest first.

    One pass to compute every term (leaving unknowns as ``None``), one pass to fill each
    term's gaps with that term's population median, then the weighted sum plus grace. The
    percentile is taken over the **whole** population — protected contacts included, since
    they are real contacts and a rank that silently excluded them would not be a rank
    against your contacts at all.

    Args:
        contacts: The contacts to rank (our own node is not among them).
        signals: Gathered evidence keyed by 12-hex node id; a contact with no entry is
            scored from an empty :class:`ContactSignals`, which protects it as unobserved.
        self_lat: Our own node's advertised latitude, if it has one.
        self_lon: Our own node's advertised longitude, if it has one.
        weights: The weighting to score under.

    Returns:
        One :class:`ScoredContact` per input contact, highest score first. Ties break by
        name so the order is stable between runs.
    """
    if not contacts:
        return []

    gathered: list[ContactSignals] = []
    for contact in contacts:
        node = _node_id(contact)
        found = signals.get(node) or ContactSignals(node=node)
        gathered.append(
            replace(found, distance_km=_self_distance(contact, self_lat, self_lon))
        )

    # The distance term calibrates against this mesh's own spread, so it needs the whole
    # population before any single contact can be scored on it.
    located = [s.distance_km for s in gathered if s.distance_km is not None]
    midpoint = median(located) if len(located) >= _MIN_LOCATED else None

    columns: dict[str, list[float | None]] = {
        "dm": [term_dm(s, weights) for s in gathered],
        "recency": [term_recency(s, weights) for s in gathered],
        "volume": [term_volume(s, weights) for s in gathered],
        "channel": [term_channel(s, weights) for s in gathered],
        "hops": [term_hops(s, weights) for s in gathered],
        "distance": [term_distance(s, midpoint) for s in gathered],
    }
    filled = {name: _fill_unknowns(column) for name, column in columns.items()}

    scores: list[float] = []
    per_contact_terms: list[dict[str, float]] = []
    for index, sig in enumerate(gathered):
        terms = {name: filled[name][index] for name in _TERM_NAMES}
        total = (
            terms["dm"] * weights.dm
            + terms["recency"] * weights.recency
            + terms["volume"] * weights.volume
            + terms["channel"] * weights.channel
            + terms["hops"] * weights.hops
            + terms["distance"] * weights.distance
            + term_grace(sig, weights)
        )
        per_contact_terms.append(terms)
        scores.append(total)

    ranked = [
        ScoredContact(
            contact=contact,
            signals=sig,
            score=score,
            percentile=percentile_rank(score, scores),
            protection=protection_for(sig),
            reasons=_reasons(sig, terms, weights),
        )
        for contact, sig, score, terms in zip(
            contacts, gathered, scores, per_contact_terms
        )
    ]
    ranked.sort(key=lambda scored: (-scored.score, scored.contact.name.casefold()))
    return ranked


def sweep_candidates(ranked: Sequence[ScoredContact], keep: int) -> list[ScoredContact]:
    """The contacts a "keep the strongest ``keep``" sweep would actually remove, weakest first.

    Protected contacts are never candidates, and — the subtlety — they **do not consume**
    one of the kept slots: a table of 100 with 30 protected and ``keep=50`` sweeps the 50
    weakest *sweepable* contacts, leaving 50 unprotected plus the 30 protected. Counting
    protections against the target instead would mean starring a node quietly deepened the
    next sweep, which is the opposite of what a star is for. The picker's rungs therefore
    report their own real removal counts rather than arithmetic on the table size (see
    :func:`~meshterm.ui.purge_screen.purge_contacts`).

    Args:
        ranked: The full ranking from :func:`rank_contacts`, strongest first.
        keep: How many unprotected contacts to keep.

    Returns:
        The victims, weakest first — so the preview reads bottom-up, and a truncated read
        of it still shows the ones going first.
    """
    sweepable = [scored for scored in ranked if not scored.protected]
    if keep >= len(sweepable):
        return []
    victims = sweepable[keep:] if keep > 0 else list(sweepable)
    return list(reversed(victims))


def _node_id(contact: Contact) -> str:
    """The 12-hex canonical id a contact's history is keyed by (see ``observations.node``)."""
    ident = contact.public_key or contact.key_prefix or ""
    return ident.lower().removeprefix("0x")[:12]
