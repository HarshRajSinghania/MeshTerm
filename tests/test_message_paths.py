"""Message-paths tests: matching a chat message back to its logged arrivals.

Channel arrivals are matched by decrypting overheard GRP_TXT frames with the channel's
own key (firmware-shaped frames are built here exactly as the packet-viewer tests build
theirs); direct arrivals are matched by time alone. Everything runs against a real
temporary repository, so the window query is exercised too.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256

from meshterm.core.channels import channel_hash, derive_secret
from meshterm.core.models import ChatMessage, Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.message_paths import (
    channel_arrivals,
    direct_frames_near,
    distinct_paths,
)

SECRET = derive_secret("#general")


def _grp_txt_raw(secret: bytes, text: str, *, attempt: int = 0) -> dict:
    """A firmware-shaped GRP_TXT packet payload for ``text``, encrypted under ``secret``."""
    plain = (0).to_bytes(4, "little") + bytes([attempt]) + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    return {
        "payload_typename": "GRP_TXT",
        "chan_hash": channel_hash(secret),
        "cipher_mac": mac.digest()[:2].hex(),
        "crypted": crypted.hex(),
    }


def _repo(tmp_path: Path) -> tuple[Repository, int]:
    repo = Repository(tmp_path / "test.db")
    run_id = repo.start_run("monitor", {}, None)
    return repo, run_id


def _record_frame(repo: Repository, run_id: int, *, when, path: str, raw: dict, snr=2.0):
    repo.record_observation(
        run_id,
        Observation(node=None, kind="packet", path=path, snr=snr, observed_at=when, raw=raw),
    )


def test_channel_arrivals_match_by_decrypted_content(tmp_path: Path) -> None:
    """Every overheard copy of a channel message surfaces, each with its own path."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    wire = "Alice: hi mesh"
    _record_frame(repo, run, when=now - timedelta(seconds=5), path="3d63", raw=_grp_txt_raw(SECRET, wire))
    _record_frame(repo, run, when=now - timedelta(seconds=3), path="3d63,a1b2", raw=_grp_txt_raw(SECRET, wire))
    _record_frame(repo, run, when=now - timedelta(seconds=1), path="", raw=_grp_txt_raw(SECRET, "Alice: other"))

    message = ChatMessage(text=wire, is_channel=True, created_at=now)
    arrivals = channel_arrivals(repo, message, channel_name="#general", secret=SECRET)
    assert [a.hops for a in arrivals] == [("3d63",), ("3d63", "a1b2")]
    assert distinct_paths(arrivals) == 2
    repo.close()


def test_channel_arrivals_tolerate_the_sender_prefix_on_the_wire(tmp_path: Path) -> None:
    """Our own outbound message (stored as typed) matches its prefixed on-air copies."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(
        repo, run, when=now + timedelta(seconds=2), path="3d63",
        raw=_grp_txt_raw(SECRET, "Homestead: on my way"),
    )
    message = ChatMessage(text="on my way", outbound=True, is_channel=True, created_at=now)
    arrivals = channel_arrivals(repo, message, channel_name="#general", secret=SECRET)
    assert len(arrivals) == 1 and arrivals[0].hops == ("3d63",)
    repo.close()


def test_channel_arrivals_carry_the_resend_counter(tmp_path: Path) -> None:
    """A decrypted frame's resend counter rides along, telling copies from retries."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(repo, run, when=now, path="", raw=_grp_txt_raw(SECRET, "Alice: hi", attempt=0))
    _record_frame(
        repo, run, when=now + timedelta(seconds=9), path="",
        raw=_grp_txt_raw(SECRET, "Alice: hi", attempt=1),
    )
    message = ChatMessage(text="Alice: hi", is_channel=True, created_at=now)
    arrivals = channel_arrivals(repo, message, channel_name="#general", secret=SECRET)
    assert [a.resend for a in arrivals] == [0, 1]
    assert distinct_paths(arrivals) == 1  # both arrived direct
    repo.close()


def test_channel_arrivals_ignore_frames_outside_the_window(tmp_path: Path) -> None:
    """A matching frame far outside the message's window is someone else's message."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(
        repo, run, when=now - timedelta(hours=2), path="3d63",
        raw=_grp_txt_raw(SECRET, "Alice: hi"),
    )
    message = ChatMessage(text="Alice: hi", is_channel=True, created_at=now)
    assert channel_arrivals(repo, message, channel_name="#general", secret=SECRET) == []
    repo.close()


def _direct_raw(dest: str = "", src: str = "") -> dict:
    """A direct-message frame's raw payload, spelled as the meshcore library reports it."""
    raw: dict = {"payload_typename": "TEXT_MSG"}
    if dest:
        raw["dest_hash"] = dest
    if src:
        raw["src_hash"] = src
    return raw


def test_direct_frames_match_by_time_only(tmp_path: Path) -> None:
    """With neither end named, every direct frame in the tight window is the evidence."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(repo, run, when=now + timedelta(seconds=3), path="3d63", raw=_direct_raw())
    _record_frame(  # a channel frame in the window is not direct-message evidence
        repo, run, when=now + timedelta(seconds=4), path="",
        raw=_grp_txt_raw(SECRET, "Alice: hi"),
    )
    _record_frame(  # a direct frame far outside the tight window doesn't correlate
        repo, run, when=now + timedelta(minutes=10), path="", raw=_direct_raw(),
    )
    message = ChatMessage(text="see you at 8", peer="d4e5", created_at=now)
    arrivals = direct_frames_near(repo, message)
    assert len(arrivals) == 1 and arrivals[0].hops == ("3d63",)
    repo.close()


def test_direct_frames_narrow_to_the_conversation_ends(tmp_path: Path) -> None:
    """Naming both ends keeps only the frames that ran between them, either direction."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    # Us (a1…) and the peer (d4…), each way round: both are this conversation's traffic.
    _record_frame(
        repo, run, when=now + timedelta(seconds=1), path="3d63",
        raw=_direct_raw(dest="d4", src="a1"),
    )
    _record_frame(
        repo, run, when=now + timedelta(seconds=2), path="c0",
        raw=_direct_raw(dest="a1", src="d4"),
    )
    # Someone else's direct message, overheard in the same window.
    _record_frame(
        repo, run, when=now + timedelta(seconds=3), path="3d63",
        raw=_direct_raw(dest="7f", src="c0"),
    )
    # Addressed to us, but by a third party — not this conversation.
    _record_frame(
        repo, run, when=now + timedelta(seconds=4), path="",
        raw=_direct_raw(dest="a1", src="7f"),
    )
    # Its addressing was never recovered, so it can't be shown to belong.
    _record_frame(repo, run, when=now + timedelta(seconds=5), path="", raw=_direct_raw())

    message = ChatMessage(text="see you at 8", peer="d4e5f6a7", created_at=now)
    arrivals = direct_frames_near(
        repo, message, ends=("d4e5f6a7", "a1" + "0" * 62),
    )
    assert [a.hops for a in arrivals] == [("3d63",), ("c0",)]
    repo.close()
