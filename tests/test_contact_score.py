"""Tests for rating contacts by how much they are worth a device slot (``core.contact_score``).

The scoring is pure — no device, no database, no screen — so these exercise the rules the
sweep's safety actually rests on: that an unknown signal is neutral rather than damning,
that an explicit choice outranks the arithmetic, that a newcomer is given time, and that
what a caller reads back is a percentile rather than a raw number.
"""

from __future__ import annotations

from meshterm.core.contact_score import (
    PROTECT_ADMIN,
    PROTECT_MESSAGED,
    PROTECT_UNOBSERVED,
    PROTECT_WATCHED,
    ContactSignals,
    percentile_rank,
    protection_for,
    rank_contacts,
    sweep_candidates,
    term_channel,
    term_distance,
    term_grace,
    term_hops,
)
from meshterm.core.models import Contact

#: A contact whose evidence every test then varies one axis of.
_BASE = dict(heard_age_days=30.0, packets=10, known_days=200.0)


def _contact(name: str, *, lat=None, lon=None) -> Contact:  # noqa: ANN001
    """A contact with a distinct key (so its hue and its node id differ from its neighbours')."""
    byte = f"{(sum(map(ord, name)) % 256):02x}"
    return Contact(
        name=name, public_key=byte * 32, key_prefix=byte * 6, lat=lat, lon=lon
    )


def _rank(*specs) -> list:  # noqa: ANN001
    """Rank ``(name, signal-overrides)`` pairs, returning the scored contacts strongest first."""
    contacts = [_contact(name) for name, _ in specs]
    signals = {}
    for contact, (_, overrides) in zip(contacts, specs, strict=True):
        node = contact.public_key[:12]
        signals[node] = ContactSignals(node=node, **{**_BASE, **overrides})
    return rank_contacts(contacts, signals)


def _by_name(ranked) -> dict:  # noqa: ANN001
    """The ranking keyed by contact name, for asserting on one row at a time."""
    return {scored.contact.name: scored for scored in ranked}


def test_an_unknown_signal_scores_neutral_not_zero() -> None:
    """A contact that advertises no position lands mid-field on that axis, not at the bottom.

    THE rule the whole sweep's fairness rests on. About half a real mesh advertises no
    location at all, plenty of nodes are never overheard as a relayed packet so carry no hop
    count, and a channel poster whose name matches no contact can't be attributed. If any of
    those resolved to zero, every unmeasurable contact would sink together and the sweep
    would be purging by how much metadata a node happens to broadcast.
    """
    # Two contacts identical in every measured way; one is simply unplaced and unrouted.
    ranked = _by_name(
        _rank(
            ("Placed", {"hops": 2.0}),
            ("Unplaced", {"hops": None}),
            # Two more placed contacts so the hop column has a median to fill from.
            ("Near", {"hops": 1.0}),
            ("Far", {"hops": 3.0}),
        )
    )
    # The unmeasured contact sits exactly where the median of the measured ones puts it —
    # between the near and the far, not beneath both.
    assert ranked["Near"].score > ranked["Unplaced"].score > ranked["Far"].score
    # And it is emphatically not at the bottom of the field.
    assert ranked["Unplaced"].percentile > 0


def test_a_column_nobody_can_measure_changes_no_ordering() -> None:
    """When *no* contact can be scored on an axis, that axis shifts everyone equally.

    The fill collapses to a flat 0.5, which is inert once weighted: it moves every score by
    the same amount, so a signal the whole mesh is silent about can never reorder the field.
    """
    ranked = _by_name(_rank(("A", {"packets": 40}), ("B", {"packets": 3})))
    # Nobody carries hops, distance or channel attribution here, and the packet counts still
    # decide it.
    assert ranked["A"].score > ranked["B"].score


def test_an_unattributable_channel_name_reads_unknown_not_silent() -> None:
    """A contact whose channel posts can't be attributed is unmeasured, not proven quiet.

    A channel frame carries no sender key — only a ``Name: `` prefix — so a name two
    contacts share attributes to neither. That has to read as "we don't know", because
    reading it as "posted nothing" would demote a node for a property of its *name*.
    """
    unattributed = ContactSignals(node="a" * 12, channel_attributed=False, channel_posts=0)
    silent = ContactSignals(node="b" * 12, channel_attributed=True, channel_posts=0)
    from meshterm.core.contact_score import DEFAULT_WEIGHTS

    assert term_channel(unattributed, DEFAULT_WEIGHTS) is None
    assert term_channel(silent, DEFAULT_WEIGHTS) == 0.0


def test_distance_calibrates_against_this_mesh_rather_than_a_constant() -> None:
    """The distance midpoint is the population's own median, so it means the same everywhere.

    50 km is far on a downtown mesh and unremarkable in a valley; a hardcoded threshold
    would have to be wrong on one of them.
    """
    near = ContactSignals(node="a" * 12, distance_km=5.0)
    # On a tight mesh (median 5 km) a 5 km contact is average; on a sparse one (median
    # 100 km) the same contact is *close*, and scores higher.
    assert term_distance(near, 100.0) > term_distance(near, 5.0)
    # Too few located contacts for a median to mean anything: the term goes unknown.
    assert term_distance(near, None) is None


