"""Unit tests for the observed-topology graph, path scenarios, and the path composer.

The graph is pure data-in/data-out, so these run headless: evidence rows are built by
hand (mirroring what the repository returns) and the assertions cover canonicalization,
bidirectional link folding, strength ordering, scenario ranking, and the composer's
step-by-step state machine.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import meshterm.ui.pathline as pathline
from meshterm.core.connection import DeviceCommandError, MockDevice
from meshterm.core.models import Contact, NeighbourInfo, Observation, utcnow
from meshterm.persistence.repository import (
    NeighbourLink,
    PacketPath,
    Repository,
    TracedPath,
)
from meshterm.services.path_probe import ProbeCandidate, probe_paths
from meshterm.services.topology import build_topology, collapse_width, render_custom_spec
from meshterm.services.trace_runner import make_node_resolver
from meshterm.ui.path_composer import AUTO_SPEC, FetchNeighbours, PathComposerScreen
from meshterm.ui.pathline import CURSOR_GLYPH, SELF_GLYPH
from meshterm.ui.tui.screen import CANCEL

US = "aaaaaaaaaaaa"
REPEATER = Contact(name="Hub", public_key="3d63c6429436" + "0" * 52, key_prefix="3d63c6429436")
FAR = Contact(name="Far", public_key="f2c24f54551e" + "0" * 52, key_prefix="f2c24f54551e")
LEAF = Contact(name="Leaf", public_key="27d4396a2967" + "0" * 52, key_prefix="27d4396a2967")


def _topo(trace_paths=(), packet_paths=(), neighbour_links=(), contacts=None):  # noqa: ANN001
    return build_topology(
        self_id=US + "0" * 52,
        contacts=list(contacts) if contacts is not None else [REPEATER, FAR, LEAF],
        trace_paths=list(trace_paths),
        packet_paths=list(packet_paths),
        neighbour_links=list(neighbour_links),
    )


def _traced(*hops, age_hours: float = 0.0) -> TracedPath:
    """A trace walk from ``(node_hash, snr)`` pairs; the final hash-less hop is us."""
    return TracedPath(when=utcnow() - timedelta(hours=age_hours), hops=list(hops))


# --- graph construction ---------------------------------------------------------


def test_trace_walk_yields_bidirectional_links_at_canonical_ids() -> None:
    """A boomerang trace folds out+return readings onto the same undirected links."""
    topo = _topo(
        trace_paths=[_traced(("3d", 12.5), ("f2", -5.5), ("3d", -6.5), (None, 11.75))]
    )
    # us↔Hub crossed twice (first hop out, last hop home), Hub↔Far twice as well.
    near = topo.link(topo.self_id, "3d63c6429436")
    far = topo.link("3d63c6429436", "f2c24f54551e")
    assert near is not None and near.samples == 2
    assert sorted(near.snrs) == [11.75, 12.5]
    assert far is not None and far.samples == 2
    assert sorted(far.snrs) == [-6.5, -5.5]


def test_canonicalization_maps_prefixes_and_rejects_junk() -> None:
    """Any-width hashes collapse onto contact ids; ambiguity and non-hex stay honest."""
    topo = _topo()
    assert topo.canonical("3d") == "3d63c6429436"
    assert topo.canonical("3d63c6429436" + "0" * 20) == "3d63c6429436"
    assert topo.canonical("hop0") is None  # legacy simulator artefact, not hex
    ambiguous = _topo(contacts=[REPEATER, Contact(name="Twin", public_key="3d99" + "0" * 60)])
    assert ambiguous.canonical("3d") == "3d"  # two contacts match: keep the short id


def test_coalesce_folds_a_short_hop_into_its_only_wide_match() -> None:
    """A bare hop beside the same node's wide id collapses to one node, its evidence pooled."""
    twin = Contact(name="Twin", public_key="27e4" + "0" * 60)  # makes a bare "27" ambiguous
    walks = [
        _traced(("27", 5.0), ("f2", -3.0), ("27", -3.5), (None, 5.0)),      # us→27→Far
        _traced(("27d4", 7.0), ("f2", -4.0), ("27d4", -4.5), (None, 7.0)),  # us→27d4→Far
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    assert "27" not in ids  # the 1-byte stub folded onto its only wide match in the graph
    assert "27d4396a2967" in ids
    link = topo.link(topo.self_id, "27d4396a2967")
    assert link is not None and link.samples == 4  # both walks' out+back readings pooled


def test_coalesce_leaves_a_genuinely_ambiguous_stub_alone() -> None:
    """A short hop that opens two nodes both present in the graph is not guessed onto one."""
    twin = Contact(name="Twin", public_key="27e41e2d7cb5" + "0" * 52, key_prefix="27e41e2d7cb5")
    walks = [  # both 27d4… and 27e4… land in the graph, plus a bare, undecidable 27
        _traced(("27d4", 5.0), (None, 5.0)),
        _traced(("27e4", 5.0), (None, 5.0)),
        _traced(("27", 5.0), (None, 5.0)),
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    assert {"27", "27d4396a2967", "27e41e2d7cb5"} <= ids  # the stub stays its own node


def test_coalesce_folds_an_ambiguous_stub_its_neighbourhood_elects() -> None:
    """A stub the hash can't place is placed by the company it keeps.

    ``3d`` opens both ``3d63…`` and ``3d99…``, so prefix alone is a coin toss — but the
    stub carries links to Far and Leaf, and only ``3d63…`` is seen with either. Two
    discriminating votes, none against, so the stub folds and pools its evidence.
    """
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60)
    walks = [
        _traced(("3d63", 5.0), ("f2", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d63", 5.0), ("27d4", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d99", 5.0), (None, 5.0)),  # the rival is in the graph, just not out here
        _traced(("3d", 5.0), ("f2", 5.0), ("3d", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("27d4", 5.0), ("3d", 5.0), (None, 5.0)),
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    assert "3d" not in ids  # elected onto the node its neighbours agree it must be
    assert {"3d63c6429436", "3d9900000000"} <= ids  # the rival is untouched
    link = topo.link(topo.self_id, "3d63c6429436")
    assert link is not None and link.samples == 8  # the stub's readings came with it


def test_coalesce_leaves_a_contested_stub_standing() -> None:
    """Neighbours pointing both ways mean the hash is pooling two nodes — leave it alone."""
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60)
    edge = Contact(name="Edge", public_key="b1" + "0" * 62)
    walks = [
        _traced(("3d63", 5.0), ("f2", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d63", 5.0), ("27d4", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d99", 5.0), ("b1", 5.0), ("3d99", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("f2", 5.0), ("3d", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("27d4", 5.0), ("3d", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("b1", 5.0), ("3d", 5.0), (None, 5.0)),
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin, edge])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    # 2 votes to 1 is a plurality, not a verdict: the margin keeps the stub its own node.
    assert {"3d", "3d63c6429436", "3d9900000000"} <= ids


def test_coalesce_ignores_neighbours_both_candidates_share() -> None:
    """Shared company is local density, not evidence — only discriminating neighbours vote."""
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60)
    walks = [
        _traced(("3d63", 5.0), ("f2", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d99", 5.0), ("f2", 5.0), ("3d99", 5.0), (None, 5.0)),
        _traced(("3d63", 5.0), ("27d4", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d99", 5.0), ("27d4", 5.0), ("3d99", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("f2", 5.0), ("3d", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("27d4", 5.0), ("3d", 5.0), (None, 5.0)),
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    # Every neighbour of the stub is a neighbour of *both* candidates, so nobody can tell
    # them apart and the stub is not guessed onto the busier one.
    assert {"3d", "3d63c6429436", "3d9900000000"} <= ids


def test_contact_routes_and_packet_paths_feed_the_graph() -> None:
    """Firmware-learned routes and RX-logged packet paths both count as evidence."""
    routed = Contact(
        name="Far", public_key=FAR.public_key, key_prefix=FAR.key_prefix,
        route_hops=("3d63c6429436",), last_seen=utcnow(),
    )
    packet = PacketPath(when=utcnow(), origin="27d4396a2967", snr=8.0, hops=["3d63c6429436"])
    topo = _topo(packet_paths=[packet], contacts=[REPEATER, routed, LEAF])
    assert topo.link(topo.self_id, "3d63c6429436") is not None  # both sources touch it
    route_link = topo.link("3d63c6429436", "f2c24f54551e")
    assert route_link is not None and route_link.sources == {"route"}
    packet_link = topo.link("27d4396a2967", "3d63c6429436")
    assert packet_link is not None and packet_link.sources == {"packet"}
    # Only the final link (last relay → us) carries the reception SNR.
    assert topo.link(topo.self_id, "3d63c6429436").snrs == [8.0]
    assert packet_link.snrs == []


def test_neighbour_reports_feed_the_graph_as_fetched_evidence() -> None:
    """A repeater's fetched table adds links at its vantage point, tagged ``neighbour``."""
    reported = [
        NeighbourLink(when=utcnow(), repeater="3d63c6429436", neighbour="f2c2", snr=7.0),
        NeighbourLink(when=utcnow(), repeater="3d63c6429436", neighbour="beefbeef", snr=None),
    ]
    topo = _topo(neighbour_links=reported)
    link = topo.link("3d63c6429436", "f2c24f54551e")  # 'f2c2' canonicalizes onto Far
    assert link is not None and link.sources == {"neighbour"}
    assert link.snrs == [7.0]  # the SNR measured at the repeater rides the link
    # A neighbour no contact matches still counts — discovery of an unseen node.
    unknown = topo.link("3d63c6429436", "beefbeef")
    assert unknown is not None and unknown.snrs == []
    suggested = [s.node for s in topo.next_hops("3d63c6429436")]
    assert "f2c24f54551e" in suggested and "beefbeef" in suggested


# --- suggestions and scenarios ----------------------------------------------------


def test_next_hops_sorts_by_link_strength_and_excludes_used_nodes() -> None:
    """Suggestions rank the heavily-observed fresh link first and honour exclusions."""
    strong = [_traced(("3d", 12.0), (None, 12.0)) for _ in range(6)]
    weak = [_traced(("27d4", -9.0), (None, -9.0), age_hours=24 * 21)]
    topo = _topo(trace_paths=strong + weak)
    nodes = [s.node for s in topo.next_hops(topo.self_id)]
    assert nodes == ["3d63c6429436", "27d4396a2967"]
    excluded = topo.next_hops(topo.self_id, exclude=frozenset({"3d63c6429436"}))
    assert [s.node for s in excluded] == ["27d4396a2967"]


def test_scenarios_rank_observed_route_and_pin_device_route_first() -> None:
    """The device route leads, then observed evidence, then the unobserved direct shot."""
    walks = [
        _traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0)) for _ in range(4)
    ]
    topo = _topo(trace_paths=walks)
    scenarios = topo.scenarios("f2c24f54551e", device_route=("3d63c6429436",))
    assert scenarios[0].source == "device"
    assert scenarios[0].hops == ("3d63c6429436",)
    labels = [s.source for s in scenarios]
    assert "direct" in labels  # the baseline is always offered
    direct = next(s for s in scenarios if s.source == "direct")
    assert direct.score == 0.0  # never observed → ranked on measurement only
    # The observed route (same hops as the device route) deduplicates away.
    assert sum(1 for s in scenarios if s.hops == ("3d63c6429436",)) == 1


def test_suggested_returns_the_strongest_observed_route_only() -> None:
    """suggested() is the data's answer: the best observed multi-hop route to the target."""
    walks = [
        _traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0)) for _ in range(4)
    ]
    topo = _topo(trace_paths=walks)
    best = topo.suggested("f2c24f54551e")
    assert best is not None
    assert best.source == "observed"  # never the device/direct families
    assert best.hops == ("3d63c6429436",)  # us → Hub → Far
    assert best.score > 0 and best.samples >= 4  # evidence-backed


def test_suggested_is_none_without_an_observed_repeater_route() -> None:
    """A target ever only reached directly (or not at all) has no route to suggest."""
    # One direct trace to the Hub — us → Hub → us, no intermediate repeater.
    topo = _topo(trace_paths=[_traced(("3d", 9.0), (None, 9.0))])
    assert topo.suggested("3d63c6429436") is None  # direct-only: nothing to suggest
    assert topo.suggested("f2c24f54551e") is None  # never reached at all


def test_scenarios_return_disjoint_alternatives_cheapest_first() -> None:
    """Two independent routes to a target both surface, ranked by evidence strength."""
    # Two repeaters, each a separate one-hop route to Far. Hub's link is stronger.
    walks = [
        _traced(("3d", 12.0), ("f2", -3.0), ("3d", -3.0), (None, 12.0)) for _ in range(3)
    ] + [
        _traced(("27", 4.0), ("f2", -8.0), ("27", -8.0), (None, 4.0)) for _ in range(3)
    ]
    topo = _topo(trace_paths=walks)
    observed = [
        s for s in topo.scenarios("f2c24f54551e") if s.source == "observed" and s.hops
    ]
    assert [s.hops for s in observed[:2]] == [("3d63c6429436",), ("27d4396a2967",)]
    assert observed[0].score > observed[1].score  # the stronger route ranks first


def test_best_routes_stays_fast_on_a_densely_connected_core() -> None:
    """A well-heard core must not stall route-finding — the pathology this search avoids.

    A best-first walk over *partial* paths floods a dense core: every wandering prefix
    through it is cheaper than the one weak link a distant target sits behind, so the
    frontier explodes to millions of dead-end prefixes (tens of seconds for a busy node).
    Yen's k-shortest search stays polynomial. Here a 20-node clique with the target hung
    off a single weak link resolves in milliseconds (the old frontier took seconds), and
    still finds the route out.
    """
    import time

    now = utcnow()
    core = [f"c0de{i:08x}" for i in range(20)]  # 20 distinct 12-hex core nodes
    target = "fa11faceface"
    links = []
    us = US + "0" * 52
    # Everyone in the core hears everyone (a full clique), and hears us — a maximally
    # dense frontier for the search to get lost in.
    for i, a in enumerate([us, *core]):
        for b in [*core][i:]:
            if a != b:
                links.append(NeighbourLink(when=now, repeater=a, neighbour=b, snr=8.0))
    # The target is reachable only across one weak link off the last core node.
    links.append(NeighbourLink(when=now, repeater=core[-1], neighbour=target, snr=-12.0))

    topo = _topo(neighbour_links=links)
    started = time.perf_counter()
    scenarios = topo.scenarios(target)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0  # milliseconds in practice; the old frontier took seconds+
    observed = [s for s in scenarios if s.source == "observed" and s.hops]
    assert observed  # the route through the core to the target was found
    assert observed[0].hops[-1] == core[-1]  # it arrives via the one weak link


def test_scenario_spec_ends_at_target_and_collapses_width() -> None:
    """Specs walk out to the target then mirror the hops back, at a uniform width."""
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    topo = _topo(trace_paths=walks)
    scenario = next(
        s for s in topo.scenarios("f2c24f54551e") if s.hops == ("3d63c6429436",)
    )
    assert scenario.spec("f2c24f54551e", 2) == "3d63,f2c2,3d63"
    assert collapse_width("3d", "f2c24f54551e", ceiling=2) == 1  # narrowest hash rules


def test_render_custom_spec_is_verbatim_at_a_uniform_width() -> None:
    """An asymmetric walk renders exactly its hops — no target append, no mirror."""
    hops = ("3d63c6429436", "f2c24f54551e", "27d4396a2967")
    assert render_custom_spec(hops, 2) == "3d63,f2c2,27d4"
    # A hop only known narrowly drags the whole spec down to what it can honour.
    assert render_custom_spec(("3d63c6429436", "be"), 2) == "3d,be"
    assert render_custom_spec((), 2) == ""  # no hops, no spec (never a fake "auto")


# --- repository round-trip ---------------------------------------------------------


def test_repository_round_trips_packet_paths_and_candidates(tmp_path) -> None:  # noqa: ANN001
    """Packet observations persist their path; candidates land in path_candidates."""
    repo = Repository(tmp_path / "t.db")
    run = repo.start_run("monitor", {})
    repo.record_observation(
        run, Observation(node="27d4396a2967", kind="packet", snr=7.5, path="3d63,f2c2")
    )
    repo.record_observation(run, Observation(node="27d4396a2967", kind="advert", snr=3.0))
    packets = repo.packet_paths()
    assert len(packets) == 1
    assert packets[0].hops == ["3d63", "f2c2"]
    assert packets[0].origin == "27d4396a2967"
    # Packet rows never pollute per-node reception statistics.
    heard = repo.heard_nodes()
    assert len(heard) == 1 and heard[0].count == 1 and heard[0].median_snr == 3.0

    probe_run = repo.start_run("trace", {"mode": "probe"})
    repo.record_path_candidate(
        probe_run, "Far", "3d,f2", bottleneck_snr=-5.0, success_rate=1.0, median_rtt_ms=320.0
    )
    row = repo._conn.execute("SELECT * FROM path_candidates").fetchone()
    assert row["target"] == "Far" and row["success_rate"] == 1.0
    repo.close()


def test_repository_neighbour_snapshots_keep_latest_per_pair(tmp_path) -> None:  # noqa: ANN001
    """A refetched table supersedes the old snapshot instead of stacking as evidence."""
    repo = Repository(tmp_path / "n.db")
    run = repo.start_run("trace", {"mode": "neighbours"})
    heard = utcnow() - timedelta(hours=2)
    repo.record_neighbours(
        run,
        "3d63c6429436",
        [
            NeighbourInfo(node="f2c24f54", snr=-5.0, heard_at=heard),
            NeighbourInfo(node="beefbeef", snr=2.0, heard_at=None),
        ],
    )
    links = {link.neighbour: link for link in repo.neighbour_links()}
    assert len(links) == 2
    assert links["f2c24f54"].snr == -5.0
    assert links["f2c24f54"].when == heard  # the repeater's own recency survives
    assert links["beefbeef"].when is not None  # no heard_at → falls back to fetched_at

    repo.record_neighbours(run, "3d63c6429436", [NeighbourInfo(node="f2c24f54", snr=9.0)])
    links = {link.neighbour: link for link in repo.neighbour_links()}
    assert len(links) == 2  # still one link per pair
    assert links["f2c24f54"].snr == 9.0  # the fresh snapshot won
    repo.close()


async def test_mock_fetch_neighbours_is_login_gated_like_real_firmware() -> None:
    """No login → ignored (error); logged in → the table, including an unseen node."""
    device = MockDevice()
    await device.connect()
    contacts = await device.get_contacts()
    yagi = next(c for c in contacts if c.name == "Yagi-Repeater")

    with pytest.raises(DeviceCommandError):
        await device.fetch_neighbours(yagi)  # mirrors v1.15 firmware: guests are ignored

    assert await device.admin_login(yagi, "admin")
    entries = await device.fetch_neighbours(yagi)
    nodes = {e.node for e in entries}
    assert "e5f6a7b8" in nodes  # a node absent from the contact list: pure discovery
    assert all(e.heard_at is not None for e in entries)

    alice = next(c for c in contacts if c.name == "Alice")
    assert await device.admin_login(alice, "admin")
    with pytest.raises(DeviceCommandError):
        await device.fetch_neighbours(alice)  # chat nodes keep no neighbour table


# --- path probe ----------------------------------------------------------------------


async def test_probe_paths_ranks_reliability_first() -> None:
    """A path that always answers outranks a stronger-SNR one that drops traces."""
    from meshterm.core.models import Hop, TraceResult

    class _Device:
        async def run_trace(self, target, *, path=None, timeout=10.0):  # noqa: ANN001
            if path == "good":
                return TraceResult(
                    target=target, success=True,
                    hops=[Hop(index=0, node="3d", snr=2.0)], round_trip_ms=300.0,
                )
            return TraceResult(target=target, success=False)

    outcomes = await probe_paths(
        _Device(), "Far",
        [ProbeCandidate(label="flaky", spec="bad"), ProbeCandidate(label="solid", spec="good")],
        samples=2, cooldown_s=0.0,
    )
    assert [o.candidate.label for o in outcomes] == ["solid", "flaky"]
    assert outcomes[0].stats.success_rate == 1.0


async def test_probe_paths_defaults_to_one_trace_per_candidate() -> None:
    """By default each candidate is measured with a single transmission.

    This is the blacklist-avoidance rule: repeaters penalize nodes that burst traffic,
    so probing N paths must cost exactly N traces unless the caller opts into more.
    """
    from meshterm.core.models import Hop, TraceResult

    transmitted: list[str] = []

    class _Device:
        async def run_trace(self, target, *, path=None, timeout=10.0):  # noqa: ANN001
            transmitted.append(path)
            return TraceResult(
                target=target, success=True,
                hops=[Hop(index=0, node="3d", snr=2.0)], round_trip_ms=300.0,
            )

    outcomes = await probe_paths(
        _Device(), "Far",
        [ProbeCandidate(label="a", spec="3d"), ProbeCandidate(label="b", spec="f2")],
        cooldown_s=0.0,
    )
    assert transmitted == ["3d", "f2"]  # one trace per candidate, in order
    assert all(o.stats.samples == 1 for o in outcomes)


# --- path composer ---------------------------------------------------------------------


def _composer(  # noqa: ANN001
    topo, hops=None, fetch_nodes=frozenset(), target=True, cursor=None, resolve=None
):
    """A composer over ``topo`` — pinned on Far (target mode) or target-less."""
    pinned = (
        dict(
            target_id="f2c24f54551e",
            target_hash="f2c24f54551e" + "0" * 52,
            target_label="Far",
        )
        if target
        else {}
    )
    screen = PathComposerScreen(
        device_label="Us",
        device_hash=US + "0" * 52,
        topology=topo,
        width_bytes=1,
        hops=list(hops or []),
        cursor=cursor,
        fetch_nodes=fetch_nodes,
        **({"resolve": resolve} if resolve is not None else {}),
        **pinned,
    )
    screen.note_viewport(30)  # the frame records the dialog budget before each paint
    return screen


def _rows_plain(screen: PathComposerScreen) -> str:
    return "\n".join(screen.render_body(90))


def test_composer_suggests_from_tail_and_appends_on_enter() -> None:
    """The strongest neighbour of the path's tail is the first suggestion; Enter adds it."""
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    screen = _composer(_topo(trace_paths=walks))
    body = _rows_plain(screen)
    assert "Hub" in body and "auto" in body.lower()  # suggestion + the "Auto" action row
    screen.handle("enter")  # top row: the Hub suggestion
    assert screen._hops == ["3d63c6429436"]
    # From the new tail the target is excluded, so Far never appears as a hop.
    assert all(s.node != "f2c24f54551e" for s in screen._suggestions())


def test_composer_windows_rows_under_the_pinned_route_preview() -> None:
    """A short dialog windows the rows; the route preview and cursor stay visible."""
    import re

    walks = [
        _traced((f"{i + 16:02x}", 3.0), (None, 3.0)) for i in range(10)
    ]
    screen = _composer(_topo(trace_paths=walks, contacts=[FAR]))
    screen.note_viewport(9)  # a short dialog budget: preview + heading + a few rows
    body = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(90)))
    assert SELF_GLYPH in body  # the route preview is pinned, never scrolled out
    assert "↓" in body and "more" in body  # hidden rows are counted at the edge
    screen.handle("end")  # cursor to the last action row — the window follows
    body = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(90)))
    assert "Auto" in body and "↑" in body


