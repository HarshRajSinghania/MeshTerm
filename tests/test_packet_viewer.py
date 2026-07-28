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
    """A raw packet's parsed payload class headlines the card; the route is a row."""
    entry = _grp_txt_entry({"chan_hash": "ff", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(entry).render_body(80))
    assert "📻 CHANNEL TEXT" in body
    assert "flood" in body


def test_packet_viewer_class_row_leads_the_card() -> None:
    """The class headline is the first row — above heard/from — for every kind."""
    packet = _grp_txt_entry({"chan_hash": "ff", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(packet).render_body(80))
    assert body.index("CHANNEL TEXT") < body.index("heard")

    advert = PacketEntry(when=utcnow(), kind="advert", node="aa")
    body = _plain(_viewer(advert).render_body(80))
    assert "📢 ADVERT" in body
    assert body.index("ADVERT") < body.index("heard")


def test_packet_viewer_class_marks_an_unknown_typename() -> None:
    """A payload class this build has never heard of keeps the ❔ mark and its raw name."""
    entry = PacketEntry(when=utcnow(), kind="packet", raw={"payload_typename": "XYZZY"})
    body = _plain(_viewer(entry).render_body(80))
    assert "❔ XYZZY" in body


def test_packet_viewer_title_carries_no_emoji() -> None:
    """Dialog titles stay emoji-free (the standards' rule); the class row has the icon."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    assert _viewer(entry).title == "advert"


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


def test_packet_viewer_draws_a_relayed_packets_route_graph() -> None:
    """A packet that crossed relays gets THE route graph — braille edges, caption, legend."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="3d63,a1b2")
    body = _plain(_viewer(entry).render_body(80))
    assert "origin → you" in body                     # the graph caption
    assert "★ you" in body and "▲ repeater" in body   # the node-type legend
    assert any("⠀" <= ch <= "⣿" for ch in body)       # braille edges are drawn
    assert "3d" in body and "a1" in body              # each relay labelled by its hash byte


def test_packet_viewer_skips_the_graph_for_a_direct_packet() -> None:
    """A packet with no relays says so in the via row and draws no (pointless) two-node graph."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="")
    body = _plain(_viewer(entry).render_body(80))
    assert "direct — no relays" in body
    assert "origin → you" not in body
    assert not any("⠀" <= ch <= "⣿" for ch in body)


def test_packet_viewer_via_wraps_at_hop_boundaries_under_its_own_lane() -> None:
    """A long ``via`` chain folds between hops, hanging under the value lane — never mid-name."""
    names = {f"{i:02d}aa": f"Relay-Number-{i:02d}" for i in range(8)}
    entry = PacketEntry(when=utcnow(), kind="packet", path=",".join(names))
    lines = _stripped(
        PacketViewer([entry], 0, resolve=lambda h: names.get(h, "")).render_body(72)
    )
    via_at = next(i for i, line in enumerate(lines) if line.startswith("via"))
    end = next(i for i, line in enumerate(lines) if "reception describes" in line)
    folded = lines[via_at:end]
    assert len(folded) > 1  # a chain this long does not fit one lane
    assert all(line.startswith(" " * 11) for line in folded[1:])  # hangs past the label lane
    for name in names.values():
        assert any(name in line for line in folded)  # every hop survives the fold whole
    assert all(line.rstrip().endswith("→") for line in folded[:-1])  # the "goes on" cue
    assert all(len(line.rstrip()) <= 72 for line in folded)


def test_packet_viewer_from_row_leads_with_the_node_type_mark() -> None:
    """The sender wears its shared node glyph — typed by the packet, by the contacts, or ``○``."""
    from meshterm.core.models import NODE_TYPE_REPEATER

    def from_row(viewer: PacketViewer) -> str:
        return next(l for l in _stripped(viewer.render_body(80)) if l.startswith("from"))

    advertised = PacketEntry(
        when=utcnow(), kind="advert", node="3d63", name="YUL", node_type=NODE_TYPE_REPEATER
    )
    assert "▲ YUL" in from_row(_viewer(advertised))  # the type the packet itself carried

    bare = PacketEntry(when=utcnow(), kind="packet", node="3d63", name="YUL")
    assert "○ YUL" in from_row(_viewer(bare))  # nothing can type it: the unknown ring
    typed = PacketViewer(
        [bare], 0, resolve=lambda h: "", type_of=lambda h: NODE_TYPE_REPEATER
    )
    assert "▲ YUL" in from_row(typed)  # …until the contacts can

    mine = PacketEntry(when=utcnow(), kind="advert", node="3d63", name="Waymarker")
    ours = PacketViewer([mine], 0, resolve=lambda h: "", self_name="Waymarker")
    assert "★ Waymarker" in from_row(ours)  # our own node keeps the app-wide star


def test_packet_viewer_graph_names_a_known_origin_else_a_question_mark() -> None:
    """The graph's left endpoint is the resolved origin name, or ``?`` when the frame named none."""
    known = PacketEntry(when=utcnow(), kind="packet", node="c0ffee", path="3d63")
    body = _plain(
        PacketViewer([known], 0, resolve=lambda h: "Base" if h == "c0ffee" else "").render_body(80)
    )
    assert "Base" in body  # the origin, resolved to its contact name

    nameless = PacketEntry(when=utcnow(), kind="packet", path="3d63")
    body2 = _plain(_viewer(nameless).render_body(80))
    assert "?" in body2  # an origin-less flood draws a plain "?" endpoint