def test_a_newcomer_is_given_a_fortnight_to_earn_its_keep() -> None:
    """The grace bonus lifts a brand-new contact clear of the sweep, then fades to nothing.

    Every other term rewards accumulated evidence, so a node first heard this morning is
    arithmetically indistinguishable from one that has been quiet for a year — both have one
    advert and no history.
    """
    from meshterm.core.contact_score import DEFAULT_WEIGHTS as w

    assert term_grace(ContactSignals(node="a" * 12, known_days=0.0), w) == w.grace
    # Half-way through, half the bonus.
    assert term_grace(ContactSignals(node="a" * 12, known_days=7.0), w) == w.grace / 2
    # Past the window it is gone, and the contact competes on merit.
    assert term_grace(ContactSignals(node="a" * 12, known_days=14.0), w) == 0.0
    assert term_grace(ContactSignals(node="a" * 12, known_days=400.0), w) == 0.0

    # End to end: a newcomer heard once outranks an equally thin contact known for months.
    ranked = _by_name(
        _rank(
            ("Newcomer", {"packets": 1, "heard_age_days": 0.1, "known_days": 1.0}),
            ("Stale", {"packets": 1, "heard_age_days": 0.1, "known_days": 300.0}),
        )
    )
    assert ranked["Newcomer"].score > ranked["Stale"].score


def test_protections_outrank_the_arithmetic() -> None:
    """A star, a sent message, an admin login or an unheard contact is never a candidate.

    A heuristic must not overrule an explicit choice, and it must not punish a contact for
    MeshTerm's own ignorance.
    """
    assert protection_for(ContactSignals(node="a" * 12, **_BASE, watched=True)) == PROTECT_WATCHED
    assert (
        protection_for(ContactSignals(node="a" * 12, **_BASE, dm_outbound=1))
        == PROTECT_MESSAGED
    )
    assert protection_for(ContactSignals(node="a" * 12, **_BASE, has_admin=True)) == PROTECT_ADMIN
    # No arrival time at all: the contact predates this history (or was added by hand), so
    # every evidence term is empty for a reason that says nothing about the node.
    assert protection_for(ContactSignals(node="a" * 12)) == PROTECT_UNOBSERVED
    # Ordinary evidence, no claims: the score decides.
    assert protection_for(ContactSignals(node="a" * 12, **_BASE)) is None


def test_inbound_messages_alone_do_not_protect() -> None:
    """Being messaged is not choosing to talk — anyone on the mesh can message you."""
    inbound_only = ContactSignals(node="a" * 12, **_BASE, dm_total=20, dm_outbound=0)
    assert protection_for(inbound_only) is None


def test_a_protected_contact_still_ranks_but_is_never_swept() -> None:
    """Protections are excluded from the victims, not from the population the rank is against.

    A percentile that silently dropped them would not be a rank against *your contacts*.
    """
    ranked = _rank(
        ("Strong", {"packets": 90, "heard_age_days": 0.1}),
        ("Starred", {"packets": 1, "heard_age_days": 500.0, "watched": True}),
        ("Weak", {"packets": 1, "heard_age_days": 500.0}),
        ("Weaker", {"packets": 0, "heard_age_days": 900.0}),
    )
    by_name = _by_name(ranked)
    assert by_name["Starred"].protected
    assert by_name["Starred"].percentile is not None  # it is ranked like everyone else
    # Keeping one unprotected contact sweeps the other two — never the starred one, however
    # badly it scores.
    victims = {v.contact.name for v in sweep_candidates(ranked, keep=1)}
    assert "Starred" not in victims
    assert victims == {"Weak", "Weaker"}


def test_a_protection_does_not_consume_a_kept_slot() -> None:
    """``keep`` counts unprotected contacts, so starring a node never deepens the next sweep.

    Counting protections against the target would mean a star quietly cost some other
    contact its place, which is the opposite of what a star is for.
    """
    ranked = _rank(
        ("Starred", {"watched": True}),
        ("A", {"packets": 50}),
        ("B", {"packets": 20}),
        ("C", {"packets": 1}),
    )
    # Three sweepable contacts, keep two: exactly one goes, and the star is untouched.
    victims = sweep_candidates(ranked, keep=2)
    assert [v.contact.name for v in victims] == ["C"]


def test_victims_come_back_weakest_first() -> None:
    """The preview is read top-down, so the first rows are the least likely to be missed."""
    ranked = _rank(
        ("Best", {"packets": 90}),
        ("Middle", {"packets": 20}),
        ("Worst", {"packets": 0, "heard_age_days": 900.0}),
    )
    assert [v.contact.name for v in sweep_candidates(ranked, keep=1)] == ["Worst", "Middle"]


def test_percentile_is_a_mid_rank_so_a_flat_field_reads_fifty() -> None:
    """Ties share their rank rather than all reading 0 or 100 — the standard definition."""
    assert percentile_rank(5.0, [5.0, 5.0, 5.0]) == 50
    assert percentile_rank(9.0, [1.0, 2.0, 9.0]) == 83
    assert percentile_rank(1.0, [1.0, 2.0, 9.0]) == 17
    assert percentile_rank(1.0, []) == 0


