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


def test_packet_viewer_reaches_packets_that_arrive_after_open() -> None:
    """A live source lets the viewer page up into packets that arrive after it opens."""
    old = PacketEntry(when=utcnow(), kind="advert", node="aa")
    feed = [old]
    viewer = PacketViewer(list(feed), 0, resolve=lambda h: "", source=lambda: list(feed))
    assert viewer._entries[viewer._index] is old

    # A newer packet is prepended (the feed is newest-first) while the dialog sits on the
    # old one; a repaint folds it in and the view stays put on `old` (now at index 1).
    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")
    feed.insert(0, newer)
    viewer.render_body(80)
    assert viewer._entries[viewer._index] is old
    assert "2/2" in viewer.title

    # ↑ (newer) now reaches the packet that arrived after the dialog opened.
    viewer.handle("up")
    assert viewer._entries[viewer._index] is newer
    assert "1/2" in viewer.title


def test_packet_viewer_heard_row_says_now_not_now_ago() -> None:
    """A just-heard packet's heard row reads "(now)" — the format_ago grammar."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    body = _plain(_viewer(entry).render_body(80))
    assert "(now)" in body
    assert "now ago" not in body


def test_packet_viewer_without_a_source_stays_a_snapshot() -> None:
    """With no live source the viewer is frozen on its opening list (unchanged behaviour)."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    viewer = PacketViewer([entry], 0, resolve=lambda h: "")
    viewer.render_body(80)
    assert viewer._entries == [entry]
    assert viewer.footer_hint == "Esc close"


def test_packet_viewer_grows_its_dialog_only() -> None:
    """The viewer opts into a grow-only dialog so paging enlarges but never shrinks it."""
    tiny = PacketEntry(when=utcnow(), kind="ack", where="01c3")
    viewer = PacketViewer([tiny], 0, resolve=lambda h: "")
    assert viewer.grow_only is True
    # The body is left at its natural (unpadded) height; the frame, not a floor, pads it.
    assert len(viewer.render_body(80)) < 10
    # ratchet_viewport is the grow-only contract: it rises to a taller body and never drops.
    assert viewer.ratchet_viewport(6) == 6
    assert viewer.ratchet_viewport(14) == 14  # a taller packet enlarges the box
    assert viewer.ratchet_viewport(4) == 14   # a shorter one after keeps the larger box


def test_packet_viewer_raw_dump_skips_fields_folded_into_flavoured_rows() -> None:
    """Fields already shown as class/route/via/channel rows don't also dump generically."""
    entry = _grp_txt_entry({
        "chan_hash": "ab", "cipher_mac": "0000", "crypted": "00" * 16,
        "path_len": 1, "path_hash_size": 1, "header": 5, "novel_field": "surprise",
    })
    body = _plain(_viewer(entry).render_body(80))
    assert "path_len" not in body
    assert "header" not in body
    assert "novel_field" in body  # an unrecognized field surfaces with its full name


def test_packet_viewer_shows_full_raw_field_labels() -> None:
    """A long raw-field name is shown whole — the label lane widens rather than clipping it."""
    entry = _grp_txt_entry({
        "chan_hash": "ab", "cipher_mac": "0000", "crypted": "00" * 16,
        "battery_millivolts": 4102,
    })
    body = _plain(_viewer(entry).render_body(80))
    assert "battery_millivolts" in body  # the 18-char key is not clipped to 8
