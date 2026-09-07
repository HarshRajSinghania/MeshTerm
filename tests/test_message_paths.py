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
    collapse,
    direct_arrivals,
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
    _record_frame(
        repo, run, when=now - timedelta(seconds=5), path="3d63", raw=_grp_txt_raw(SECRET, wire)
    )
    _record_frame(
        repo,
        run,
        when=now - timedelta(seconds=3),
        path="3d63,a1b2",
        raw=_grp_txt_raw(SECRET, wire),
    )
    _record_frame(
        repo,
        run,
        when=now - timedelta(seconds=1),
        path="",
        raw=_grp_txt_raw(SECRET, "Alice: other"),
    )

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
        repo,
        run,
        when=now + timedelta(seconds=2),
        path="3d63",
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
        repo,
        run,
        when=now + timedelta(seconds=9),
        path="",
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
        repo,
        run,
        when=now - timedelta(hours=2),
        path="3d63",
        raw=_grp_txt_raw(SECRET, "Alice: hi"),
    )
    message = ChatMessage(text="Alice: hi", is_channel=True, created_at=now)
    assert channel_arrivals(repo, message, channel_name="#general", secret=SECRET) == []
    repo.close()


def _direct_raw(dest: str = "", src: str = "", mac: str = "", route: str = "") -> dict:
    """A direct-message frame's raw payload, spelled as the meshcore library reports it."""
    raw: dict = {"payload_typename": "TEXT_MSG"}
    if dest:
        raw["dest_hash"] = dest
    if src:
        raw["src_hash"] = src
    if mac:
        raw["cipher_mac"] = mac
    if route:
        raw["route_typename"] = route
    return raw


def test_direct_frames_without_a_mac_fall_back_to_address_and_time(tmp_path: Path) -> None:
    """History recorded before the MAC was kept still correlates — and says that it did.

    The fallback is honest evidence, not a claim: a frame between the right pair in the
    right window is only *probably* this message, and ``exact`` comes back ``False`` so the
    view can bill it that way.
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=3),
        path="3d63",
        raw=_direct_raw(dest="d4", src="a1"),
    )
    _record_frame(  # a channel frame in the window is not direct-message evidence
        repo,
        run,
        when=now + timedelta(seconds=4),
        path="",
        raw=_grp_txt_raw(SECRET, "Alice: hi"),
    )
    _record_frame(  # a direct frame far outside the window doesn't correlate
        repo,
        run,
        when=now + timedelta(minutes=10),
        path="",
        raw=_direct_raw(dest="d4", src="a1"),
    )
    message = ChatMessage(text="see you at 8", outbound=True, peer="d4e5", created_at=now)
    arrivals, exact = direct_arrivals(repo, message, self_key="a1" + "0" * 62, peer_key="d4e5")
    assert [a.hops for a in arrivals] == [("3d63",)]
    assert exact is False
    repo.close()


def test_direct_frames_keep_only_the_direction_the_message_travelled(tmp_path: Path) -> None:
    """A send shows our outgoing frames; a received message shows the incoming ones.

    Both ends' hashes sit on every frame of a conversation whichever way it went, so
    matching on the pair alone put our own sends into a received message's view. That is
    where the implausible one-hop rows came from (JP, 2026-09-02): our transmissions heard
    coming back off the repeaters in earshot, correctly one hop, shown as if they were an
    inbound route to us.
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    ours = dict(dest="d4", src="a1", mac="beef")  # us -> peer
    theirs = dict(dest="a1", src="d4", mac="f00d")  # peer -> us
    _record_frame(repo, run, when=now + timedelta(seconds=1), path="3d63", raw=_direct_raw(**ours))
    _record_frame(repo, run, when=now + timedelta(seconds=2), path="c0", raw=_direct_raw(**theirs))
    # Someone else's traffic, overheard in the same window.
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=3),
        path="3d63",
        raw=_direct_raw(dest="7f", src="c0", mac="dead"),
    )
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=4),
        path="",
        raw=_direct_raw(dest="a1", src="7f", mac="cafe"),
    )

    sent = ChatMessage(text="see you at 8", outbound=True, peer="d4e5f6a7", created_at=now)
    got = ChatMessage(text="ok", outbound=False, peer="d4e5f6a7", created_at=now)
    keys = dict(self_key="a1" + "0" * 62, peer_key="d4e5f6a7")

    assert [a.hops for a in direct_arrivals(repo, sent, **keys)[0]] == [("3d63",)]
    assert [a.hops for a in direct_arrivals(repo, got, **keys)[0]] == [("c0",)]
    repo.close()


