"""What an overheard frame is *addressed to* — the packet body nobody else parses.

The companion's RX packet log hands every overheard frame to the meshcore library, which
splits off the header it always understands — route type, payload class, relay path — and
leaves the rest as an undecoded ``pkt_payload`` blob. The library then decodes that blob
for exactly two classes: an advert (the identity and location it carries) and a
channel-text frame (its channel fingerprint, MAC and ciphertext). Every other class — the
direct messages, requests, responses, path returns, acks and traces that make up half of
what a busy mesh puts on the air — arrives as bytes, which is why an origin-less flood
used to have nothing to say about itself beyond its class.

Those bytes are not opaque. MeshCore addresses a frame in the first few of them, and the
layout is fixed per payload class — as published in the protocol's own ``docs/payloads.md``
and ``docs/packet_format.md``:

===========================  ==========================================================
class                        body layout
===========================  ==========================================================
``REQ``/``RESPONSE``/        ``[dest hash:1][src hash:1][MAC:2][ciphertext]``
``TEXT_MSG``/``PATH``
``ANON_REQ``                 ``[dest hash:1][sender key:32][MAC:2][ciphertext]``
``ACK``                      ``[checksum:4]``
``TRACE``                    ``[tag:4][auth code:4][flags:1][hops…]``
``GRP_TXT``/``GRP_DATA``     ``[channel hash:1][MAC:2][ciphertext]``
===========================  ==========================================================

One class also breaks the rule the *header* follows. Every other frame's ``path`` field is
the list of relay hashes it has crossed; a ``TRACE``'s is a list of **signed SNR bytes**,
one per hop traversed — see :func:`trace_link_snrs`, which is what reads it, and which
exists because reading those bytes as hashes fabricated adjacency in the topology graph.

Endpoints are named by a *hash* — the leading byte of the node's public key, the same
one-byte identity the relay path's hops use — so they resolve through the app's ordinary
node resolver and land on the same names, hues and collisions as every hop does. An
anonymous request is the exception: having no shared secret to be recognised by yet, it
carries its sender's whole public key (the protocol docs' "sender's Ed25519 public key" —
the ``Packet.h`` header comment still calls it "ephemeral", but the login payloads it
carries are addressed to a node that has to know who logged in, and the docs' field table
is the later word). What is recovered here is only ever addressing: who a frame is for,
who it says it is from, which channel it belongs to, the token it carries. The ciphertext
is left alone (a channel we hold the key for is decrypted in
:mod:`~meshterm.core.channels`, by MAC-confirmed key, and nothing else on the mesh is ours
to read).

Two things are checked before a single byte is trusted, because a wrong slice would
produce not an error but a plausible-looking hash. The frame's **payload version** must be
the one these layouts describe — v1, the only version deployed, and the only one whose
hashes are one byte and whose MAC is two (the header reserves two bits for a v2 that
widens both). And the body must be **long enough** to hold the layout its class promises.
Either check failing yields nothing at all.

The recovered fields are merged straight into the frame's raw payload (see
:func:`~meshterm.core.connection.packet_observation_from_event`) under their own keys, so
every reader — the live feed's lane, the packet viewer's card, the repository's stored
columns — reads them exactly as it reads the two classes the library decoded itself.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Payload classes MeshCore addresses with a pair of one-byte key hashes: the recipient
#: first, the sender second, then the MAC and the ciphertext. A direct message, a request
#: to a repeater, its response, and a returned path all share this envelope.
ADDRESSED_CLASSES = frozenset({"REQ", "RESPONSE", "TEXT_MSG", "PATH"})

#: Payload classes carrying the ``[channel hash][MAC][ciphertext]`` channel envelope: a
#: channel's text messages and its datagrams. Only ``GRP_DATA`` is decoded here — the
#: library already breaks ``GRP_TXT`` out itself, into these very same field names — but
#: both are stored, decrypted and named by the same code from here on.
CHANNEL_CLASSES = frozenset({"GRP_TXT", "GRP_DATA"})

#: Bytes of a public key a frame names an endpoint by — one, the same one-byte identity a
#: relay-path hop carries, which is why an endpoint resolves through the ordinary node
#: resolver (and collides exactly as a hop does). Fixed by :data:`_PAYLOAD_V1`.
ENDPOINT_HASH_BYTES = 1

#: The payload version these layouts describe: 1-byte endpoint hashes and a 2-byte MAC.
#: The header's two version bits reserve a v2 that widens both, so a frame announcing any
#: other version is left undecoded rather than sliced by v1's offsets.
_PAYLOAD_V1 = 0

#: Bytes of the MAC that follows the addressing.
_MAC = 2
#: Bytes of a full public key, carried whole by an anonymous request's sender.
_KEY = 32
#: Bytes of a frame's own token — an ack's checksum, a trace's tag.
_TOKEN = 4


def frame_addressing(payload: Mapping[str, Any]) -> dict[str, str]:
    """Recover what a raw RX-logged frame addresses, from its undecoded body.

    Reads the leading bytes of ``pkt_payload`` according to the frame's payload class
    (see the module docstring's table), and returns them as hex under the raw-payload keys
    the rest of the app reads:

    * ``dest_hash`` — the recipient's one-byte key hash (every addressed class).
    * ``src_hash`` — the sender's one-byte key hash (the two-hash classes).
    * ``cipher_mac`` — the two-byte MAC that follows them: a tag over the encrypted
      message, and so a fingerprint identifying *which* message a frame carries without
      being able to read it.
    * ``src_key`` — the sender's *whole* public key, which an anonymous request carries
      instead of a hash (it has no shared secret to be recognised by yet).
    * ``ack_crc`` — an ack's four-byte checksum of the message it acknowledges, hex in
      wire order: the same form the companion reports its *own* delivery acks in, so the
      two can be compared.
    * ``trace_tag`` — a trace's tag, the token its reply is matched by. Read as the
      little-endian ``uint32`` the firmware writes and shown as eight hex digits, so it
      reads as the same number a trace reply's ``tag`` does rather than byte-reversed.
    * ``chan_hash``/``cipher_mac``/``crypted`` — a channel datagram's envelope, the same
      three fields the library breaks a channel *text* frame into.

    Hashes and keys are the bytes as they sit on the wire, hex-encoded — a hash reads
    exactly as the path hops beside it do.

    Args:
        payload: The frame's raw payload, as the RX-log event carried it (needs
            ``payload_typename`` and the undecoded ``pkt_payload`` bytes; ``payload_ver``
            is honoured when present).

    Returns:
        The recovered fields, or an empty mapping when the class carries no addressing,
        the body is missing, the frame announces a payload version these layouts don't
        describe, or the body is too short to hold the layout its class promises.
    """
    typename = payload.get("payload_typename")
    body = payload.get("pkt_payload")
    if not typename or not isinstance(body, (bytes, bytearray)):
        return {}
    version = payload.get("payload_ver")
    if version is not None and version != _PAYLOAD_V1:
        return {}  # a wider hash/MAC: v1's offsets would slice the wrong bytes
    body = bytes(body)

    hash_w = ENDPOINT_HASH_BYTES
    if typename in ADDRESSED_CLASSES:
        if len(body) < hash_w * 2 + _MAC:
            return {}
        return {
            "dest_hash": body[:hash_w].hex(),
            "src_hash": body[hash_w : hash_w * 2].hex(),
            # The MAC an addressed frame carries is a tag over *this* message's plaintext,
            # so two frames sharing one (between the same pair) are copies of the same
            # message — a fingerprint that needs no key to compare. It is what lets the
            # message-paths view group a direct message's own retransmissions and tell them
            # from the next message's, which content-matching does for a channel frame we
            # can decrypt (see :mod:`~meshterm.services.message_paths`).
            "cipher_mac": body[hash_w * 2 : hash_w * 2 + _MAC].hex(),
        }
    if typename == "ANON_REQ":
        if len(body) < hash_w + _KEY + _MAC:
            return {}
        return {
            "dest_hash": body[:hash_w].hex(),
            "src_key": body[hash_w : hash_w + _KEY].hex(),
        }
    if typename == "ACK":
        if len(body) < _TOKEN:
            return {}
        return {"ack_crc": body[:_TOKEN].hex()}
    if typename == "TRACE":
        # tag, auth code, flags — the shortest trace is one that collected no hops yet.
        if len(body) < _TOKEN * 2 + 1:
            return {}
        # The firmware memcpy's the tag straight out of a uint32, and the library reads a
        # trace reply's back the same way: little-endian, so the two agree on the number.
        return {"trace_tag": f"{int.from_bytes(body[:_TOKEN], 'little'):08x}"}
    if typename == "GRP_DATA":  # GRP_TXT's twin, which the library leaves undecoded
        if len(body) < hash_w + _MAC:
            return {}
        return {
            "chan_hash": body[:hash_w].hex(),
            "cipher_mac": body[hash_w : hash_w + _MAC].hex(),
            "crypted": body[hash_w + _MAC :].hex(),
        }
    return {}


def trace_link_snrs(payload: Mapping[str, Any]) -> list[float] | None:
    """Recover the per-hop link readings a ``TRACE`` frame collected, from its path field.

    A trace is the one class whose header ``path`` field does not hold relay hashes. The
    firmware grows it by **one signed SNR byte per hop the packet traverses** — the
    reading the relaying node heard its predecessor at — which is how a trace reply can
    report a whole route's link quality without a second field, and what the meshcore
    parser's ``# Beware of traces where pathes are mixed`` warns about. Read as hashes it
    is not merely useless but actively false: the bytes resolve to whichever nodes happen
    to share those leading digits and enter the topology graph as adjacency that was never
    observed.

    The distinction is measurable, not theoretical. Across a captured population of 1,604
    traces (2,221 path entries), every entry read as a signed byte over four lands inside
    the LoRa SNR band, spanning −10.5 to +15.0 dB — where relay hashes, being uniform over
    the byte, would put roughly five in six outside it.

    Args:
        payload: The frame's raw payload as the RX-log event carried it (needs
            ``payload_typename``, ``path_len`` and the hex ``path``).

    Returns:
        One SNR in dB per traversed hop, in the order the packet walked them, or ``None``
        when the frame is not a trace or its path field is unreadable at the announced
        length. A trace nobody has relayed yet correctly yields an empty list.
    """
    if payload.get("payload_typename") != "TRACE":
        return None
    try:
        hop_count = int(payload.get("path_len") or 0)
    except (TypeError, ValueError):
        return None
    if hop_count < 0:
        return None
    raw = str(payload.get("path") or "").lower().removeprefix("0x")
    try:
        readings = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(readings) < hop_count:
        return None  # short of what it announced: the tail would be invented, not read
    return [_snr_db(b) for b in readings[:hop_count]]


def _snr_db(byte: int) -> float:
    """Decode one wire SNR byte: a signed value in quarter-decibels."""
    return (byte - 256 if byte >= 128 else byte) / 4.0
