"""Smoke tests for the skeleton: models, mock device, service, and persistence.

These run without hardware against the :class:`MockDevice` simulator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshterm.core.connection import DeviceCommandError, MockDevice, clamp_tx_power
from meshterm.core.models import Contact, TraceResult, TraceStats
from meshterm.persistence.repository import Repository
from meshterm.services import trace_runner


def test_clamp_tx_power() -> None:
    """TX power clamps to the supported range."""
    assert clamp_tx_power(-5) == 1
    assert clamp_tx_power(99) == 22
    assert clamp_tx_power(14) == 14


def test_advert_time_refuses_a_future_sender_clock() -> None:
    """An advert stamp is hearsay from the sender's clock: plausible converts, future doesn't.

    A node with a mis-set clock advertises timestamps from the future; taken at face value
    the contact would read "heard now" and top every heard-sorted list until the wall clock
    caught up. Ordinary skew (under the tolerance) stays: its age clamps to "now" briefly
    and self-heals. The classic never-heard shapes (zero/absent/garbage) still map to None.
    """
    from meshterm.core.models import ADVERT_FUTURE_SKEW_S, advert_time, utcnow

    past = int(utcnow().timestamp()) - 3600
    when = advert_time(past)
    assert when is not None and int(when.timestamp()) == past
    assert advert_time(int(utcnow().timestamp()) + 60) is not None  # ordinary skew
    assert advert_time(int(utcnow().timestamp()) + ADVERT_FUTURE_SKEW_S + 60) is None
    assert advert_time(0) is None
    assert advert_time(None) is None
    assert advert_time("garbage") is None


def test_trace_stats_aggregation() -> None:
    """Robust stats reflect successes and bottleneck SNR."""
    from meshterm.core.models import Hop

    traces = [
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 5.0), Hop(1, "b", -2.0)]),
        TraceResult(target="x", success=False),
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 7.0), Hop(1, "b", 0.0)]),
    ]
    stats = TraceStats.from_traces("x", traces)
    assert stats.samples == 3
    assert stats.successes == 2
    assert stats.success_rate == pytest.approx(2 / 3)
    assert stats.median_min_snr == pytest.approx(-1.0)  # median of [-2.0, 0.0]


def test_parse_trace_path_mixes_names_and_hex() -> None:
    """Names and hex mix; the hex width is preserved and names truncate to match."""
    contacts = [Contact(name="Alice", key_prefix="d4e5f6a7")]
    # 1-byte hops: hex, a contact name (case-insensitively), hex again
    assert trace_runner.parse_trace_path("3d, alice ,f2", contacts) == "3d,d4,f2"
    # the width comes from the hex tokens you type — no truncation to 1 byte
    assert trace_runner.parse_trace_path("a1b2c3", contacts) == "a1b2c3"


def test_parse_trace_path_preserves_three_byte_width() -> None:
    """A 3-byte (6 hex) width is kept, and names truncate to that width."""
    contacts = [Contact(name="Alice", key_prefix="d4e5f6a7b8c9")]
    assert trace_runner.parse_trace_path("3d5f7a,Alice,f2a1b3", contacts) == "3d5f7a,d4e5f6,f2a1b3"


def test_parse_trace_path_rejects_mixed_widths() -> None:
    """Hex hops must all use the same number of bytes."""
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("3d,a1b2c3", contacts=[])


def test_last_traced_by_name_folds_hex_to_contacts_and_keeps_latest() -> None:
    """The Trace picker's TRACED lane folds every stored target onto its contact.

    A node traced as "alice" one day and by its key prefix another shows one honest last-
    traced time — the latest of the two. A hex-looking *name* stays a name (matched by
    name, not address), and an unknown prefix that names no contact simply drops out.
    """
    from datetime import datetime, timezone

    from meshterm.tools.trace import _last_traced_by_name

    contacts = [
        Contact(name="Alice", public_key="d4e5f6a7" + "00" * 28, key_prefix="d4e5f6a7"),
        Contact(name="cafe", public_key="12ab34cd" + "00" * 28, key_prefix="12ab34cd"),
    ]

    def when(day: int) -> datetime:
        return datetime(2026, 7, day, tzinfo=timezone.utc)

    traced = {
        "d4e5f6": when(1),  # Alice by an early key-prefix trace
        "Alice": when(9),  # …and by name, more recently — the latest must win
        "cafe": when(5),  # a hex-looking *name*, matched as a name
        "beefbeef": when(3),  # a prefix that names no contact — dropped
    }
    assert _last_traced_by_name(contacts, traced) == {"Alice": when(9), "cafe": when(5)}


def test_node_type_resolver_matches_hop_hash_to_contact_type() -> None:
    """A relay's hash resolves to its contact's node type (prefix-matched either way)."""
    from meshterm.core.models import NODE_TYPE_REPEATER

    type_of = trace_runner.make_node_type_resolver(
        [
            Contact(
                name="Repeater",
                public_key="3d63c6" + "00" * 26,
                key_prefix="3d63c6429436",
                node_type=NODE_TYPE_REPEATER,
            ),
            Contact(name="Typeless", public_key="a1b2c3" + "00" * 26, key_prefix="a1b2c3d4"),
        ]
    )
    assert type_of("3d63") == NODE_TYPE_REPEATER  # a short hash prefixes the full key
    assert type_of("a1b2") is None  # known contact, but no advertised type
    assert type_of("ffff") is None  # unknown node
    assert type_of(None) is None  # our own device, passed through


def test_key_resolver_expands_a_stored_prefix_to_the_full_key() -> None:
    """A heard node's stored 12-hex prefix resolves to the contact's whole public key."""
    full = "3d63c6429436" + "ab" * 26  # 64 hex
    resolve = trace_runner.make_key_resolver(
        [
            Contact(name="Repeater", public_key=full, key_prefix="3d63c6429436"),
            Contact(name="Keyless", public_key="", key_prefix="a1b2c3d4"),
        ]
    )
    assert resolve("3d63c6429436") == full  # the stored prefix begins the contact's key
    assert resolve("3d63") == full  # any shorter slice of it, too
    assert resolve("ffffffffffff") == "ffffffffffff"  # no contact: the prefix stands
    assert resolve("a1b2c3d4") == "a1b2c3d4"  # contact has no full key to expand to
    assert resolve(None) is None


def test_path_hash_flags_power_of_two_only() -> None:
    """Trace flags encode the hash width as 1 << s, so only 1/2/4/8 bytes map."""
    assert trace_runner.path_hash_flags(1) == 0
    assert trace_runner.path_hash_flags(2) == 1
    assert trace_runner.path_hash_flags(4) == 2
    assert trace_runner.path_hash_flags(8) == 3
    assert trace_runner.path_hash_flags(3) is None


def test_parse_trace_hops_reads_path_snr() -> None:
    """Per-hop SNR comes from payload['path'] dicts; the final hash-less node counts."""
    from meshterm.core.connection import parse_trace_hops

    payload = {
        "path": [
            {"hash": "3d", "snr": 4.5},
            {"hash": "f2", "snr": -2.0},
            {"snr": 1.25},  # final node: our device, no hash
        ]
    }
    hops = parse_trace_hops(payload)
    assert [h.node for h in hops] == ["3d", "f2", None]
    assert [h.snr for h in hops] == [4.5, -2.0, 1.25]
    assert [h.index for h in hops] == [0, 1, 2]


def test_parse_trace_hops_empty_without_path() -> None:
    """A reply with no parsed path yields no hops (SNR shows as n/a)."""
    from meshterm.core.connection import parse_trace_hops

    assert parse_trace_hops({}) == []


def test_route_text_annotates_nodes_with_command_width_hash() -> None:
    """The route shows each node's hash at the command's path-hash width."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import _route_text

    resolve = trace_runner.make_node_resolver(
        [Contact(name="Alice", public_key="3d63c6" + "00" * 26, key_prefix="3d63c6429436")]
    )
    # Known node (resolves to Alice) then the reply returning to our device (node=None).
    result = TraceResult(
        target="Alice",
        success=True,
        hops=[Hop(0, "3d63c6", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=2,  # command used 2-byte hashes
    )
    plain = _route_text(result, "Me", resolve).plain
    assert "Alice" in plain
    assert "(3d63)" in plain  # truncated to 2 bytes, not the full 3d63c6
    assert "3d63c6" not in plain
    assert plain.count("Me") == 2  # our device at both ends, carrying no hash
    assert "Me (" not in plain.replace(" ", " ")  # device never annotated


def test_route_text_annotates_our_device_with_hash() -> None:
    """Given our key, both endpoints (us) carry our hash at the command width."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import _route_text

    result = TraceResult(
        target="x",
        success=True,
        hops=[Hop(0, "3d63", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=2,  # command used 2-byte hashes
    )
    plain = _route_text(result, "Me", device_hash="a1b2c3" + "00" * 29).plain.replace("\xa0", " ")
    assert plain.count("Me (a1b2)") == 2  # our device at both ends, carrying our hash
    assert "a1b2c3" not in plain  # truncated to the command's 2-byte width


def test_traces_table_annotates_links_with_hashes() -> None:
    """The per-trace From→To column shows ``name (hash)``, including our device."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import traces_table

    resolve = trace_runner.make_node_resolver(
        [Contact(name="Alice", public_key="3d63c6" + "00" * 26, key_prefix="3d63c6429436")]
    )
    traces = [
        TraceResult(
            target="Alice",
            success=True,
            hops=[Hop(0, "3d63c6", 12.0), Hop(1, None, 9.0)],
            path_hash_bytes=2,  # command used 2-byte hashes
        )
    ]
    table = traces_table(traces, "Me", resolve, device_hash="a1b2c3" + "00" * 29)
    links = [cell.plain.replace("\xa0", " ") for cell in table.columns[1].cells]
    assert any("Me (a1b2)" in link for link in links)  # our device carries its hash
    assert any("Alice (3d63)" in link for link in links)  # truncated to the 2-byte width
    assert not any("3d63c6" in link for link in links)


def test_route_text_unknown_node_shows_hash_only() -> None:
    """An unresolved node shows just its hash, truncated to the command width."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import _route_text

    result = TraceResult(
        target="x",
        success=True,
        hops=[Hop(0, "aa11bb", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=1,  # 1-byte width
    )
    plain = _route_text(result, "Me").plain.replace(" ", " ")
    assert "→ aa →" in plain  # 1-byte hash, no name, no parens
    assert "aa11bb" not in plain


def test_route_text_contains_no_non_breaking_spaces() -> None:
    """prompt_toolkit draws U+00A0 as an underscore, so route text must never emit one."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import _route_text

    result = TraceResult(
        target="x",
        success=True,
        hops=[Hop(0, "3d63", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=2,
    )
    plain = _route_text(result, "Me", device_hash="a1b2" + "00" * 30).plain
    assert "\xa0" not in plain
    assert plain == "Me (a1b2) → 3d63 → Me (a1b2)"


def test_parse_trace_path_rejects_unknown_token() -> None:
    """A token that is neither a known contact nor valid hex is an error."""
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("nope", contacts=[])
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("  ", contacts=[])


async def test_adopt_device_is_reused_without_reopening(tmp_path: Path) -> None:
    """A device adopted from the startup probe is returned by ``device()`` as-is."""
    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
    from meshterm.core.config import Settings
    from meshterm.core.device_store import DeviceStore

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "adopt.db")
    ctx = AppContext(
        console=Console(file=__import__("io").StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
    )
    try:
        adopted = MockDevice()
        await adopted.connect()
        ctx.adopt_device(adopted)
        # ``device()`` must hand back the very connection we adopted — never open another.
        assert await ctx.device() is adopted
    finally:
        ctx.repo.close()


async def test_mock_device_honors_forced_path() -> None:
    """A forced path drives the repeater hops; a return-to-us hop is appended."""
    device = MockDevice()
    await device.connect()
    result = await device.run_trace("Alice", path="3d,f2,3d")
    # Three forced repeater hops, plus the hash-less hop for the reply returning to us.
    assert result.hop_count == 4
    assert [h.node for h in result.hops] == ["3d", "f2", "3d", None]


async def test_mock_device_remove_contact_drops_it_from_the_table() -> None:
    """Removing a contact deletes it from the device's table (matched by public key)."""
    from meshterm.core.connection import ContactNotOnDeviceError

    device = MockDevice()
    await device.connect()
    before = await device.get_contacts()
    alice = next(c for c in before if c.name == "Alice")
    await device.remove_contact(alice)
    after = await device.get_contacts()
    assert "Alice" not in {c.name for c in after}
    assert len(after) == len(before) - 1
    # Removing one it no longer holds is refused the way firmware refuses it, so the
    # screen's "wasn't on the device, removed here anyway" path is walkable on --mock.
    with pytest.raises(ContactNotOnDeviceError):
        await device.remove_contact(alice)
    assert len(await device.get_contacts()) == len(after)


async def test_mock_device_blank_path_traces() -> None:
    """A blank/auto path still produces a successful, non-empty trace."""
    device = MockDevice()
    await device.connect()
    # Try several times since the simulator drops very weak links occasionally.
    results = [await device.run_trace("Alice") for _ in range(10)]
    assert any(r.success and r.hops for r in results)


def test_trace_timeout_scales_with_hops() -> None:
    """The reply-wait grows one allowance per hop, within its documented bounds."""
    from meshterm.services.trace_runner import (
        TRACE_TIMEOUT_BASE_S,
        TRACE_TIMEOUT_CEILING_S,
        TRACE_TIMEOUT_FLOOD_S,
        TRACE_TIMEOUT_PER_HOP_S,
        trace_timeout,
    )

    # An unknown hop count (a path-less flood) can't be sized, so it falls back to the
    # flat historical budget.
    assert trace_timeout(0) == TRACE_TIMEOUT_FLOOD_S
    assert trace_timeout(-3) == TRACE_TIMEOUT_FLOOD_S

    # Each hop adds exactly one per-hop allowance on top of the fixed base...
    assert trace_timeout(1) == TRACE_TIMEOUT_BASE_S + TRACE_TIMEOUT_PER_HOP_S
    assert trace_timeout(5) == TRACE_TIMEOUT_BASE_S + 5 * TRACE_TIMEOUT_PER_HOP_S
    # ...so a longer walk is always given longer to come home than a shorter one...
    assert trace_timeout(8) > trace_timeout(3)
    # ...until the ceiling caps a route that would otherwise scale without end.
    assert trace_timeout(10_000) == TRACE_TIMEOUT_CEILING_S


async def test_run_trace_sizes_wait_to_the_forced_route(monkeypatch) -> None:  # noqa: ANN001
    """A forced path's reply-wait is sized to its hop count, not a flat default.

    The real device must give the reply the route-scaled budget so a long walk isn't
    cut off before it can travel out and back.
    """
    import asyncio as _asyncio

    from meshterm.core import connection as connection_module
    from meshterm.core.connection import MeshCoreDevice
    from meshterm.services.trace_runner import trace_timeout

    seen: dict[str, float] = {}
    real_wait_for = _asyncio.wait_for

    async def spy_wait_for(awaitable, timeout):  # noqa: ANN001, ANN202
        seen["timeout"] = timeout
        return await real_wait_for(awaitable, 0)  # a miss, without burning the budget

    monkeypatch.setattr(connection_module.asyncio, "wait_for", spy_wait_for)

    class _Commands:
        async def send_trace(self, *, auth_code, tag, flags, path):  # noqa: ANN001, ANN201
            return None

    class _MC:
        commands = _Commands()

        def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001, ANN201
            return _Subscription()

    class _Subscription:
        def unsubscribe(self) -> None:
            return None

    device = MeshCoreDevice(port="COM-test")
    device._mc = _MC()

    # Five single-byte hops → a five-hop walk (the spec is already the mirrored round trip).
    result = await device.run_trace("Alice", path="3d,f2,3d,f2,3d")
    assert result.success is False
    assert seen["timeout"] == trace_timeout(5)

    # An explicit timeout still wins over the auto-scaling.
    await device.run_trace("Alice", path="3d,f2,3d,f2,3d", timeout=2.0)
    assert seen["timeout"] == 2.0


async def test_run_trace_catches_a_reply_that_beats_the_send_acknowledgement() -> None:
    """A reply landing while ``send_trace`` is still blocked is still our trace's reply.

    The companion's send acknowledgement is correlated by nothing but its event type, so
    a concurrent command can consume ours and leave the send waiting out its own default.
    Listening for the tag only *after* the send returns dropped every reply that arrived
    in that window — the trace read as "no reply" though the mesh had answered it.
    """
    import asyncio as _asyncio

    from meshterm.core.connection import MeshCoreDevice

    class _Event:
        payload = {"tag": 1, "path": [{"hash": "3d", "snr": 4.0}, {"snr": 2.0}]}

    listeners: list = []

    class _Commands:
        async def send_trace(self, *, auth_code, tag, flags, path):  # noqa: ANN001, ANN201
            # The reply overtakes the acknowledgement: deliver it, then keep stalling.
            for callback in listeners:
                callback(_Event())
            await _asyncio.sleep(0)
            return None

        async def send_appstart(self):  # noqa: ANN201 - the tx-power read on success
            return _SelfInfo()

    class _SelfInfo:
        payload = {"tx_power": 20}

    class _MC:
        commands = _Commands()

        def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001, ANN201
            listeners.append(callback)
            return _Subscription()

    class _Subscription:
        def unsubscribe(self) -> None:
            listeners.clear()

    device = MeshCoreDevice(port="COM-test")
    device._mc = _MC()
    result = await device.run_trace("Alice", path="3d,f2,3d")
    assert result.success is True
    assert [h.node for h in result.hops] == ["3d", None]


def test_trace_edges_endpoints_are_our_device() -> None:
    """The first edge originates at us and the last edge returns to us (#3/#4)."""
    from meshterm.core.models import Hop

    result = TraceResult(
        target="Alice",
        success=True,
        hops=[Hop(0, "3d", 5.0), Hop(1, "f2", 1.0), Hop(2, None, -1.0)],
    )
    edges = result.edges(device_label="us")
    assert [(e.origin, e.destination) for e in edges] == [
        ("us", "3d"),
        ("3d", "f2"),
        ("f2", "us"),
    ]
    assert [e.snr for e in edges] == [5.0, 1.0, -1.0]


def test_trace_stats_reports_per_hop_medians() -> None:
    """Aggregation yields a median SNR for every hop position (#5)."""
    from meshterm.core.models import Hop

    traces = [
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 4.0), Hop(1, None, 0.0)]),
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 8.0), Hop(1, None, 2.0)]),
    ]
    stats = TraceStats.from_traces("x", traces)
    # Node identities are raw (None = our device) so a label is applied at render time.
    assert [(h.origin, h.destination, h.median_snr) for h in stats.hop_snrs] == [
        (None, "a", 6.0),
        ("a", None, 1.0),
    ]


