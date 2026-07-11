"""Tests for the passive mesh monitor: listening, aggregation, and persistence.

All run against the :class:`MockDevice` simulator and an on-disk SQLite database, no
hardware required.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from meshterm.core.connection import (
    _MOCK_MONITOR_INTERVAL_S as _MOCK_INTERVAL,
)
from meshterm.core.connection import (
    MockDevice,
    ack_from_event,
    message_from_event,
    observation_from_event,
)
from meshterm.core.models import HeardNode, Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.monitor_service import MonitorService


class _StubContext:
    """Minimal stand-in for :class:`~meshterm.context.AppContext` for service tests."""

    def __init__(self, repo: Repository, device: MockDevice) -> None:
        self.repo = repo
        self._device = device
        self._events = None
        self.profile_name = None
        self.log = logging.getLogger("test.monitor")

    async def device(self) -> MockDevice:
        await self._device.connect()
        return self._device

    @property
    def events(self):
        """Lazily build a real event hub bound to this stub context (as AppContext does)."""
        from meshterm.services.event_hub import EventHub

        if self._events is None:
            self._events = EventHub(self)
        return self._events


async def _collect_observations(device: MockDevice, duration_s: float) -> list[Observation]:
    """Subscribe, gather observations for a window, then unsubscribe (test helper)."""
    seen: list[Observation] = []

    def on_event(event) -> None:
        if event.observation is not None:
            seen.append(event.observation)

    unsubscribe = await device.subscribe_events(on_event)
    try:
        await asyncio.sleep(duration_s)
    finally:
        unsubscribe()
    return seen


async def test_subscribe_events_streams_observations_then_stops() -> None:
    """The event stream emits an immediate burst, keeps streaming, and halts on unsub."""
    device = MockDevice()
    await device.connect()

    seen: list[Observation] = []

    def on_event(event) -> None:
        if event.observation is not None:
            seen.append(event.observation)

    unsubscribe = await device.subscribe_events(on_event)
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
    await device.subscribe_events(lambda _event: None)
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


def test_message_from_event_parses_direct_message() -> None:
    """A CONTACT_MSG_RECV payload maps to a direct message with sender and timestamp."""
    from datetime import datetime, timezone

    class _Event:
        payload = {
            "type": "PRIV",
            "pubkey_prefix": "aabbccddeeff",
            "text": "hello there",
            "sender_timestamp": 1_700_000_000,
            "txt_type": 0,
        }

    msg = message_from_event(_Event())
    assert msg is not None
    assert msg.text == "hello there"
    assert msg.sender == "aabbccddeeff"
    assert msg.is_channel is False
    assert msg.channel is None
    assert msg.sender_timestamp == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_message_from_event_parses_channel_message() -> None:
    """A CHANNEL_MSG_RECV payload maps to a channel message with no per-contact sender."""

    class _Event:
        payload = {"type": "CHAN", "channel_idx": 2, "text": "net tonight", "SNR": 5.0}

    msg = message_from_event(_Event())
    assert msg is not None
    assert msg.is_channel is True
    assert msg.channel == 2
    assert msg.sender is None
    assert msg.snr == 5.0


def test_message_from_event_without_text_is_skipped() -> None:
    """A payload carrying no text body yields no message."""

    class _Event:
        payload = {"pubkey_prefix": "aabb"}

    assert message_from_event(_Event()) is None


def test_ack_from_event_extracts_code() -> None:
    """An ACK payload maps to an Ack carrying the correlation code."""

    class _Event:
        payload = {"code": "deadbeef"}

    assert ack_from_event(_Event()).code == "deadbeef"


async def test_observations_round_trip_and_aggregate(tmp_path: Path) -> None:
    """Observations persist and heard_nodes reaggregates them across runs."""
    repo = Repository(tmp_path / "obs.db")
    run_id = repo.start_run("monitor", {"duration": 1})

    device = MockDevice()
    await device.connect()
    observations = await _collect_observations(device, 0.2)
    for obs in observations:
        repo.record_observation(run_id, obs)
    assert observations

    nodes = repo.heard_nodes()
    assert nodes
    # Every aggregated reception should be counted — except packet-log rows, whose SNR
    # belongs to the last relay rather than the node, so they feed topology instead.
    receptions = [o for o in observations if o.kind != "packet"]
    assert sum(n.count for n in nodes) == len(receptions)
    # The simulator also overhears relayed packets, which persist with their path.
    packets = repo.packet_paths()
    assert packets and all(p.hops for p in packets)
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


async def test_monitor_service_records_while_hub_pumps(tmp_path: Path) -> None:
    """start() subscribes before the hub opens, then logs observations once it pumps."""
    repo = Repository(tmp_path / "svc.db")
    ctx = _StubContext(repo, MockDevice())
    service = MonitorService(ctx)

    assert not service.active
    await service.start()  # device-free: no connection has been opened yet
    assert service.active
    await ctx.events.start()  # the hub opens; recording catches the initial burst

    await asyncio.sleep(_MOCK_INTERVAL * 3)  # let packets stream in while we "do other work"
    assert service.session_count > 0
    # Started from an empty database, so total equals what this session captured.
    assert service.total_count() == service.session_count

    await service.stop()
    assert not service.active
    assert repo.observation_count() == service.session_count  # everything was logged

    frozen = service.session_count
    await asyncio.sleep(_MOCK_INTERVAL * 3)
    assert service.session_count == frozen  # recording really stopped

    # The always-on hub keeps listening after recording stops; shut it down cleanly so the
    # simulator's background emitter task doesn't outlive the test.
    await ctx.events.stop()
    await ctx._device.disconnect()
    repo.close()


async def test_monitor_service_start_without_device_is_safe(tmp_path: Path) -> None:
    """Recording can start before any device exists, and a silent session leaves no run."""

    class _NoDevice(_StubContext):
        async def device(self) -> MockDevice:
            raise ValueError("no companion device selected")

    repo = Repository(tmp_path / "nodev.db")
    service = MonitorService(_NoDevice(repo, MockDevice()))

    await service.start()  # never touches the device, so this cannot fail
    assert service.active
    assert service.session_count == 0

    await service.stop()
    assert not service.active
    assert repo.observation_count() == 0
    assert repo.list_runs() == []  # the run row is opened lazily, so none was created
    repo.close()
