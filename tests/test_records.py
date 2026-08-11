"""Unit tests for the trophy-case records: walk scoring, derivation, and persistence.

The scoring layer is pure measurement (the UI owns the radio), so everything here runs
headless: hand-built trace results, synthetic positions, and a temp repository for the
leaderboard invariants.
"""

from __future__ import annotations

from meshterm.core.models import Hop, TraceResult
from meshterm.persistence.repository import Repository
from meshterm.services.records import (
    CATEGORY_BY_ID,
    compute_walk_stats,
    first_repeated_edge,
    max_hops,
    walk_from_trace,
    walk_scores,
)

#: Montréal-ish positions ~1.1 km apart, so distance scores are easy to sanity-check.
SELF_POS = (45.500, -73.600)
HUB_ID, FAR_ID, LEAF_ID = "3d63c6429436", "f2c24f54551e", "27d4396a2967"
POSITIONS = {
    HUB_ID: (45.510, -73.600),   # ~1.11 km north of us
    FAR_ID: (45.510, -73.585),   # north-east of us
    LEAF_ID: (45.490, -73.600),  # ~1.11 km south of us
}


def _result(*snrs: float, rtt: float = 250.0, nodes=None) -> TraceResult:  # noqa: ANN001
    """A successful walk reply: one hop per SNR, the last one hash-less (us)."""
    hops = []
    for i, snr in enumerate(snrs):
        node = None if i == len(snrs) - 1 else (nodes[i] if nodes else f"{i:02x}")
        hops.append(Hop(index=i, node=node, snr=snr))
    return TraceResult(target="walk", success=True, hops=hops, round_trip_ms=rtt)


# --- scoring ---------------------------------------------------------------------


def test_walk_stats_measure_distance_reach_and_area() -> None:
    """The circuit us→Hub→Far→us is positioned end to end: exact km, far, and area."""
    stats = compute_walk_stats(
        (HUB_ID, FAR_ID), _result(8.0, 6.0, 7.0).hops, rtt_ms=250.0,
        positions=POSITIONS, self_pos=SELF_POS,
    )
    assert stats.km_complete
    assert stats.km_travelled > 2.0  # up ~1.1 km, across, and back home
    assert stats.far_km is not None and 1.0 < stats.far_km < 2.5
    assert stats.area_km2 is not None and stats.area_km2 > 0.3
    assert stats.distinct_nodes == 2 and not stats.repeats
    assert stats.min_snr == 6.0


def test_walk_stats_unpositioned_segments_score_a_lower_bound() -> None:
    """A hop with no position contributes 0 km and clears the complete flag."""
    positions = {HUB_ID: POSITIONS[HUB_ID]}  # Far is off the map
    stats = compute_walk_stats(
        (HUB_ID, FAR_ID), _result(8.0, 6.0, 7.0).hops, rtt_ms=None,
        positions=positions, self_pos=SELF_POS,
    )
    assert not stats.km_complete
    assert 1.0 < stats.km_travelled < 1.3  # only the positioned us→Hub leg counts


def test_longest_leg_measures_one_link_and_names_its_ends() -> None:
    """The longest single segment of the circuit, with the two nodes it spanned.

    Hub sits ~1.11 km north and Leaf ~1.11 km south, so the Hub→Leaf crossing (~2.2 km)
    beats both of the legs touching us — a link between two hops, neither of them ours.
    """
    stats = compute_walk_stats(
        (HUB_ID, LEAF_ID), _result(8.0, 6.0, 7.0).hops, rtt_ms=None,
        positions=POSITIONS, self_pos=SELF_POS,
    )
    assert stats.leg_km is not None and 2.0 < stats.leg_km < 2.5
    assert stats.leg_link == (HUB_ID, LEAF_ID)
    # Far point measures reach, not span: the furthest node is only half as far away.
    assert stats.far_km is not None and stats.far_km < stats.leg_km
    assert stats.as_dict()["leg_link"] == [HUB_ID, LEAF_ID]  # JSON-shaped for storage