def test_direct_correspondence_is_the_heaviest_single_signal() -> None:
    """No other lane moves a score as far — a conversation took a decision on both ends.

    Heaviest *single* lane, deliberately not heavier than two strong ones combined: a node
    heard constantly and heard often is genuinely worth a slot, and one axis that could
    overrule two others outright would be a weighting with a single point of failure.

    Note what this comparison is really about. A contact you have *replied* to is protected
    outright (:data:`~meshterm.core.contact_score.PROTECT_MESSAGED`) and never reaches the
    arithmetic at all, so the DM term does its work on inbound-only history — someone who
    messaged you and got no answer — which is exactly the case where it should count for a
    lot but not for everything.
    """
    quiet = {"packets": 2, "heard_age_days": 60.0}
    ranked = _by_name(
        _rank(
            ("Correspondent", {**quiet, "dm_total": 30, "dm_age_days": 20.0}),
            ("Chatty", {**quiet, "channel_attributed": True, "channel_posts": 100}),
            ("Near", {**quiet, "hops": 0.0}),
            ("Nobody", quiet),
        )
    )
    # Against contacts alike in every other way, the conversation wins by the widest margin.
    base = ranked["Nobody"].score
    assert ranked["Correspondent"].score - base > ranked["Chatty"].score - base
    assert ranked["Correspondent"].score - base > ranked["Near"].score - base


def test_no_single_signal_overrules_two_strong_ones() -> None:
    """A node heard constantly *and* heard often outranks one lane maxed out on its own.

    The reason the score is a weighted sum with a bounded heaviest term rather than a
    ranking with a dominant key: a contact that is genuinely present on the mesh should not
    be swept because it never sent a direct message.
    """
    ranked = _by_name(
        _rank(
            ("Present", {"packets": 100, "heard_age_days": 0.1, "dm_total": 0}),
            ("Inbox", {"packets": 2, "heard_age_days": 60.0, "dm_total": 30,
                       "dm_age_days": 20.0}),
        )
    )
    assert ranked["Present"].score > ranked["Inbox"].score


def test_a_quiet_conversation_keeps_most_of_its_worth() -> None:
    """Correspondence ages far more slowly than adverts: it is a relationship, not an event."""
    ranked = _by_name(
        _rank(
            ("Recent", {"dm_total": 10, "dm_age_days": 1.0}),
            ("Lapsed", {"dm_total": 10, "dm_age_days": 120.0}),
            ("Never", {"dm_total": 0}),
        )
    )
    assert ranked["Recent"].score > ranked["Lapsed"].score
    # …but the lapsed conversation is still worth far more than none at all.
    gap_to_recent = ranked["Recent"].score - ranked["Lapsed"].score
    gap_to_never = ranked["Lapsed"].score - ranked["Never"].score
    assert gap_to_never > gap_to_recent


def test_fewer_hops_ranks_higher() -> None:
    """Topological closeness, hyperbolic: zero-to-one matters, four-to-five barely does."""
    from meshterm.core.contact_score import DEFAULT_WEIGHTS as w

    direct = term_hops(ContactSignals(node="a" * 12, hops=0.0), w)
    one = term_hops(ContactSignals(node="a" * 12, hops=1.0), w)
    four = term_hops(ContactSignals(node="a" * 12, hops=4.0), w)
    five = term_hops(ContactSignals(node="a" * 12, hops=5.0), w)
    assert direct == 1.0
    assert direct - one > four - five


def test_ranking_an_empty_table_is_empty_not_an_error() -> None:
    """A device with no contacts has nothing to rank — the sweep's own guard reads this."""
    assert rank_contacts([], {}) == []


def test_a_contact_with_no_signals_at_all_is_protected() -> None:
    """A contact the history has never met is protected rather than swept as worthless."""
    contact = _contact("Inherited")
    ranked = rank_contacts([contact], {})
    assert ranked[0].protection == PROTECT_UNOBSERVED


def test_reasons_name_the_weakest_lanes_in_plain_words() -> None:
    """Each preview row explains itself without the reader having to know the formula."""
    ranked = _rank(("Quiet", {"packets": 3, "heard_age_days": 240.0, "dm_total": 0}))
    reasons = ranked[0].reasons
    assert "never messaged" in reasons
    assert "3 pkts" in reasons
    # The age reads in the fewest words that are still true — months, here.
    assert any(r.endswith("mo") for r in reasons)


def test_a_stronger_contact_never_scores_below_a_weaker_one_on_every_axis() -> None:
    """Sanity: dominance is preserved — better everywhere means ranked above."""
    ranked = _by_name(
        _rank(
            ("Better", {"packets": 50, "heard_age_days": 1.0, "dm_total": 5,
                        "dm_age_days": 1.0, "hops": 1.0}),
            ("Worse", {"packets": 2, "heard_age_days": 300.0, "dm_total": 0,
                       "hops": 4.0}),
        )
    )
    assert ranked["Better"].score > ranked["Worse"].score
    assert ranked["Better"].percentile > ranked["Worse"].percentile
