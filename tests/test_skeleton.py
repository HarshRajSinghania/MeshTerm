"""Smoke tests for the skeleton: models, mock device, service, and persistence.

These run without hardware against the :class:`MockDevice` simulator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshtools.core.connection import MockDevice, clamp_tx_power
from meshtools.core.models import Contact, TraceResult, TraceStats
from meshtools.persistence.repository import Repository
from meshtools.services import trace_runner


def test_clamp_tx_power() -> None:
    """TX power clamps to the supported range."""
    assert clamp_tx_power(-5) == 1
    assert clamp_tx_power(99) == 22
    assert clamp_tx_power(14) == 14


def test_trace_stats_aggregation() -> None:
    """Robust stats reflect successes and bottleneck SNR."""
    from meshtools.core.models import Hop

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
    assert (
        trace_runner.parse_trace_path("3d5f7a,Alice,f2a1b3", contacts)
        == "3d5f7a,d4e5f6,f2a1b3"
    )


def test_parse_trace_path_rejects_mixed_widths() -> None:
    """Hex hops must all use the same number of bytes."""
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("3d,a1b2c3", contacts=[])


def test_path_hash_flags_power_of_two_only() -> None:
    """Trace flags encode the hash width as 1 << s, so only 1/2/4/8 bytes map."""
    assert trace_runner.path_hash_flags(1) == 0
    assert trace_runner.path_hash_flags(2) == 1
    assert trace_runner.path_hash_flags(4) == 2
    assert trace_runner.path_hash_flags(8) == 3
    assert trace_runner.path_hash_flags(3) is None


def test_parse_trace_hops_reads_path_snr() -> None:
    """Per-hop SNR comes from payload['path'] dicts; the final hash-less node counts."""
    from meshtools.core.connection import parse_trace_hops

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
    from meshtools.core.connection import parse_trace_hops

    assert parse_trace_hops({}) == []


def test_route_text_annotates_nodes_with_command_width_hash() -> None:
    """The route shows each node's hash at the command's path-hash width."""
    from meshtools.core.models import Hop
    from meshtools.ui.widgets import _route_text

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
    from meshtools.core.models import Hop
    from meshtools.ui.widgets import _route_text

    result = TraceResult(
        target="x",
        success=True,
        hops=[Hop(0, "3d63", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=2,  # command used 2-byte hashes
    )
    plain = _route_text(
        result, "Me", device_hash="a1b2c3" + "00" * 29
    ).plain.replace("\xa0", " ")
    assert plain.count("Me (a1b2)") == 2  # our device at both ends, carrying our hash
    assert "a1b2c3" not in plain  # truncated to the command's 2-byte width


def test_traces_table_annotates_links_with_hashes() -> None:
    """The per-trace From→To column shows ``name (hash)``, including our device."""
    from meshtools.core.models import Hop
    from meshtools.ui.widgets import traces_table

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
    from meshtools.core.models import Hop
    from meshtools.ui.widgets import _route_text

    result = TraceResult(
        target="x",
        success=True,
        hops=[Hop(0, "aa11bb", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=1,  # 1-byte width
    )
    plain = _route_text(result, "Me").plain.replace(" ", " ")
    assert "→ aa →" in plain  # 1-byte hash, no name, no parens
    assert "aa11bb" not in plain


def test_parse_trace_path_rejects_unknown_token() -> None:
    """A token that is neither a known contact nor valid hex is an error."""
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("nope", contacts=[])
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("  ", contacts=[])


async def test_mock_device_honors_forced_path() -> None:
    """A forced path drives the repeater hops; a return-to-us hop is appended."""
    device = MockDevice()
    await device.connect()
    result = await device.run_trace("Alice", path="3d,f2,3d")
    # Three forced repeater hops, plus the hash-less hop for the reply returning to us.
    assert result.hop_count == 4
    assert [h.node for h in result.hops] == ["3d", "f2", "3d", None]


async def test_mock_device_blank_path_traces() -> None:
    """A blank/auto path still produces a successful, non-empty trace."""
    device = MockDevice()
    await device.connect()
    # Try several times since the simulator drops very weak links occasionally.
    results = [await device.run_trace("Alice") for _ in range(10)]
    assert any(r.success and r.hops for r in results)


def test_trace_edges_endpoints_are_our_device() -> None:
    """The first edge originates at us and the last edge returns to us (#3/#4)."""
    from meshtools.core.models import Hop

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
    from meshtools.core.models import Hop

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
        stats = await trace_runner.measure(device, "Alice", samples=15, cooldown_s=0)
        return stats.median_min_snr or -99.0

    low, peak, high = await avg_snr(2), await avg_snr(14), await avg_snr(22)
    assert peak > low
    assert peak > high


async def test_tx_optimizer_finds_simulator_peak(tmp_path: Path) -> None:
    """The optimizer converges near the simulator's known optimal TX power."""
    from meshtools.services import tx_optimizer

    device = MockDevice(optimal_tx=14)
    await device.connect()
    await device.set_tx_power(20)

    result = await tx_optimizer.optimize_tx_power(
        device, "Alice", samples_per_level=12, coarse_step=3, cooldown_s=0
    )
    assert abs(result.best_tx - 14) <= 2  # within a step of the true peak
    assert result.original_tx == 20
    assert await device.get_tx_power() == 20  # original restored, not the winner
    assert not result.applied


def test_tx_plot_writes_html(tmp_path: Path) -> None:
    """The Plotly renderer produces a self-contained HTML file."""
    import asyncio

    from meshtools.services import tx_optimizer
    from meshtools.viz.tx_plot import render_tx_optimization

    async def _build():
        device = MockDevice(optimal_tx=14)
        await device.connect()
        return await tx_optimizer.optimize_tx_power(
            device, "Alice", samples_per_level=4, coarse_step=4, refine=False, cooldown_s=0
        )

    result = asyncio.run(_build())
    path = render_tx_optimization(result, tmp_path)
    assert path.exists()
    assert path.suffix == ".html"
    assert path.stat().st_size > 1000  # plotly.js inlined → non-trivial size


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


async def test_three_byte_route_appends_destination_hash() -> None:
    """A learned multi-hop route walks the repeaters then ends at the target (#1).

    Two things matter for a non-power-of-two region (3-byte hashes): each 3-byte
    routing hash collapses to its leading 2 bytes (the widest representable trace
    width), and the destination's own hash is appended as the final hop — a trace
    only replies when its destination is the last hop.
    """
    from meshtools.core.connection import MeshCoreDevice

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
    # Two repeater hops collapsed to 2 bytes, then the target's own 2-byte hash.
    assert path_bytes == bytes.fromhex("1122" "4455" "aabb")
    assert flags == trace_runner.path_hash_flags(2)  # 2-byte width


async def test_direct_neighbor_resolves_to_destination_hash() -> None:
    """A direct neighbor (no learned route) resolves to a single destination hop.

    Verified on hardware: these contacts report ``out_path_len == -1`` (no stored
    route) yet answer a single-hop trace addressed to their own hash. With the
    contact's mode unknown (-1) the region's path-hash mode (3-byte) sets the width,
    collapsed to the representable 2 bytes.
    """
    from meshtools.core.connection import MeshCoreDevice

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
    from meshtools.core.connection import MeshCoreDevice

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
    from meshtools.core.connection import MeshCoreDevice

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
    from meshtools.core.connection import DeviceCommandError, MeshCoreDevice

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
    from meshtools.core.models import Hop

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