def test_longest_leg_names_our_own_end_as_none() -> None:
    """A leg that leaves or comes home marks our end ``None`` — the ★ the UI draws."""
    stats = compute_walk_stats(
        (HUB_ID,), _result(8.0, 7.0).hops, rtt_ms=None,
        positions=POSITIONS, self_pos=SELF_POS,
    )
    assert stats.leg_link == (None, HUB_ID)  # us → Hub, the outbound half


def test_longest_leg_needs_two_positioned_ends() -> None:
    """No segment with both ends placed scores nothing rather than a bogus zero."""
    stats = compute_walk_stats(
        (HUB_ID,), _result(8.0, 7.0).hops, rtt_ms=None,
        positions=POSITIONS, self_pos=None,
    )
    assert stats.leg_km is None and stats.leg_link is None
    assert "long_leg" not in walk_scores(stats)


def test_cross_category_scores_score_every_game_at_once() -> None:
    """One walk competes everywhere it can — the every-board-at-once rule."""
    stats = compute_walk_stats(
        (HUB_ID, FAR_ID, HUB_ID), _result(8.0, -11.0, 7.0, 9.0).hops, rtt_ms=100.0,
        positions=POSITIONS, self_pos=SELF_POS,
    )
    scores = walk_scores(stats)
    assert scores["grand_tour"] == 2.0          # two distinct nodes, revisit allowed
    assert "clean_trail" not in scores          # Hub repeats: disqualified, not zeroed
    assert scores["thin_thread"] == -11.0       # the weakest surviving link
    assert scores["long_haul"] > 3.0
    assert scores["far_point"] > 1.0
    assert scores["long_leg"] > 0.5


def test_category_titles_are_plain_and_ids_are_stable() -> None:
    """Ids are database keys (unchanged); titles read plainly for the trophy case."""
    assert CATEGORY_BY_ID["grand_tour"].title == "Most nodes"
    assert CATEGORY_BY_ID["thin_thread"].title == "Weakest link"
    assert CATEGORY_BY_ID["long_haul"].title == "Longest distance"
    # Every id still resolves — the persisted keys never changed under the rename.
    assert set(CATEGORY_BY_ID) == {
        "long_haul", "far_point", "long_leg", "grand_tour", "clean_trail", "thin_thread",
        "big_loop",
    }


def test_max_hops_follows_the_64_byte_path_field() -> None:
    """The width bounds the walk: 64 hops at 1 byte, 16 at 4."""
    assert max_hops(1) == 64
    assert max_hops(2) == 32
    assert max_hops(4) == 16


# --- the no-cheat rule (a record walk must be a trail) -------------------------------


def test_first_repeated_edge_flags_only_a_same_direction_recross() -> None:
    """a→b twice breaks the trail; the boomerang's b→a return leg never does."""
    assert first_repeated_edge(("a", "b", "c")) is None
    # A target boomerang retraces every link backwards by design: still a trail.
    assert first_repeated_edge(("a", "b", "t", "b", "a")) is None
    assert first_repeated_edge(("a", "b", "a", "b")) == ("a", "b")
    assert first_repeated_edge(("A ", "b", "a", "B")) == ("a", "b")  # case/space folded
    assert first_repeated_edge(()) is None


def test_walk_scores_disqualify_a_non_trail_from_every_board() -> None:
    """Riding a link twice the same way pumps km and hops for free — every board says no."""
    stats = compute_walk_stats(
        (HUB_ID, FAR_ID, HUB_ID, FAR_ID),  # Hub→Far ridden twice
        _result(8.0, 6.0, 7.0, 5.0, 9.0).hops, rtt_ms=100.0,
        positions=POSITIONS, self_pos=SELF_POS,
    )
    assert stats.km_travelled > 0  # it would have scored…
    assert walk_scores(stats) == {}  # …but the arbiter disqualifies it outright


# --- deriving a walk from a trace ---------------------------------------------------


def test_walk_from_trace_derives_spec_route_and_stats() -> None:
    """A successful reply yields its re-walkable spec, canonical route, and stats."""
    result = _result(8.0, 6.0, 7.0, nodes=["3d", "f2"])  # two relays, then us
    canon = {"3d": HUB_ID, "f2": FAR_ID}
    derived = walk_from_trace(
        result, canonical=lambda h: canon.get(h),
        positions=POSITIONS, self_pos=SELF_POS,
    )
    assert derived is not None
    spec, route, stats = derived
    assert spec == "3d,f2"                 # the reply hops, joined — re-walkable verbatim
    assert route == (HUB_ID, FAR_ID)       # canonicalized for stable display + geometry
    assert stats.km_complete and stats.far_km is not None