async def test_mock_device_trace_is_unimodal_in_tx() -> None:
    """The simulator peaks near its optimal TX power (averaged over noise)."""
    device = MockDevice(optimal_tx=14)
    await device.connect()

    async def avg_snr(tx: int) -> float:
        await device.set_tx_power(tx)
        results = await trace_runner.run_traces(device, "Alice", samples=15, cooldown_s=0)
        stats = TraceStats.from_traces("Alice", results)
        return stats.median_min_snr or -99.0

    low, peak, high = await avg_snr(2), await avg_snr(14), await avg_snr(22)
    assert peak > low
    assert peak > high


async def _setup_link(optimal_remote_tx: int = 20):
    """Return a connected mock plus the (admin, target) contacts and forced path.

    The path runs admin → target, so the optimizer tunes the admin node and reads the
    SNR the target receives from it.
    """
    device = MockDevice(optimal_remote_tx=optimal_remote_tx)
    await device.connect()
    contacts = await device.get_contacts()
    admin = next(c for c in contacts if c.name == "Yagi-Repeater")
    target = next(c for c in contacts if c.name == "Alice")
    path = trace_runner.parse_trace_path(f"{admin.name},{target.name}", contacts)
    assert await device.admin_login(admin, "admin")
    return device, admin, target, path


