"""Packet viewer tests: per-kind flavouring, the raw-field dedup, and channel decrypt.

The viewer is pure Rich-in, ANSI-lines-out (``render_body``), so it's driven headless
against a synthetic :class:`PacketEntry` — the same approach as the dashboard tests.
"""

from __future__ import annotations

import re

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256

from meshterm.core.channels import channel_hash, derive_secret
from meshterm.core.models import utcnow
from meshterm.ui.packet_viewer import PacketEntry, PacketViewer


def _stripped(lines: list[str]) -> list[str]:
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _plain(lines: list[str]) -> str:
    return "\n".join(_stripped(lines))


def _grp_txt_entry(raw_extra: dict) -> PacketEntry:
    raw = {"payload_typename": "GRP_TXT", "route_typename": "FLOOD", **raw_extra}
    return PacketEntry(when=utcnow(), kind="packet", path="a1b2c3d4e5f6", raw=raw)


def _viewer(entry: PacketEntry, channels=()) -> PacketViewer:
    return PacketViewer([entry], 0, resolve=lambda h: "", channels=channels)


def _grp_txt_frame(secret: bytes, text: str) -> tuple[str, str, str]:
    """A firmware-shaped GRP_TXT frame for ``text``, encrypted under ``secret``."""
    plain = (0).to_bytes(4, "little") + bytes([0]) + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    return channel_hash(secret), mac.digest()[:2].hex(), crypted.hex()


def test_packet_viewer_shows_class_and_route() -> None:
    """A raw packet's parsed payload class and route surface as labelled rows."""
    entry = _grp_txt_entry({"chan_hash": "ff", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(entry).render_body(80))
    assert "channel text" in body
    assert "flood" in body


def test_packet_viewer_decrypts_a_known_channel() -> None:
    """A channel-text frame we hold the key for decrypts to its plaintext."""
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(secret, "hi mesh")
    entry = _grp_txt_entry({"chan_hash": chash, "cipher_mac": mac, "crypted": crypted})
    body = _plain(_viewer(entry, channels=[(name, secret)]).render_body(80))
    assert "#general" in body
    assert "hi mesh" in body


def test_packet_viewer_reports_an_unknown_channel() -> None:
    """A frame from a channel we don't hold the key for says so instead of guessing."""
    entry = _grp_txt_entry({"chan_hash": "ab", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(entry).render_body(80))
    assert "unknown" in body and "can't decrypt" in body


def test_packet_viewer_raw_dump_skips_fields_folded_into_flavoured_rows() -> None:
    """Fields already shown as class/route/via/channel rows don't also dump generically."""
    entry = _grp_txt_entry({
        "chan_hash": "ab", "cipher_mac": "0000", "crypted": "00" * 16,
        "path_len": 1, "path_hash_size": 1, "header": 5, "novel_field": "surprise",
    })
    body = _plain(_viewer(entry).render_body(80))
    assert "path_len" not in body
    assert "header" not in body
    assert "novel_fi" in body  # an unrecognized field still surfaces (label truncates to 8)