def test_walk_from_trace_ignores_a_failure_or_a_reply_with_no_relays() -> None:
    """A failed trace, or one that only came back off us, scores nothing."""
    failed = TraceResult(target="walk", success=False, hops=[])
    assert walk_from_trace(failed, canonical=lambda h: h, positions={}, self_pos=None) is None
    # A direct-to-us reply carries only the hash-less homecoming hop: no relay to score.
    direct = TraceResult(target="walk", success=True, hops=[Hop(index=0, node=None, snr=9.0)])
    assert walk_from_trace(direct, canonical=lambda h: h, positions={}, self_pos=None) is None


# --- the leaderboards ----------------------------------------------------------------


def test_leaderboard_keeps_five_dedupes_and_prunes_the_worst(tmp_path) -> None:  # noqa: ANN001
    """Placement, spec dedupe, and pruning hold the top-5 invariant."""
    repo = Repository(tmp_path / "t.db")
    try:
        for i in range(6):
            repo.record_discovery(
                "grand_tour", 1, f"a{i},b{i}", (f"a{i}", f"b{i}"),
                score=float(i), stats={}, app_version="0.1.0",
            )
        rows = repo.discoveries("grand_tour", width_bytes=1)
        assert len(rows) == 5
        assert min(r.score for r in rows) == 1.0  # the 0-score walk fell off
        # Re-walking a stored spec only ever *improves* its row.
        assert repo.record_discovery(
            "grand_tour", 1, "a5,b5", ("a5", "b5"),
            score=2.0, stats={}, app_version="0.1.0",
        ) is None
        improved = repo.record_discovery(
            "grand_tour", 1, "a5,b5", ("a5", "b5"),
            score=9.0, stats={"hop_count": 2}, app_version="0.1.1",
        )
        assert improved is not None
        rows = repo.discoveries("grand_tour", width_bytes=1)
        assert len(rows) == 5
        top = max(rows, key=lambda r: r.score)
        assert top.score == 9.0 and top.app_version == "0.1.1"
    finally:
        repo.close()


def test_leaderboard_ascends_for_thin_thread(tmp_path) -> None:  # noqa: ANN001
    """Weakest link keeps the *lowest* scores: a strong link never places."""
    repo = Repository(tmp_path / "t.db")
    try:
        for i, snr in enumerate((-2.0, -8.0, -5.0, -11.0, -3.0)):
            repo.record_discovery(
                "thin_thread", 1, f"c{i}", (f"c{i}",),
                score=snr, stats={}, app_version="0.1.0", ascending=True,
            )
        strong = repo.record_discovery(
            "thin_thread", 1, "c9", ("c9",),
            score=7.0, stats={}, app_version="0.1.0", ascending=True,
        )
        assert strong is None  # +7 dB is a great link and a terrible record
        rows = repo.discoveries("thin_thread", width_bytes=1)
        assert len(rows) == 5 and max(r.score for r in rows) == -2.0
    finally:
        repo.close()


def test_record_deletion_by_row_category_and_wholesale(tmp_path) -> None:  # noqa: ANN001
    """The three deletion grains: one record, one category, everything."""
    repo = Repository(tmp_path / "t.db")
    try:
        for category in ("grand_tour", "long_haul"):
            for width in (1, 2):
                repo.record_discovery(
                    category, width, f"{category[:2]},{width}", ("x",),
                    score=1.0, stats={}, app_version="0.1.0",
                )
        first = repo.discoveries("grand_tour", width_bytes=1)[0]
        assert repo.delete_discovery(first.id)
        assert not repo.delete_discovery(first.id)  # already gone
        assert repo.delete_discoveries("grand_tour") == 1  # the width-2 row
        assert repo.discoveries("grand_tour") == []
        assert repo.delete_discoveries() == 2  # long_haul at both widths
        assert repo.discoveries() == []
    finally:
        repo.close()