async def test_admin_login_rejects_wrong_password() -> None:
    """A wrong admin password is refused, and the node stays un-tunable."""
    from meshterm.core.connection import LoginResult

    device = MockDevice(admin_password="secret")
    await device.connect()
    admin = (await device.get_contacts())[0]

    refused = await device.admin_login(admin, "nope")
    assert refused is LoginResult.REFUSED and not refused
    with pytest.raises(DeviceCommandError):
        await device.set_remote_tx_power(admin, 18)  # not logged in
    accepted = await device.admin_login(admin, "secret")
    assert accepted is LoginResult.ACCEPTED and accepted
    await device.set_remote_tx_power(admin, 18)  # now allowed
    assert await device.get_remote_tx_power(admin) == 18


async def test_tx_optimizer_finds_simulator_peak(tmp_path: Path) -> None:
    """The optimizer converges near the simulated remote optimum and applies it."""
    from meshterm.services import tx_optimizer

    device, admin, target, path = await _setup_link(optimal_remote_tx=20)

    result = await tx_optimizer.optimize_tx_power(
        device,
        target.name,
        admin,
        path,
        samples_per_level=8,
        coarse_step=3,
        cooldown_s=0,
    )
    assert abs(result.best_tx - 20) <= 3  # near the true remote peak
    assert result.best_success_rate == 1.0  # reliability-first: the winner never drops
    assert result.applied
    assert await device.get_remote_tx_power(admin) == result.best_tx  # left tuned
    assert result.admin_node == "Yagi-Repeater"


