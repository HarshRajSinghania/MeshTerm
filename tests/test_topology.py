"""Unit tests for the observed-topology graph, path scenarios, and the path composer.

The graph is pure data-in/data-out, so these run headless: evidence rows are built by
hand (mirroring what the repository returns) and the assertions cover canonicalization,
bidirectional link folding, strength ordering, scenario ranking, and the composer's
step-by-step state machine.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from meshterm.core.connection import DeviceCommandError, MockDevice
from meshterm.core.models import Contact, NeighbourInfo, Observation, utcnow
from meshterm.persistence.repository import (
    NeighbourLink,
    PacketPath,
    Repository,
    TracedPath,
)
from meshterm.services.path_probe import ProbeCandidate, ProbeOutcome, probe_paths
from meshterm.services.topology import build_topology, collapse_width
from meshterm.ui.path_composer import AUTO_SPEC, FetchNeighbours, PathComposerScreen
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


def test_scenario_spec_ends_at_target_and_collapses_width() -> None:
    """Specs walk out to the target then mirror the hops back, at a uniform width."""
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    topo = _topo(trace_paths=walks)
    scenario = next(
        s for s in topo.scenarios("f2c24f54551e") if s.hops == ("3d63c6429436",)
    )
    assert scenario.spec("f2c24f54551e", 2) == "3d63,f2c2,3d63"
    assert collapse_width("3d", "f2c24f54551e", ceiling=2) == 1  # narrowest hash rules


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
    from meshterm.core.models import TraceResult, Hop

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


def _composer(topo, hops=None, fetch_nodes=frozenset()):  # noqa: ANN001
    return PathComposerScreen(
        target_id="f2c24f54551e",
        target_hash="f2c24f54551e" + "0" * 52,
        target_label="Far",
        device_label="Us",
        topology=topo,
        width_bytes=1,
        hops=list(hops or []),
        fetch_nodes=fetch_nodes,
    )


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


def test_composer_typed_hex_adds_a_custom_hop_and_backspace_removes() -> None:
    """Even-length hex typed into the filter becomes an addable custom hop."""
    screen = _composer(_topo())
    for ch in "beef":
        screen.handle("text", ch)
    screen.handle("enter")  # the "+ add hop beef" row is first
    assert screen._hops == ["beef"]
    screen.handle("backspace")  # entry is empty → removes the last hop
    assert screen._hops == []


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


async def test_composer_commits_spec_auto_and_cancel() -> None:
    """Use resolves the spec with its mirrored return leg; Auto resolves empty; Esc cancels."""
    import asyncio

    walks = [_traced(("3d", 12.0), (None, 12.0))]

    screen = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"])
    screen.future = asyncio.get_running_loop().create_future()
    screen.handle("end")  # jump to the last action row (Cancel)…
    screen.handle("up")
    screen.handle("up")  # …then back up to "Use this path"
    screen.handle("enter")
    assert screen.future.result() == "3d,f2,3d"

    auto = _composer(_topo(trace_paths=walks))
    auto.future = asyncio.get_running_loop().create_future()
    auto.handle("end")
    auto.handle("up")  # "Auto — let the device route"
    auto.handle("enter")
    assert auto.future.result() == AUTO_SPEC

    cancelled = _composer(_topo(trace_paths=walks))
    cancelled.future = asyncio.get_running_loop().create_future()
    cancelled.handle("escape")
    assert cancelled.future.result() is CANCEL
