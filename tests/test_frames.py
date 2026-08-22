"""Frame-body decoding: what an overheard packet is addressed to.

The companion's RX log hands us every frame's header parsed and its body raw; these
cover the layouts :mod:`meshterm.core.frames` reads out of that body, the length checks
that keep a truncated frame from inventing addressing, and the round trip through
storage — the feed opens on stored history, so what a live frame said about itself has to
survive being written down and read back.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from meshterm.core.connection import packet_observation_from_event
from meshterm.core.frames import frame_addressing, trace_link_snrs
from meshterm.core.models import Observation, utcnow
from meshterm.persistence.repository import Repository


class _Event:
    """A meshcore event stand-in: anything exposing a ``payload`` mapping."""

    def __init__(self, **payload) -> None:
        self.payload = payload


def _frame(typename: str, body: bytes, **extra) -> dict:
    return {"payload_typename": typename, "pkt_payload": body, **extra}


def test_addressed_classes_name_both_ends() -> None:
    """A direct message, request, response or returned path opens with dest then src."""
    body = bytes.fromhex("3da1") + b"\xab\xcd" + b"\x00" * 16
    for typename in ("TEXT_MSG", "REQ", "RESPONSE", "PATH"):
        assert frame_addressing(_frame(typename, body)) == {
            "dest_hash": "3d", "src_hash": "a1",
        }


def test_anonymous_request_carries_its_senders_whole_key() -> None:
    """No shared secret yet, so the sender identifies itself in full — 32 bytes of key."""
    key = bytes(range(32))
    decoded = frame_addressing(_frame("ANON_REQ", b"\x3d" + key + b"\xab\xcd" + b"\x00" * 16))
    assert decoded == {"dest_hash": "3d", "src_key": key.hex()}


def test_tokened_classes_name_their_token() -> None:
    """An ack carries the checksum of what it answers; a trace carries its tag.

    The ack's checksum keeps wire order — the form the companion reports its own delivery
    acks in — while the trace's tag is the little-endian ``uint32`` the firmware wrote, so
    it reads as the same number a trace reply's ``tag`` field does.
    """
    assert frame_addressing(_frame("ACK", bytes.fromhex("9b71e004"))) == {
        "ack_crc": "9b71e004",
    }
    trace = bytes.fromhex("102a3c5f") + bytes.fromhex("deadbeef") + b"\x01"
    assert frame_addressing(_frame("TRACE", trace)) == {"trace_tag": "5f3c2a10"}


def test_a_future_payload_version_is_left_undecoded() -> None:
    """v2 widens the hashes and the MAC, so v1's offsets would slice the wrong bytes."""
    body = bytes.fromhex("3da1") + b"\xab\xcd" + b"\x00" * 16
    assert frame_addressing(_frame("TEXT_MSG", body, payload_ver=0))["dest_hash"] == "3d"
    assert frame_addressing(_frame("TEXT_MSG", body, payload_ver=1)) == {}
    assert frame_addressing(_frame("TEXT_MSG", body))["dest_hash"] == "3d"  # unstated: v1


def test_channel_datagram_is_broken_out_like_a_channel_text() -> None:
    """``GRP_DATA`` shares the channel envelope the library only decodes for ``GRP_TXT``."""
    decoded = frame_addressing(_frame("GRP_DATA", b"\xa3\xab\xcd" + bytes(range(16))))
    assert decoded == {
        "chan_hash": "a3", "cipher_mac": "abcd", "crypted": bytes(range(16)).hex(),
    }


def test_a_body_too_short_for_its_layout_decodes_to_nothing() -> None:
    """Better a silent lane than a plausible hash sliced out of the wrong bytes."""
    assert frame_addressing(_frame("TEXT_MSG", b"\x3d")) == {}
    assert frame_addressing(_frame("ANON_REQ", b"\x3d" + bytes(20))) == {}
    assert frame_addressing(_frame("ACK", b"\x9b")) == {}
    assert frame_addressing(_frame("TRACE", bytes(6))) == {}
    assert frame_addressing(_frame("GRP_DATA", b"\xa3")) == {}


def test_classes_that_address_nothing_stay_silent() -> None:
    """An advert names its node in the header; multipart and control say nothing at all."""
    for typename in ("ADVERT", "MULTIPART", "CONTROL", "UNK"):
        assert frame_addressing(_frame(typename, bytes(64))) == {}
    assert frame_addressing({"payload_typename": "TEXT_MSG"}) == {}  # no body at all
    assert frame_addressing({"pkt_payload": bytes(20)}) == {}        # no class at all


def test_rx_log_events_carry_their_addressing_into_the_observation() -> None:
    """The decode happens once, at the edge, so every reader downstream sees the same keys."""
    event = _Event(
        payload_typename="TEXT_MSG", pkt_payload=bytes.fromhex("3da1abcd") + bytes(16),
        path_len=1, path_hash_size=1, path="3d", snr=6.5,
    )
    obs = packet_observation_from_event(event)
    assert obs is not None and obs.raw is not None
    assert obs.raw["dest_hash"] == "3d" and obs.raw["src_hash"] == "a1"
    assert obs.node is None  # an addressed frame still names no *origin* node


