"""DeviceState session-cache tests: read once, serve cached, invalidate on write.

The cache exists to keep screen navigation off the slow radio round-trips (contacts,
self-info, the channel probe). These tests pin the two behaviours that make it correct:
stable facts are fetched once and reused until an in-app write invalidates them, and the
contacts list refreshes in the background once it ages past the TTL without ever blocking a
read. A fake device counts round-trips so "served from cache" is assertable without hardware.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from meshterm.services.device_state import DeviceState, _CONTACTS_TTL_S


class FakeDevice:
    """A stand-in device that counts how often each cached read hits the wire."""

    def __init__(self) -> None:
        self.contacts_calls = 0
        self.self_info_calls = 0
        self.mode_calls = 0
        self.channel_calls = 0
        self.capacity_calls = 0

    async def get_contacts(self) -> list:
        self.contacts_calls += 1
        return [f"contact-{self.contacts_calls}"]  # a fresh identity per fetch, to spot refreshes

    async def get_self_info(self) -> dict:
        self.self_info_calls += 1
        return {"name": "node", "tx_power": 20}

    async def get_path_hash_mode(self) -> int:
        self.mode_calls += 1
        return 2

    async def get_channel(self, idx: int):
        # One configured slot at index 0, then the firmware "rejects" index 1 to end the probe.
        self.channel_calls += 1
        if idx == 0:
            return {"channel_name": "public", "channel_secret": b"\x00" * 16}
        raise RuntimeError("out of range")

    async def channel_capacity(self) -> int:
        # A fixed hardware constant; the cache must read it exactly once for the session.
        self.capacity_calls += 1
        return 8


def _devstate(device: FakeDevice) -> DeviceState:
    """A DeviceState over a fake ctx exposing just device() and a silent logger."""

    async def device_getter():
        return device

    ctx = SimpleNamespace(
        device=device_getter,
        log=SimpleNamespace(debug=lambda *a, **k: None),
    )
    return DeviceState(ctx)  # type: ignore[arg-type]


def test_stable_facts_are_fetched_once_and_served_from_cache() -> None:
    """self-info, path-hash mode, and channels read the radio once, then reuse the value."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        for _ in range(3):
            assert (await ds.self_info())["name"] == "node"
            assert await ds.path_hash_mode() == 2
            assert [s.name for s in await ds.channel_slots()] == ["public"]

    asyncio.run(run())
    assert dev.self_info_calls == 1
    assert dev.mode_calls == 1
    # The channel probe ran once (idx 0 ok, idx 1 rejected) and was cached wholesale.
    assert dev.channel_calls == 2


def test_contacts_served_from_cache_within_ttl() -> None:
    """Repeated contacts reads inside the TTL hit the wire exactly once."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        first = await ds.contacts()
        for _ in range(5):
            assert await ds.contacts() is first  # same cached list object

    asyncio.run(run())
    assert dev.contacts_calls == 1


def test_contacts_refresh_in_background_past_ttl() -> None:
    """Past the TTL a read returns the stale list instantly and refreshes behind it."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        stale = await ds.contacts()  # first fetch
        assert dev.contacts_calls == 1
        # Age the cache past the TTL, then read: the read must return immediately (the stale
        # copy) and schedule a background refresh rather than block on the slow call.
        ds._contacts_at = time.monotonic() - _CONTACTS_TTL_S - 1
        served = await ds.contacts()
        assert served is stale  # served the old list, did not block on a re-fetch
        # Let the scheduled background refresh run.
        await asyncio.gather(*list(ds._tasks))
        assert dev.contacts_calls == 2  # refreshed behind the read
        assert (await ds.contacts())[0] == "contact-2"  # now serving the fresh list

    asyncio.run(run())


def test_force_bypasses_the_contacts_cache() -> None:
    """A forced read always re-fetches, for the rare caller that needs it fresh."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await ds.contacts()
        await ds.contacts(force=True)

    asyncio.run(run())
    assert dev.contacts_calls == 2


def test_invalidation_forces_a_re_read() -> None:
    """Each invalidate drops exactly its own entry so the next read re-fetches it."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await ds.self_info(); await ds.path_hash_mode(); await ds.channel_slots(); await ds.contacts()
        # invalidate_config drops self-info + path-hash mode together (the config editor's write).
        ds.invalidate_config()
        await ds.self_info(); await ds.path_hash_mode()
        assert dev.self_info_calls == 2 and dev.mode_calls == 2
        # channels and contacts were untouched by that invalidation.
        assert dev.channel_calls == 2 and dev.contacts_calls == 1
        ds.invalidate_channels()
        await ds.channel_slots()
        assert dev.channel_calls == 4  # probed again

    asyncio.run(run())


def test_reset_clears_everything() -> None:
    """A reconnect's reset drops the whole cache so every fact is re-read on next use."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await ds.self_info(); await ds.contacts(); await ds.path_hash_mode(); await ds.channel_capacity()
        ds.reset()
        await ds.self_info(); await ds.contacts(); await ds.path_hash_mode(); await ds.channel_capacity()

    asyncio.run(run())
    assert dev.self_info_calls == 2
    assert dev.contacts_calls == 2
    assert dev.mode_calls == 2
    assert dev.capacity_calls == 2  # capacity is a hardware constant, but a reconnect re-reads it


def test_channel_capacity_is_fetched_once_and_served_from_cache() -> None:
    """Capacity is a hardware constant: probed once, then reused for the session."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        for _ in range(3):
            assert await ds.channel_capacity() == 8

    asyncio.run(run())
    assert dev.capacity_calls == 1


def test_prewarm_fills_the_slow_caches_off_the_read_path() -> None:
    """prewarm() warms contacts, channels, and capacity so the first read hits no wire."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        ds.prewarm()
        await asyncio.gather(*list(ds._tasks))  # let the background warm finish
        # Every slow cache was filled by the prewarm: contacts once, the channel probe once
        # (idx 0 ok, idx 1 rejected), and the capacity probe once.
        assert dev.contacts_calls == 1
        assert dev.channel_calls == 2
        assert dev.capacity_calls == 1
        # A screen opening now is served from cache — no additional round-trips.
        await ds.contacts()
        await ds.channel_slots()
        await ds.channel_capacity()
        assert dev.contacts_calls == 1
        assert dev.channel_calls == 2
        assert dev.capacity_calls == 1

    asyncio.run(run())
