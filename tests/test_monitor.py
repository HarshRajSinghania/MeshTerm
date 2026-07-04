"""Tests for the passive mesh monitor: listening, aggregation, and persistence.

All run against the :class:`MockDevice` simulator and an on-disk SQLite database, no
hardware required.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from meshtools.core.connection import (
    _MOCK_MONITOR_INTERVAL_S as _MOCK_INTERVAL,
)
from meshtools.core.connection import (
    MockDevice,
    observation_from_event,
)
from meshtools.core.models import HeardNode, Observation, utcnow
from meshtools.core.monitor_store import MonitorStore
from meshtools.persistence.repository import Repository
from meshtools.services.monitor_service import MonitorService


class _StubContext:
    """Minimal stand-in for :class:`~meshtools.context.AppContext` for service tests."""

    def __init__(self, repo: Repository, device: MockDevice) -> None:
        self.repo = repo
        self._device = device
        self.profile_name = None
        self.log = logging.getLogger("test.monitor")

    async def device(self) -> MockDevice:
        await self._device.connect()
        return self._device


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


async def test_subscribe_observations_streams_then_stops() -> None:
    """The subscription emits an immediate burst, keeps streaming, and halts on unsub."""
    device = MockDevice()
    await device.connect()

    seen: list[Observation] = []
    unsubscribe = await device.subscribe_observations(seen.append)
    try:
        # The first burst is emitted synchronously, before any await.
        first_burst = len(seen)
        assert first_burst >= 1
        assert any(o.lat is not None for o in seen)  # a located repeater is in the burst

        await asyncio.sleep(_MOCK_INTERVAL * 3)
        assert len(seen) > first_burst  # more packets arrived while we did nothing
    finally:
        unsubscribe()

    frozen = len(seen)
    await asyncio.sleep(_MOCK_INTERVAL * 3)
    assert len(seen) == frozen  # nothing more after unsubscribing


async def test_disconnect_stops_background_emitter() -> None:
    """Disconnecting cancels any live subscription task rather than leaking it."""
    device = MockDevice()
    await device.connect()
    await device.subscribe_observations(lambda _obs: None)
    assert device._bg_tasks  # a background emitter is running
    await device.disconnect()
    await asyncio.sleep(0)  # let the cancellation settle
    assert not device._bg_tasks


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


def test_observation_count_totals_every_run(tmp_path: Path) -> None:
    """observation_count sums observations across all runs, empty database included."""
    repo = Repository(tmp_path / "count.db")
    assert repo.observation_count() == 0
    run_id = repo.start_run("monitor", {})
    repo.record_observation(run_id, Observation(node="a1", snr=1.0))
    repo.record_observation(run_id, Observation(node="b2", snr=2.0))
    assert repo.observation_count() == 2
    repo.close()


def test_monitor_store_defaults_off_and_persists(tmp_path: Path) -> None:
    """The preference defaults to off and round-trips through a fresh store instance."""
    path = tmp_path / "monitor.json"
    assert MonitorStore(path).load_enabled() is False  # nothing saved yet → off
    MonitorStore(path).save_enabled(True)
    assert MonitorStore(path).load_enabled() is True  # remembered across instances
    MonitorStore(path).save_enabled(False)
    assert MonitorStore(path).load_enabled() is False


async def test_monitor_service_captures_in_background(tmp_path: Path) -> None:
    """Enabling starts a live subscription that logs observations without blocking."""
    repo = Repository(tmp_path / "svc.db")
    ctx = _StubContext(repo, MockDevice())
    store = MonitorStore(tmp_path / "monitor.json")
    service = MonitorService(ctx, store)

    assert not service.enabled and not service.active
    await service.enable()
    assert service.enabled and service.active
    assert store.load_enabled() is True  # preference persisted for next session

    await asyncio.sleep(_MOCK_INTERVAL * 3)  # let packets stream in while we "do other work"
    assert service.session_count > 0
    # Started from an empty database, so total equals what this session captured.
    assert service.total_count() == service.session_count

    await service.disable()
    assert not service.enabled and not service.active
    assert store.load_enabled() is False
    assert repo.observation_count() == service.session_count  # everything was logged

    frozen = service.session_count
    await asyncio.sleep(_MOCK_INTERVAL * 3)
    assert service.session_count == frozen  # capture really stopped
    repo.close()


async def test_monitor_service_enable_without_device_keeps_preference(tmp_path: Path) -> None:
    """If capture can't start, the on preference is still remembered so it can resume."""

    class _NoDevice(_StubContext):
        async def device(self) -> MockDevice:
            raise ValueError("no companion device selected")

    repo = Repository(tmp_path / "nodev.db")
    service = MonitorService(_NoDevice(repo, MockDevice()), MonitorStore(tmp_path / "m.json"))

    with pytest.raises(ValueError):
        await service.enable()
    assert service.enabled is True  # preference stays on to resume later
    assert service.active is False
    assert service.last_error == "no companion device selected"
    repo.close()