def test_a_shared_mac_separates_one_message_from_the_next(tmp_path: Path) -> None:
    """Two sends seconds apart keep their own frames — the MAC is the fingerprint.

    This is the direct-message counterpart of the channel view's content match: the frames
    cannot be read, but copies of one message carry the same MAC over its ciphertext and the
    next message's carry a different one.
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    # First send, retried twice and heard off two repeaters.
    for offset, path in ((1, "3d63"), (1, "27d4"), (6, "3d63"), (6, "27d4")):
        _record_frame(
            repo,
            run,
            when=now + timedelta(seconds=offset),
            path=path,
            raw=_direct_raw(dest="d4", src="a1", mac="beef"),
        )
    # Second send, twelve seconds later, same pair and same paths.
    for offset, path in ((13, "3d63"), (13, "27d4")):
        _record_frame(
            repo,
            run,
            when=now + timedelta(seconds=offset),
            path=path,
            raw=_direct_raw(dest="d4", src="a1", mac="f00d"),
        )

    keys = dict(self_key="a1" + "0" * 62, peer_key="d4e5f6a7")
    first = ChatMessage(text="one", outbound=True, peer="d4e5f6a7", created_at=now)
    second = ChatMessage(
        text="two",
        outbound=True,
        peer="d4e5f6a7",
        created_at=now + timedelta(seconds=12),
    )
    got, exact = direct_arrivals(repo, first, **keys)
    assert exact is True
    assert len(got) == 4, "the first send's four frames, and not the second's"
    assert len(direct_arrivals(repo, second, **keys)[0]) == 2
    repo.close()


def test_the_window_clamps_to_the_messages_going_the_same_way(tmp_path: Path) -> None:
    """Each direction is bounded by its own neighbours, and bounded differently.

    Without a clamp a fast exchange put every message's frames in every other's view — nine
    messages of one real conversation fell inside a single flat window. With the wrong
    clamp, a *received* message got an empty one: its stamp is the sender's clock, so
    ordering it against our own sends compares two clocks and can bound it by an edge that,
    in its own clock, hasn't happened yet (JP, 2026-09-02).
    """
    from meshterm.services.message_paths import _DIRECT_WINDOW, direct_window

    repo, run = _repo(tmp_path)
    now = utcnow()
    sent = [now, now + timedelta(seconds=20)]
    received = [now - timedelta(seconds=30), now + timedelta(seconds=50)]
    for at in sent:
        repo.record_chat_message(
            ChatMessage(text="ours", outbound=True, peer="d4e5f6a7", created_at=at)
        )
    for at in received:
        repo.record_chat_message(
            ChatMessage(text="theirs", outbound=False, peer="d4e5f6a7", created_at=at)
        )

    # Our own send: our clock, so nothing of it predates it — the window opens at the
    # message and runs forward to the next send, where retries actually live.
    ours = ChatMessage(text="ours", outbound=True, peer="d4e5f6a7", created_at=now)
    start, end = direct_window(repo, ours)
    assert (now - start) < timedelta(seconds=5), "no room behind a send"
    assert end == sent[1], "forward to the next send, not to the next received message"

    # A received message keeps a centred window, reaching halfway to the received messages
    # either side of it — and is untouched by our own sends in between.
    theirs = ChatMessage(
        text="theirs", outbound=False, peer="d4e5f6a7", created_at=now + timedelta(seconds=50)
    )
    start, end = direct_window(repo, theirs)
    assert start == theirs.created_at - timedelta(seconds=40), "halfway back to the last one"
    assert end == theirs.created_at + _DIRECT_WINDOW, "nothing follows, so that side stands"
    repo.close()


def test_collapse_folds_a_repeated_path_into_one_counted_row(tmp_path: Path) -> None:
    """One row per path, carrying its copy count, first sighting and best SNR."""
    from meshterm.services.message_paths import Arrival

    base = utcnow()
    arrivals = [
        Arrival(when=base, hops=("3d63",), snr=10.0),
        Arrival(when=base + timedelta(seconds=1), hops=("27d4",), snr=5.0),
        Arrival(when=base + timedelta(seconds=5), hops=("3d63",), snr=13.5),
    ]
    folded = collapse(arrivals)
    assert [a.hops for a in folded] == [("3d63",), ("27d4",)], "first-heard order"
    assert folded[0].copies == 2 and folded[1].copies == 1
    assert folded[0].when == base, "the first sighting is when the path first worked"
    assert folded[0].snr == 13.5, "the best reading is what the path can do"


def test_a_routed_frames_path_is_where_it_was_going_not_where_it_has_been(
    tmp_path: Path,
) -> None:
    """The route type decides what ``path`` means, and an empty routed one means nothing.

    A flooded packet accumulates its path — every relay appends itself — so the hops are the
    route it travelled to us. A direct-routed packet carries a route its sender wrote and
    its relays consume, so an empty path means the route was used up, not that the packet
    crossed no relays. Read the second as the first and a message from five hops away is
    drawn as having arrived out of thin air: the "impossible direct path" (JP, 2026-09-02).
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(  # flooded, one relay: it really did come to us that way
        repo,
        run,
        when=now + timedelta(seconds=1),
        path="3d63",
        raw=_direct_raw(dest="a1", src="d4", mac="beef", route="FLOOD"),
    )
    _record_frame(  # direct-routed, route consumed: says nothing about how it got here
        repo,
        run,
        when=now + timedelta(seconds=2),
        path="",
        raw=_direct_raw(dest="a1", src="d4", mac="beef", route="DIRECT"),
    )
    message = ChatMessage(text="hi", outbound=False, peer="d4e5f6a7", created_at=now)
    arrivals, _exact = direct_arrivals(repo, message, self_key="a1" + "0" * 62, peer_key="d4e5f6a7")
    flooded, routed = arrivals
    assert flooded.routed is False and flooded.route_known is True
    assert routed.routed is True and routed.route_known is False, (
        "an empty routed path is not a zero-hop arrival"
    )
    repo.close()


def test_history_without_a_route_type_is_unknown_rather_than_assumed(tmp_path: Path) -> None:
    """Frames recorded before the route type was kept read as before, not as guesses."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=1),
        path="",
        raw=_direct_raw(dest="a1", src="d4", mac="beef"),
    )
    message = ChatMessage(text="hi", outbound=False, peer="d4e5f6a7", created_at=now)
    arrivals, _exact = direct_arrivals(repo, message, self_key="a1" + "0" * 62, peer_key="d4e5f6a7")
    assert arrivals[0].routed is None
    assert arrivals[0].route_known is True, "unknown falls back to the old reading"
    repo.close()


def test_collapse_keeps_a_routed_path_apart_from_the_same_hops_flooded(tmp_path: Path) -> None:
    """Identical hashes mean two different things under the two route types."""
    from meshterm.services.message_paths import Arrival

    base = utcnow()
    folded = collapse(
        [
            Arrival(when=base, hops=("3d63",), snr=1.0, routed=False),
            Arrival(when=base + timedelta(seconds=1), hops=("3d63",), snr=2.0, routed=True),
        ]
    )
    assert len(folded) == 2, "a route travelled is not a route intended"
    assert [a.routed for a in folded] == [False, True]