def test_composer_typed_hex_adds_a_custom_hop_and_backspace_removes() -> None:
    """Even-length hex typed into the filter becomes an addable custom hop."""
    screen = _composer(_topo())
    for ch in "beef":
        screen.handle("text", ch)
    screen.handle("enter")  # the "+ add hop beef" row is first
    assert screen._hops == ["beef"]
    screen.handle("backspace")  # entry is empty → removes the last hop
    assert screen._hops == []


def test_composer_cursor_inserts_and_deletes_mid_path() -> None:
    """←/→ walk the insertion cursor; adds splice in at it and ⌫ removes to its left."""
    screen = _composer(
        _topo(), hops=["27d4396a2967", "f2c24f54551e"], target=False
    )
    assert screen.cursor == 2  # opens on the last arrow: inserting is appending
    screen.handle("right")
    assert screen.cursor == 2  # clamped at the end…
    screen.handle("left")
    screen.handle("left")
    screen.handle("left")
    assert screen.cursor == 0  # …and at home
    for ch in "beef":  # insert at home: the hop lands before the seeded ones
        screen.handle("text", ch)
    screen.handle("enter")
    assert screen._hops == ["beef", "27d4396a2967", "f2c24f54551e"]
    assert screen.cursor == 1  # the cursor rode past what it inserted
    screen.handle("backspace")  # ⌫ takes the hop left of the cursor and follows it
    assert screen._hops == ["27d4396a2967", "f2c24f54551e"] and screen.cursor == 0
    # A reopen (the fetch-neighbours round trip) resumes at the seeded position.
    assert _composer(_topo(), hops=["27d4396a2967"], cursor=1).cursor == 1
    assert _composer(_topo(), hops=["27d4396a2967"], cursor=99).cursor == 1  # clamped