async def test_tx_optimizer_no_apply_restores_original(tmp_path: Path) -> None:
    """With apply off, the node is left at the power it started with."""
    from meshterm.services import tx_optimizer

    device, admin, target, path = await _setup_link()
    await device.set_remote_tx_power(admin, 15)  # a known starting power

    result = await tx_optimizer.optimize_tx_power(
        device,
        target.name,
        admin,
        path,
        samples_per_level=4,
        coarse_step=4,
        refine=False,
        verify=False,
        apply=False,
        cooldown_s=0,
    )
    assert result.original_tx == 15
    assert not result.applied
    assert await device.get_remote_tx_power(admin) == 15  # restored, not the winner


async def test_tx_optimizer_reports_phases_in_order() -> None:
    """on_phase announces each enabled search stage, in coarse → refine → verify order."""
    from meshterm.services import tx_optimizer

    device, admin, target, path = await _setup_link()
    phases: list[str] = []

    await tx_optimizer.optimize_tx_power(
        device,
        target.name,
        admin,
        path,
        samples_per_level=2,
        coarse_step=6,
        apply=False,
        cooldown_s=0,
        on_phase=phases.append,
    )
    assert phases == list(tx_optimizer.PHASES)

    phases.clear()
    await tx_optimizer.optimize_tx_power(
        device,
        target.name,
        admin,
        path,
        samples_per_level=2,
        coarse_step=6,
        refine=False,
        verify=False,
        apply=False,
        cooldown_s=0,
        on_phase=phases.append,
    )
    assert phases == ["coarse"]  # disabled stages are never announced


