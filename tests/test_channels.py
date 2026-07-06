"""Tests for the channel-management feature: pure logic, QR rendering, and the tool.

The logic (secret derivation, share-URL round-tripping) is hardware-free; the tool actions
run against the :class:`MockDevice` simulator and a temporary database.
"""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

import pytest
from rich.console import Console

from meshtools.context import AppContext
from meshtools.core.admin_store import AdminStore
from meshtools.core.channels import (
    CHANNEL_SECRET_BYTES,
    channel_hash,
    derive_secret,
    full_channel_hash,
    normalize_secret,
    parse_share_url,
    random_secret,
    share_url,
)
from meshtools.core.config import Settings
from meshtools.core.device_store import DeviceStore
from meshtools.persistence.repository import Repository
from meshtools.tools.channels import ChannelsTool
from meshtools.ui.channels import ChannelSlot, _apply_order, _next_free_slot, _read_slots
from meshtools.ui.qr import qr_text

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# -- pure channel logic -------------------------------------------------------


def test_derive_secret_matches_firmware_formula() -> None:
    """A derived key is sha256(name)[:16] — the firmware's public-channel scheme."""
    assert derive_secret("#public") == sha256(b"#public").digest()[:16]
    assert len(derive_secret("#anything")) == CHANNEL_SECRET_BYTES
    assert derive_secret("#a") == derive_secret("#a")  # deterministic


def test_random_secret_is_16_unique_bytes() -> None:
    """A random private key is 16 bytes and (practically) never repeats."""
    a, b = random_secret(), random_secret()
    assert len(a) == CHANNEL_SECRET_BYTES and len(b) == CHANNEL_SECRET_BYTES
    assert a != b


def test_normalize_secret_accepts_common_shapes() -> None:
    """A pasted key is accepted with 0x, spaces, or colons; length is enforced."""
    raw = "000102030405060708090a0b0c0d0e0f"
    expected = bytes(range(16))
    assert normalize_secret(raw) == expected
    assert normalize_secret("0x" + raw) == expected
    assert normalize_secret("0001 0203 0405 0607 0809 0a0b 0c0d 0e0f") == expected
    assert normalize_secret("00:01:02:03:04:05:06:07:08:09:0a:0b:0c:0d:0e:0f") == expected


@pytest.mark.parametrize("bad", ["", "abcd", "zz" * 16, "00" * 15, "00" * 17])
def test_normalize_secret_rejects_bad_keys(bad: str) -> None:
    """Non-hex or wrong-length keys raise, so the prompt can re-ask."""
    with pytest.raises(ValueError):
        normalize_secret(bad)


def test_channel_hash_is_two_hex_chars() -> None:
    """The channel hash is the leading byte of sha256(secret), as the companion reports."""
    secret = bytes(range(16))
    assert channel_hash(secret) == sha256(secret).hexdigest()[:2]
    assert len(channel_hash(secret)) == 2


def test_full_channel_hash_is_the_whole_digest_led_by_the_short_hash() -> None:
    """The full hash is the complete sha256(secret) digest, opening with the short hash."""
    secret = bytes(range(16))
    full = full_channel_hash(secret)
    assert full == sha256(secret).hexdigest()
    assert len(full) == 64
    assert full[:2] == channel_hash(secret)  # the highlighted first byte


def test_share_url_round_trips_with_encoding() -> None:
    """A share URL builds and parses back to the same name and secret, encoding included."""
    name, secret = "Ops Team #1", bytes(range(16))
    url = share_url(name, secret)
    assert url.startswith("meshcore://channel/add?")
    assert "Ops%20Team" in url  # the space is percent-encoded
    parsed = parse_share_url(url)
    assert parsed == (name, secret)


def test_parse_share_url_rejects_non_channel_links() -> None:
    """Anything that isn't a valid channel-add link parses to None."""
    assert parse_share_url("https://example.com") is None
    assert parse_share_url("meshcore://contact/add?name=x&public_key=ab") is None
    assert parse_share_url("meshcore://channel/add?name=x") is None  # missing secret
    assert parse_share_url("meshcore://channel/add?name=x&secret=nothex") is None
    assert parse_share_url("meshcore://channel/add?secret=" + "00" * 16) is None  # no name


# -- QR rendering -------------------------------------------------------------


def test_qr_text_renders_square_with_quiet_zone() -> None:
    """qr_text produces block rows framed by a light quiet zone, wider than the data."""
    console = Console(force_terminal=True, width=120, file=__import__("io").StringIO())
    console.print(qr_text("meshcore://channel/add?name=Test&secret=" + "ab" * 16))
    plain = _ANSI.sub("", console.file.getvalue()).rstrip("\n")
    lines = plain.splitlines()
    assert lines, "QR produced no output"
    # A version-appropriate QR for this URL is at least ~25 modules wide plus an 8-module
    # quiet zone; the finder pattern makes the code non-trivial.
    assert len(lines[0]) >= 30
    assert any("█" in line for line in lines)  # dark modules were drawn