def test_composer_suggestions_follow_the_cursor_anchor() -> None:
    """The heading and list re-seat on the node left of the cursor as it moves."""
    import re

    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    screen = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"], target=False)

    def body() -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(90)))

    assert "Next hop from Hub" in body()  # cursor at the end: the anchor is the tail
    screen.handle("left")
    assert "Next hop from Us" in body()
    # Inserting the cursor's right neighbour would self-loop — Hub is not proposed.
    assert all(s.node != "3d63c6429436" for s in screen._suggestions())


def test_composer_preview_stands_the_cursor_in_the_route() -> None:
    """The composer preview stands the cursor in the route itself.

    The insertion point is a hop — the ``+`` slot the next node takes — and ←/→ slide
    it along, one position at a time.
    """
    screen = _composer(_topo(), hops=["3d63c6429436"], target=False)

    def slot_at() -> int:
        labels = [hop.label for hop in screen._route_preview().hops]
        assert labels.count(CURSOR_GLYPH) == 1  # exactly one slot, always
        return labels.index(CURSOR_GLYPH)

    assert slot_at() == 2  # opens after us and the composed hop: inserting is appending
    screen.handle("left")
    assert slot_at() == 1  # slid home, before the hop it would now precede


def test_composer_warns_when_the_walk_repeats_a_link() -> None:
    """Riding a link twice the same way shows the yellow not-a-trail note; fixing it clears."""
    clean = _composer(_topo(), hops=["3d63c6429436", "f2c24f54551e"], target=False)
    assert "not a trail" not in _rows_plain(clean)
    screen = _composer(
        _topo(),
        hops=["3d63c6429436", "f2c24f54551e"] * 2,  # …→ 3d → f2 ridden again
        target=False,
    )
    body = _rows_plain(screen)
    assert "⚠" in body and "not a trail" in body and "records ignore" in body
    screen.handle("backspace")  # drop the second f2: the repeat is gone
    assert "not a trail" not in _rows_plain(screen)