def test_select_best_prefers_reliability_then_lowest_power() -> None:
    """A 100%-reliable level beats a flakier higher-SNR one; ties go to lower TX."""
    from meshterm.core.models import TraceStats, TxLevelResult
    from meshterm.services.tx_optimizer import select_best

    def level(tx: int, snr: float, successes: int, samples: int = 5) -> TxLevelResult:
        return TxLevelResult(
            tx_power=tx,
            samples=samples,
            successes=successes,
            target_snr=snr,
            score=snr,
            stats=TraceStats(
                target="t",
                samples=samples,
                successes=successes,
                median_min_snr=snr,
                median_rtt_ms=None,
            ),
        )

    # A flaky level with a great SNR must not beat a perfectly reliable one.
    flaky = level(26, snr=12.0, successes=3)
    solid = level(20, snr=7.0, successes=5)
    assert select_best([flaky, solid]).tx_power == 20

    # Among reliable levels with SNR within tolerance, the lowest TX wins.
    a = level(18, snr=7.4, successes=5)
    b = level(22, snr=8.0, successes=5)
    assert select_best([a, b], snr_tolerance=1.0).tx_power == 18


def test_round_trip_path_mirrors_back_to_a_reachable_node() -> None:
    """The trace path goes out to the target and back, so a near node answers."""
    from meshterm.services.tx_optimizer import _round_trip_path

    assert _round_trip_path(["3f", "f2"]) == "3f,f2,3f"
    assert _round_trip_path(["3d", "3f", "f2"]) == "3d,3f,f2,3f,3d"
    assert _round_trip_path(["f2"]) == "f2"  # single hop: nothing to mirror