def test_stored_frames_remember_what_they_addressed(tmp_path: Path) -> None:
    """A replayed row reads back exactly as the live frame did — same keys, same values.

    The feed opens seeded from history, so addressing that lived only in the raw payload
    of a live event would leave every row on the opening screen blank.
    """
    repo = Repository(tmp_path / "frames.db")
    run = repo.start_run("monitor", {}, None)
    frames = {
        "TEXT_MSG": {"dest_hash": "3d", "src_hash": "a1"},
        "ANON_REQ": {"dest_hash": "3d", "src_key": bytes(range(32)).hex()},
        "TRACE": {"trace_tag": "5f3c2a10"},
        "ACK": {"ack_crc": "9b71e004"},
        "GRP_DATA": {"chan_hash": "a3", "cipher_mac": "abcd", "crypted": "00" * 16},
    }
    for typename, decoded in frames.items():
        repo.record_observation(
            run,
            Observation(
                node=None, kind="packet", path="3d",
                raw={"payload_typename": typename, **decoded},
            ),
        )

    stored = repo.recent_observations(since=utcnow() - timedelta(hours=2))
    read_back = {(o.raw or {}).get("payload_typename"): (o.raw or {}) for o in stored}
    assert set(read_back) == set(frames)
    for typename, decoded in frames.items():
        for key, value in decoded.items():
            assert read_back[typename][key] == value, (typename, key)
    repo.close()


def test_a_trace_path_is_link_readings_not_relay_hashes() -> None:
    """The one class whose header path field means something else entirely.

    A trace grows its path by one signed SNR byte per hop, so reading it as hashes both
    invents adjacency and throws away the readings. Bytes here span the wire's signed
    range: +13.25 dB, then two negative legs.
    """
    payload = _frame("TRACE", bytes(9), path_len=3, path_hash_size=1, path="35eeef")
    assert trace_link_snrs(payload) == [13.25, -4.5, -4.25]


def test_only_a_trace_reads_its_path_that_way() -> None:
    """Every other class keeps hashes there, so nothing else may be decoded as readings."""
    for typename in ("TEXT_MSG", "ADVERT", "GRP_TXT", "ACK", "PATH"):
        assert trace_link_snrs(_frame(typename, bytes(9), path_len=1, path="35")) is None


def test_an_unrelayed_trace_has_readings_for_no_hops() -> None:
    """Nobody has forwarded it yet, so there is nothing to have measured it — not a fault."""
    assert trace_link_snrs(_frame("TRACE", bytes(9), path_len=0, path="")) == []


def test_a_trace_path_shorter_than_announced_is_not_invented() -> None:
    """Three hops promised, one byte delivered: the missing readings stay missing."""
    assert trace_link_snrs(_frame("TRACE", bytes(9), path_len=3, path="35")) is None


def test_a_traces_readings_reach_the_observation_and_its_hops_do_not() -> None:
    """The readings ride in the raw payload; ``path`` stays empty, having no hops to hold.

    Storing those bytes as a path fed the topology graph links that were never observed —
    whichever nodes happened to share the leading digits of an SNR reading.
    """
    obs = packet_observation_from_event(
        _Event(payload_typename="TRACE", pkt_payload=bytes.fromhex("5f3c2a10") + bytes(5),
               path_len=3, path_hash_size=1, path="35eeef", snr=13.75)
    )
    assert obs is not None and obs.raw is not None
    assert obs.path == ""
    assert obs.raw["trace_snrs"] == [13.25, -4.5, -4.25]
    assert obs.raw["trace_tag"] == "102a3c5f"


def test_a_frame_heard_straight_off_its_sender_is_kept() -> None:
    """Zero relays is the strongest adjacency evidence there is, not the absence of any.

    This is the whole of what a device sitting beside this one puts on the air: nothing
    has relayed it, so it names no repeater, and only an advert carries an origin key.
    Dropping it made a neighbour's every trace, message and ack invisible.
    """
    for typename in ("TRACE", "TEXT_MSG", "ACK", "REQ", "GRP_TXT"):
        obs = packet_observation_from_event(
            _Event(payload_typename=typename, pkt_payload=bytes(24),
                   path_len=0, path_hash_size=1, path="", snr=9.25, rssi=-61)
        )
        assert obs is not None, typename
        assert obs.path == "" and obs.snr == 9.25
        assert (obs.raw or {})["payload_typename"] == typename


def test_a_frame_with_no_class_at_all_is_still_dropped() -> None:
    """The library's sentinel for a frame too short to parse teaches nothing about anything."""
    assert packet_observation_from_event(
        _Event(payload_typename="UNK", pkt_payload=b"", path_len=0, path="")
    ) is None
    assert packet_observation_from_event(_Event(snr=6.0)) is None