def test_composer_dialog_only_ever_grows() -> None:
    """The box ratchets in both dimensions, so it never twitches as the path changes."""
    screen = _composer(_topo(), hops=[], target=False)
    assert screen.grow_only is True
    # ratchet_viewport / ratchet_width are the grow-only contract: rise, never drop.
    assert screen.ratchet_viewport(6) == 6
    assert screen.ratchet_viewport(14) == 14  # a longer suggestion list enlarges the box
    assert screen.ratchet_viewport(4) == 14   # a shorter one after keeps the taller box
    assert screen.ratchet_width(40) == 40
    assert screen.ratchet_width(64) == 64     # a longer route preview widens the box
    assert screen.ratchet_width(40) == 64     # narrower content after keeps the wider box


def test_composer_target_mode_warning_covers_the_mirrored_return() -> None:
    """The check runs over the whole boomerang — a repeated outbound stretch fires it."""
    clean = _composer(_topo(), hops=["3d63c6429436"])
    assert "not a trail" not in _rows_plain(clean)  # the plain boomerang is a trail
    screen = _composer(
        _topo(),
        hops=["3d63c6429436", "27d4396a2967", "3d63c6429436", "27d4396a2967"],
    )
    assert "not a trail" in _rows_plain(screen)


async def test_composer_fetch_row_resolves_a_fetch_request() -> None:
    """Standing on a fetchable repeater, Enter on the fetch row hands off to the owner."""
    import asyncio

    screen = _composer(
        _topo(), hops=["3d63c6429436"], fetch_nodes=frozenset({"3d63c6429436"})
    )
    screen.future = asyncio.get_running_loop().create_future()
    body = _rows_plain(screen)
    assert "Fetch neighbours from" in body and "Hub" in body
    screen.handle("enter")  # an empty graph offers no suggestions: fetch is the top row
    result = screen.future.result()
    assert isinstance(result, FetchNeighbours) and result.node == "3d63c6429436"
    assert screen.hops == ["3d63c6429436"]  # preserved, so the owner can reopen mid-path