def test_trace_target_snr_matches_target_hop_not_position() -> None:
    """The target's SNR is read from its hash, even when it's the turn-around hop."""
    from meshterm.core.models import Hop
    from meshterm.services.tx_optimizer import trace_target_snr

    # Round trip 3f -> f2 -> 3f -> us: the f2 hop (index 1) is what we want, not the
    # later 3f hop or the hash-less return hop.
    trace = TraceResult(
        target="f2",
        success=True,
        hops=[Hop(0, "3f", 4.0), Hop(1, "f2", 7.5), Hop(2, "3f", 3.0), Hop(3, None, 4.0)],
    )
    assert trace_target_snr(trace, "f2") == 7.5
    assert trace_target_snr(TraceResult(target="f2", success=False), "f2") is None


class _Event:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def is_error(self) -> bool:
        return False


def _mc_with(contacts: dict, path_hash_mode: int = 2):
    """Build a minimal mock ``MeshCore`` client exposing the trace commands used."""

    class _Commands:
        async def get_contacts(self) -> _Event:
            return _Event(contacts)

        async def get_path_hash_mode(self) -> int:
            return path_hash_mode

    class _MC:
        commands = _Commands()

    return _MC()


async def test_contact_with_future_advert_stamp_reads_as_never_heard() -> None:
    """A contact whose advert was stamped by a wrong (future) clock arrives with no heard time.

    The firmware stores the advert's embedded timestamp verbatim and only ever advances it
    (its replay filter), so a bogus future stamp sticks until real time passes it — the
    conversion refuses it here rather than showing the contact as "heard in the future".
    """
    from meshterm.core.connection import MeshCoreDevice
    from meshterm.core.models import utcnow

    now = int(utcnow().timestamp())
    mc = _mc_with(
        {
            "Bogus-Clock": {
                "adv_name": "Bogus-Clock",
                "public_key": "aa" * 32,
                "last_advert": now + 7 * 86400,
            },
            "Honest": {"adv_name": "Honest", "public_key": "bb" * 32, "last_advert": now - 300},
        }
    )
    device = MeshCoreDevice(port="COM-test")
    device._mc = mc
    by_name = {c.name: c for c in await device.get_contacts()}
    assert by_name["Bogus-Clock"].last_seen is None  # refused, not "heard in the future"
    assert by_name["Honest"].last_seen is not None


