"""Tests for the passive mesh monitor: listening, aggregation, and persistence.

All run against the :class:`MockDevice` simulator and an on-disk SQLite database, no
hardware required.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from meshtools.core.connection import MockDevice, observation_from_event
from meshtools.core.models import HeardNode, Observation, utcnow
from meshtools.persistence.repository import Repository


async def test_mock_listen_emits_observations_and_callbacks() -> None:
    """Listening yields observations and fires the per-observation callback."""
    device = MockDevice()
    await device.connect()

    seen: list[Observation] = []
    observations = await device.listen(0.2, on_observation=seen.append)

    assert observations  # the simulator advertises its known contacts
    assert seen == observations  # every observation was reported via the callback
    assert all(o.node for o in observations)
    # At least one node advertises a location (for the coverage map).
    assert any(o.lat is not None and o.lon is not None for o in observations)


def test_heard_node_aggregation() -> None:
    """from_observations summarizes count, robust SNR, latest RSSI, and location."""
    now = utcnow()
    obs = [
        Observation(node="a1", name="Yagi", snr=4.0, rssi=-100.0, observed_at=now),
        Observation(
            node="a1", name="Yagi", snr=8.0, rssi=-90.0, lat=45.5, lon=-73.5,
            observed_at=now + timedelta(seconds=5),
        ),
        Observation(
            node="a1", name="Yagi", snr=6.0, rssi=-95.0, observed_at=now + timedelta(seconds=2)
        ),
    ]
    node = HeardNode.from_observations("a1", obs)
    assert node.count == 3
    assert node.median_snr == 6.0  # median of [4, 8, 6]
    assert node.best_snr == 8.0
    assert node.last_seen == now + timedelta(seconds=5)  # freshest observation
    assert node.last_rssi == -90.0  # RSSI from that freshest observation
    assert node.has_location  # location carried by one observation is retained


def test_heard_node_latest_wins_for_rssi_and_location() -> None:
    """The freshest observation supplies RSSI; the freshest *located* one supplies coords."""
    now = utcnow()
    obs = [
        Observation(node="b2", snr=1.0, rssi=-80.0, lat=10.0, lon=20.0, observed_at=now),
        Observation(node="b2", snr=2.0, rssi=-70.0, observed_at=now + timedelta(seconds=10)),
    ]
    node = HeardNode.from_observations("b2", obs)
    assert node.last_rssi == -70.0  # latest reading
    assert (node.lat, node.lon) == (10.0, 20.0)  # latest one that actually had a fix


def test_observation_from_event_parses_advert() -> None:
    """The event parser pulls node id, name, SNR/RSSI, and location from a payload."""

    class _Event:
        payload = {
            "public_key": "AABBCC0011",
            "adv_name": "Repeater",
            "snr": "7.5",
            "rssi": -92,
            "adv_lat": 45.5,
            "adv_lon": -73.6,
        }

    obs = observation_from_event(_Event(), "advert")
    assert obs is not None
    assert obs.node == "aabbcc0011"
    assert obs.name == "Repeater"
    assert obs.snr == 7.5
    assert obs.rssi == -92.0
    assert (obs.lat, obs.lon) == (45.5, -73.6)


def test_observation_from_event_without_node_is_skipped() -> None:
    """A payload carrying no node identifier yields no observation."""

    class _Event:
        payload = {"snr": 3.0}

    assert observation_from_event(_Event(), "advert") is None


async def test_observations_round_trip_and_aggregate(tmp_path: Path) -> None:
    """Observations persist and heard_nodes reaggregates them across runs."""
    repo = Repository(tmp_path / "obs.db")
    run_id = repo.start_run("monitor", {"duration": 1})

    device = MockDevice()
    await device.connect()
    observations = await device.listen(
        0.2, on_observation=lambda o: repo.record_observation(run_id, o)
    )
    assert observations

    nodes = repo.heard_nodes()
    assert nodes
    # Every node aggregated should have been heard at least once.
    assert sum(n.count for n in nodes) == len(observations)
    # Nodes are ordered most-recently-heard first.
    assert nodes == sorted(nodes, key=lambda n: n.last_seen, reverse=True)
    repo.close()