def test_composer_hides_fetch_row_off_fetchable_tails() -> None:
    """The fetch row tracks the path's tail: at our own node there is nothing to ask."""
    screen = _composer(_topo(), fetch_nodes=frozenset({"3d63c6429436"}))
    assert "Fetch neighbours" not in _rows_plain(screen)  # tail is us, not the repeater


def test_composer_use_row_names_the_action_not_the_path() -> None:
    """The commit row doesn't respell the route — the preview two rows up *is* the route.

    Repeating it as raw hex (JP, 2026-08-10) said the same thing worse, and grew the row by
    a hop every time the path did. Only the unarmed case still needs words: a row that
    cannot commit has to say why.
    """
    from tests.conftest import plain

    walks = [_traced(("3d", 12.0), (None, 12.0))]

    def use_row(screen: PathComposerScreen) -> str:
        body = plain(screen.render_body(90))
        row = next(ln for ln in body.splitlines() if "Use this path" in ln)
        return row.replace("❯", "").strip()

    armed = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"])
    assert use_row(armed) == "✓ Use this path"
    assert armed._spec() == "3d,f2,3d"  # …the spec is still what Enter commits

    # An unarmed row still has to say why it can't commit.
    empty = _composer(_topo(trace_paths=walks), hops=[])
    empty._spec = lambda: ""  # type: ignore[method-assign]
    assert use_row(empty) == "✓ Use this path  (add a hop first)"