async def test_three_byte_route_appends_destination_hash() -> None:
    """A learned multi-hop route walks the repeaters, the target, then back (#1).

    Two things matter for a non-power-of-two region (3-byte hashes): each 3-byte
    routing hash collapses to its leading 2 bytes (the widest representable trace
    width), and the destination's own hash is appended as the final outbound hop —
    a trace only replies when its destination is the last outbound hop. The trace
    protocol has no separate return-path field, so the same repeaters are mirrored
    back afterwards — otherwise nothing relays the reply home.
    """
    from meshterm.core.connection import MeshCoreDevice

    mc = _mc_with(
        {
            "Repeater": {
                "adv_name": "Repeater",
                "public_key": "aabbcc" + "00" * 29,
                # Two hops, each a 3-byte routing hash (mode 2 => size 3).
                "out_path": "112233445566",
                "out_path_len": 2,
                "out_path_hash_mode": 2,
            }
        }
    )

    device = MeshCoreDevice(port="COM-test")
    resolved = await device._trace_path_to_contact(mc, "Repeater")
    assert resolved is not None
    path_bytes, flags = resolved
    # Two repeater hops collapsed to 2 bytes, the target's own 2-byte hash, then
    # the same two repeaters mirrored back to us.
    assert path_bytes == bytes.fromhex("11224455aabb44551122")
    assert flags == trace_runner.path_hash_flags(2)  # 2-byte width


async def test_direct_neighbor_resolves_to_destination_hash() -> None:
    """A direct neighbor (no learned route) resolves to a single destination hop.

    Verified on hardware: these contacts report ``out_path_len == -1`` (no stored
    route) yet answer a single-hop trace addressed to their own hash. With the
    contact's mode unknown (-1) the region's path-hash mode (3-byte) sets the width,
    collapsed to the representable 2 bytes.
    """
    from meshterm.core.connection import MeshCoreDevice

    mc = _mc_with(
        {
            "Neighbor": {
                "adv_name": "Neighbor",
                "public_key": "3d63c6" + "00" * 29,
                # Direct neighbor as the firmware reports it: no learned route.
                "out_path": "",
                "out_path_len": -1,
                "out_path_hash_mode": -1,
            }
        },
        path_hash_mode=2,
    )

    device = MeshCoreDevice(port="COM-test")
    resolved = await device._trace_path_to_contact(mc, "Neighbor")
    assert resolved is not None
    path_bytes, flags = resolved
    # Just the target's own hash, region width 3 collapsed to a 2-byte trace hop.
    assert path_bytes == bytes.fromhex("3d63")
    assert flags == trace_runner.path_hash_flags(2)