# -- channel slot model -------------------------------------------------------


def test_channel_slot_classifies_public_and_private() -> None:
    """A #-named or name-derived slot reads as public; a random-key slot as private."""
    public = ChannelSlot(idx=0, name="#public", secret=derive_secret("#public"))
    assert public.is_public
    derived = ChannelSlot(idx=1, name="general", secret=derive_secret("general"))
    assert derived.is_public  # key derived from the name, even without a leading #
    private = ChannelSlot(idx=2, name="Ops", secret=bytes(range(16)))
    assert not private.is_public
    assert private.conversation.is_channel and private.conversation.channel_idx == 2
    assert private.conversation.label == "Ops"  # raw name, no forced leading '#'


def test_next_free_slot_finds_gaps_and_full() -> None:
    """The next free slot skips used indices and is None when every slot is taken."""
    slots = [ChannelSlot(idx=0, name="a", secret=b"\x00" * 16),
             ChannelSlot(idx=2, name="c", secret=b"\x00" * 16)]
    assert _next_free_slot(slots) == 1
    full = [ChannelSlot(idx=i, name=str(i), secret=b"\x00" * 16) for i in range(8)]
    assert _next_free_slot(full) is None


async def test_apply_order_relays_channels_into_new_positions(ctx: AppContext) -> None:
    """Applying a new order re-lays channels across the same slots in display order."""
    device = await ctx.device()
    await device.set_channel(0, "Alpha", bytes(range(16)))  # private
    await device.set_channel(1, "#beta", None)  # public, key derived from the name
    await device.set_channel(2, "Gamma", bytes(range(16, 32)))  # private

    slots = await _read_slots(device)
    # Reverse the display order: the row that was third moves first, first moves last.
    writes = await _apply_order(device, slots, [2, 1, 0])
    assert writes == 2  # the middle channel keeps its slot; the two ends swap

    after = {s.idx: s for s in await _read_slots(device)}
    assert after[0].name == "Gamma" and after[0].secret == bytes(range(16, 32))
    assert after[1].name == "#beta" and after[1].is_public  # public key re-derived in place
    assert after[2].name == "Alpha" and after[2].secret == bytes(range(16))


# -- the tool against the simulator -------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context with the plain (console) UI surface."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "chan.db")
    context = AppContext(
        console=Console(file=__import__("io").StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


async def test_cli_add_private_generates_key_and_lists(ctx: AppContext) -> None:
    """`add` with no secret creates a private channel with a random key; `list` sees it."""
    tool = ChannelsTool()
    result = await tool.run(ctx, {"cli_action": "add", "index": 1, "name": "Ops", "secret": None})
    assert result.summary == {"index": 1, "name": "Ops"}

    slots = await _read_slots(await ctx.device())
    slot = next(s for s in slots if s.idx == 1)
    assert slot.name == "Ops"
    assert not slot.is_public  # a random key, not derived from the name


async def test_cli_add_public_derives_key(ctx: AppContext) -> None:
    """`add` with a #-name creates a public channel keyed from the name."""
    tool = ChannelsTool()
    await tool.run(ctx, {"cli_action": "add", "index": 2, "name": "#general", "secret": None})
    slot = next(s for s in await _read_slots(await ctx.device()) if s.idx == 2)
    assert slot.name == "#general" and slot.is_public


async def test_cli_join_and_import_round_trip(ctx: AppContext) -> None:
    """A channel joined by key can be shared and re-imported to the same secret."""
    tool = ChannelsTool()
    secret = bytes(range(16))
    await tool.run(
        ctx, {"cli_action": "join", "index": 3, "name": "Squad", "secret": secret.hex()}
    )
    slot = next(s for s in await _read_slots(await ctx.device()) if s.idx == 3)
    assert slot.name == "Squad" and slot.secret == secret

    # Importing the channel's own share link onto another slot reproduces it.
    await tool.run(ctx, {"cli_action": "import", "index": 4, "url": share_url("Squad", secret)})
    imported = next(s for s in await _read_slots(await ctx.device()) if s.idx == 4)
    assert imported.name == "Squad" and imported.secret == secret


async def test_cli_import_rejects_bad_link(ctx: AppContext) -> None:
    """Importing a non-channel link is a clean parameter error, not a crash."""
    import typer

    tool = ChannelsTool()
    with pytest.raises(typer.BadParameter):
        await tool.run(ctx, {"cli_action": "import", "index": 5, "url": "https://nope"})