async def test_composer_commits_spec_auto_and_cancel() -> None:
    """Use resolves the spec with its mirrored return leg; Auto resolves empty; Esc cancels.

    Leaving is Esc's alone — the action group ends at Auto, so End lands on the last row
    that *does* something rather than on one that only pressed Esc on your behalf.
    """
    import asyncio

    walks = [_traced(("3d", 12.0), (None, 12.0))]

    screen = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"])
    screen.future = asyncio.get_running_loop().create_future()
    screen.handle("end")  # jump to the last action row (Auto)…
    screen.handle("up")  # …up to "Use this path"
    screen.handle("enter")
    assert screen.future.result() == "3d,f2,3d"

    auto = _composer(_topo(trace_paths=walks))
    auto.future = asyncio.get_running_loop().create_future()
    auto.handle("end")  # "Auto — let the device route" now closes the group
    auto.handle("enter")
    assert auto.future.result() == AUTO_SPEC

    cancelled = _composer(_topo(trace_paths=walks))
    cancelled.future = asyncio.get_running_loop().create_future()
    cancelled.handle("escape")
    assert cancelled.future.result() is CANCEL


def test_composer_path_mode_opens_star_to_star_without_auto(monkeypatch) -> None:  # noqa: ANN001
    """Target-less, the preview is just ``★ → + → ★`` and there is no Auto action.

    Both ends are our own node, bare and faded: a walk always leaves us and comes home
    to us, and neither end is the user's to compose or remove — the automatic grey says
    so. Between them waits the empty slot. With no destination there is nothing for the
    device to route to either.
    """
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: False)  # assert the words
    screen = _composer(_topo(), target=False)
    assert screen.title == "Compose path"  # no target to name
    preview = screen._route_preview().text()
    # Our name and hash go unsaid; the slot says where the first hop would land.
    assert preview.plain == f"{SELF_GLYPH} → {CURSOR_GLYPH} → {SELF_GLYPH}"
    stars = [
        str(span.style) for span in preview.spans
        if preview.plain[span.start : span.end] == SELF_GLYPH
    ]
    assert stars == ["faint", "faint"]  # read-only, like every auto-managed chip
    assert "Auto" not in _rows_plain(screen)


def test_composer_names_an_ambiguous_hop_through_the_owning_screens_resolver() -> None:
    """A short hop the topology won't name still reads as a name, with its hash beside it.

    A 1-byte hop that prefix-matches two contacts is ambiguous, so the topology refuses
    to guess and keeps it as the bare hash — which left the suggestion list proposing
    opaque hex for nodes the trace window behind it was happily naming. The owning
    screen's resolver is the fallback, so both surfaces say the same thing.
    """
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60)  # makes a bare "3d" ambiguous
    topo = _topo(trace_paths=[_traced(("3d", 12.0), (None, 12.0))],
                 contacts=[REPEATER, twin])
    assert topo.display_name("3d") is None  # two contacts match: the graph won't pick

    import re

    blind = _composer(topo, target=False)
    assert "Hub" not in _rows_plain(blind)  # without a fallback: bare hex, as before

    named = _composer(topo, target=False, resolve=lambda h: "Hub" if h == "3d" else h)
    body = re.sub(r"\x1b\[[0-9;]*m", "", _rows_plain(named))
    assert "Hub (3d)" in body  # the name, and the hash it is addressed by beside it