async def test_unknown_contact_resolves_to_none() -> None:
    """An unrecognized target yields ``None`` so the trace can fall back to path-less."""
    from meshterm.core.connection import MeshCoreDevice

    mc = _mc_with(
        {
            "Somebody": {
                "adv_name": "Somebody",
                "public_key": "abcdef" + "00" * 29,
                "out_path": "",
                "out_path_len": -1,
                "out_path_hash_mode": -1,
            }
        }
    )

    device = MeshCoreDevice(port="COM-test")
    resolved = await device._trace_path_to_contact(mc, "Nobody")
    assert resolved is None


async def test_contacts_payload_retries_transient_error() -> None:
    """A transient 'no event received' on contacts retrieval is retried, not fatal."""
    from meshterm.core.connection import MeshCoreDevice

    calls = {"n": 0}

    class _Err:
        payload = {"reason": "no event received during contacts retrieval"}

        def is_error(self) -> bool:
            return True

    class _Ok:
        payload = {"Repeater": {"adv_name": "Repeater", "public_key": "aabbcc" + "00" * 29}}

        def is_error(self) -> bool:
            return False

    class _Commands:
        async def get_contacts(self):  # noqa: ANN202
            calls["n"] += 1
            return _Err() if calls["n"] < 3 else _Ok()

    class _MC:
        commands = _Commands()

    device = MeshCoreDevice(port="COM-test")
    payload = await device._contacts_payload(_MC(), retries=3, delay=0)
    assert "Repeater" in payload
    assert calls["n"] == 3  # failed twice, succeeded on the third attempt


async def test_contacts_payload_raises_clean_error_after_retries() -> None:
    """Persistent contacts-retrieval failure raises a clean, actionable error."""
    from meshterm.core.connection import DeviceCommandError, MeshCoreDevice

    class _Err:
        payload = {"reason": "no event received during contacts retrieval"}

        def is_error(self) -> bool:
            return True

    class _Commands:
        async def get_contacts(self):  # noqa: ANN202
            return _Err()

    class _MC:
        commands = _Commands()

    device = MeshCoreDevice(port="COM-test")
    with pytest.raises(DeviceCommandError) as exc:
        await device._contacts_payload(_MC(), retries=2, delay=0)
    assert "no event received" in str(exc.value)


async def test_latest_trace_returns_previous_run(tmp_path: Path) -> None:
    """latest_trace rehydrates the most recent stored trace, excluding a given run."""
    from meshterm.core.models import Hop

    repo = Repository(tmp_path / "prev.db")
    old_run = repo.start_run("trace", {"target": "Alice"})
    repo.record_trace(
        old_run,
        TraceResult(target="Alice", success=True, hops=[Hop(0, "3d", 4.0), Hop(1, None, 1.0)]),
    )
    new_run = repo.start_run("trace", {"target": "Alice"})

    prev = repo.latest_trace("Alice", exclude_run_id=new_run)
    assert prev is not None
    assert [h.node for h in prev.hops] == ["3d", None]
    assert prev.edges("us")[-1].destination == "us"
    assert repo.latest_trace("Nobody") is None
    repo.close()


async def test_repository_round_trip(tmp_path: Path) -> None:
    """A run and its traces persist and read back."""
    repo = Repository(tmp_path / "test.db")
    run_id = repo.start_run("trace", {"target": "Alice", "samples": 2})

    device = MockDevice()
    await device.connect()
    results = await trace_runner.run_traces(
        device, "Alice", samples=2, cooldown_s=0, persist=lambda t: repo.record_trace(run_id, t)
    )
    repo.finish_run(run_id, "ok", {"count": len(results)})

    runs = repo.list_runs()
    assert runs[0].tool == "trace"
    assert runs[0].status == "ok"
    repo.close()