def test_composer_merges_a_stub_and_its_full_id_into_one_suggestion() -> None:
    """One node the graph holds twice reads — and is addressed — as one row.

    ``3d`` prefix-matches two contacts, so the topology keeps it as its own vertex
    rather than guessing, separate from the ``3d63c6429436`` its wider sightings landed
    on. Once both are named, the list would propose the same node twice; they fold onto
    the longer id, evidence pooled. The 3d-prefixed *twin* is a different name, so it
    stays its own row — a shared prefix alone never merges two nodes.
    """
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60, key_prefix="3d9900000000")
    walks = [
        _traced(("3d", 6.0), (None, 6.0)),            # the ambiguous stub
        _traced(("3d63c6429436", 8.0), (None, 8.0)),  # the very same node, full id
        _traced(("3d9900000000", 4.0), (None, 4.0)),  # a different node behind 3d
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, twin])
    assert topo.display_name("3d") is None  # ambiguous: the graph won't fold it itself
    assert {s.node for s in topo.next_hops(topo.self_id)} == {
        "3d", "3d63c6429436", "3d9900000000",
    }

    screen = _composer(topo, target=False, resolve=make_node_resolver([REPEATER, twin]))
    rows = screen._suggestions()
    assert [r.node for r in rows] == ["3d63c6429436", "3d9900000000"]
    assert rows[0].link.samples == 4  # both walks' out-and-back readings, pooled
    assert rows[0].link.median_snr == 7.0  # 6.0 and 8.0 twice each


def test_composer_steps_off_a_merged_node_with_all_its_evidence() -> None:
    """Standing on the merged node sees everything *either* of its ids was heard by.

    Folding the stub away must not fold its links away with it — otherwise merging
    would quietly cost the composer whatever was only ever observed under the stub.
    """
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60, key_prefix="3d9900000000")
    walks = [
        _traced(("3d", 6.0), ("f2", -5.0), ("3d", -5.5), (None, 6.0)),  # Far, via the stub
        _traced(("3d63c6429436", 8.0), (None, 8.0)),                    # the full id
        _traced(("3d9900000000", 4.0), (None, 4.0)),                    # the other 3d node
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, twin])
    screen = _composer(topo, target=False, hops=["3d63c6429436"], resolve=make_node_resolver(
        [REPEATER, FAR, twin]
    ))
    # Far was only ever heard through the stub, and is still offered from the full id.
    assert [s.node for s in screen._suggestions()] == ["f2c24f54551e"]


async def test_composer_path_mode_suggests_through_anything_and_commits_verbatim() -> None:
    """A path walk routes *through* any node and commits exactly the composed hops."""
    import asyncio

    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    screen = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"], target=False)
    # No pinned target: the tail (Hub) hears Far, so Far is a plain hop suggestion.
    assert any(s.node == "f2c24f54551e" for s in screen._suggestions())
    screen.handle("enter")  # the sole suggestion: Far joins the walk
    # A return leg may legitimately reuse an outbound repeater (only the tail is barred).
    assert any(s.node == "3d63c6429436" for s in screen._suggestions())
    # Out via Hub, home directly off Far: nothing is appended or mirrored.
    assert screen._spec() == "3d,f2"

    loop = asyncio.get_running_loop()
    screen.future = loop.create_future()
    screen.handle("end")  # "Use this path" — path mode ends the group there, no Auto row
    screen.handle("enter")
    assert screen.future.result() == "3d,f2"

    # With no hops at all there is nothing to commit: Use must stay inert, because
    # resolving "" would masquerade as Auto (device-routed).
    empty = _composer(_topo(trace_paths=walks), target=False)
    empty.future = loop.create_future()
    assert "(add a hop first)" in _rows_plain(empty)
    empty.handle("end")
    empty.handle("up")
    empty.handle("enter")
    assert not empty.future.done()


# --- the prefix settle is a sorted-range scan, not a sweep of the graph --------------


def test_extensions_matches_a_naive_scan_over_awkward_shapes() -> None:
    """The binary-searched range must return exactly what scanning every node would.

    ``_extensions`` answers "which ids strictly extend this short one" by binary-searching
    a lexicographically sorted list, because the caller asks it once per node and its own
    caller restarts the whole pass after every merge — on a real mesh that was ~24 million
    ``len`` calls and sixteen seconds. The shapes below are the ones a hex-prefix graph
    actually produces: chains, shared stems, a full-width id, and near-misses either side
    of the run that a sloppy range would swallow.
    """
    from meshterm.services.topology import MeshTopology

    nodes = {
        "6", "65", "6532", "6532eb", "6532eb00aa11",
        "6533", "653300ff", "66", "6600",
        "a1", "a1b2", "a1b3", "b0",
        "ffffffffffff",
    }
    ordered = sorted(nodes)
    for short in sorted(nodes):
        naive = sorted(
            other
            for other in nodes
            if other != short and len(other) > len(short) and other.startswith(short)
        )
        assert sorted(MeshTopology._extensions(short, ordered)) == naive, short


def test_extensions_ignores_a_full_width_id() -> None:
    """Twelve hex is canonical: it is nobody's prefix, however the graph sorts."""
    from meshterm.services.topology import MeshTopology

    ordered = sorted({"6532eb00aa11", "6532eb00aa1122", "65"})
    assert MeshTopology._extensions("6532eb00aa11", ordered) == []


def test_prefix_settle_folds_a_chain_but_not_a_fork() -> None:
    """The end-to-end behaviour the fast lookup has to preserve.

    ``65`` -> ``6532`` -> ``6532eb...`` is one node named three ways, so the graph ends up
    holding it once. Add a sibling that genuinely forks the stem and ``65`` stops being an
    abbreviation and starts being a coin toss between two real nodes — it must then stay
    exactly as it is, under-named but honest, rather than be folded onto either.
    """
    from meshterm.core.models import utcnow
    from meshterm.services.topology import MeshTopology

    def graph(hops):
        topo = MeshTopology(self_id="local", contacts=[])
        now = utcnow()
        for hop in hops:
            topo.add_walk(["local", hop], when=now, source="trace")
        topo.coalesce_prefixes()
        return {end for link in topo._links for end in link}

    chain = graph(("65", "6532", "6532eb00aa11"))
    assert chain == {"local", "6532eb00aa11"}, f"chain not folded: {sorted(chain)}"

    fork = graph(("65", "6532", "6532eb00aa11", "653300ff2211"))
    assert "6532eb00aa11" in fork and "653300ff2211" in fork
    assert "6532" not in fork, "an unambiguous link in the chain should still fold"
    assert "65" in fork, "a stub opening two real nodes must not be guessed onto one"


def test_prefix_settle_is_repeatable_whatever_order_the_nodes_arrived_in() -> None:
    """The same evidence must always settle to the same graph.

    The passes offer the under-specified ids shortest first, and for a while the ids of
    equal width came out in whatever order the node *set* happened to iterate. CPython
    randomizes string hashing per process, so two runs over identical evidence could take
    their merges in different orders — and where one fold changes what a later one can see,
    end up with different graphs. Feeding the same walks in reversed order reproduces that
    (a set built by a different insertion sequence iterates differently); the settled
    endpoints must not care.
    """
    from meshterm.core.models import utcnow
    from meshterm.services.topology import MeshTopology

    walks = [
        ["local", "a1", "b2c3"], ["local", "a1b2", "b2c3d4"],
        ["local", "a1b2c3d4e5f6"], ["local", "b2c3d4e5f601"],
        ["local", "a1b2c3", "c4"], ["local", "c4d5e6f70011"],
        ["local", "b2", "a1b2c3d4e5f6"],
    ]

    def settle(order):
        topo = MeshTopology(self_id="local", contacts=[])
        now = utcnow()
        for walk in order:
            topo.add_walk(list(walk), when=now, source="trace")
        topo.coalesce_prefixes()
        return sorted(topo._links)

    assert settle(walks) == settle(list(reversed(walks)))


def test_a_merge_leaves_the_neighbour_table_exactly_as_a_fresh_one() -> None:
    """The settle carries one table across every merge instead of rebuilding it.

    That is only sound while the incremental update is *exact* — a stale neighbour or a
    node left behind after its last link folded away would feed the corroboration vote bad
    evidence, or offer a merge candidate that no longer exists. So after each merge the
    carried table has to equal one derived from the reshaped graph.
    """
    from meshterm.core.models import utcnow
    from meshterm.services.topology import MeshTopology

    topo = MeshTopology(self_id="local", contacts=[])
    now = utcnow()
    for walk in (
        ["local", "a1", "b2c3d4e5f601"], ["local", "a1b2c3d4e5f6"],
        ["a1", "c4d5e6f70011"], ["a1b2", "a1b2c3d4e5f6"],
        ["b2c3d4e5f601", "c4d5e6f70011"], ["local", "d7"], ["d7e8f9001122", "local"],
    ):
        topo.add_walk(list(walk), when=now, source="trace")

    adjacency = topo._adjacency()
    merges = 0
    while True:
        shorts = sorted(
            (n for n in adjacency if len(n) < 12), key=lambda n: (len(n), n)
        )
        ordered = sorted(adjacency)
        merge = topo._next_prefix_merge(shorts, ordered) or topo._next_corroborated_merge(
            shorts, ordered, adjacency
        )
        if merge is None:
            break
        topo._merge_node(*merge, adjacency=adjacency)
        merges += 1
        assert adjacency == topo._adjacency(), f"table drifted after merging {merge}"
    assert merges, "the fixture should exercise at least one merge"


def test_a_stub_whose_only_link_was_its_owner_takes_the_owner_out_with_it() -> None:
    """The corner the incremental table has to get right: both ends can leave at once.

    ``a1`` is heard only in company with ``a1b2c3d4e5f6`` — one link, between the two names
    of one node. Folding the stub collapses that link to a self-loop and discards it, so the
    owner is left holding nothing and leaves the graph too. Neither may linger in the node
    set the next pass reads.
    """
    from meshterm.core.models import utcnow
    from meshterm.services.topology import MeshTopology

    topo = MeshTopology(self_id="local", contacts=[])
    topo.add_walk(["a1", "a1b2c3d4e5f6"], when=utcnow(), source="trace")
    adjacency = topo._adjacency()
    topo._merge_node("a1", "a1b2c3d4e5f6", adjacency=adjacency)
    assert topo._links == {}
    assert adjacency == {}


def test_composer_row_cursor_clamps_at_both_ends() -> None:
    """The windowed suggestion list does not wrap.

    ↑ on the first row and ↓ past the last stay where they are, rather than hauling
    the window end to end.
    """
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    screen = _composer(_topo(trace_paths=walks))
    screen.render_body(90)
    last = len(screen._rows()) - 1
    screen.handle("up")
    assert screen._index == 0
    for _ in range(last + 5):
        screen.handle("down")
    assert screen._index == last
