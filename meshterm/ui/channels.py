"""The interactive channel manager, rendered in the full-screen session.

This is the user-facing channel-management experience for the ``channels`` tool: a live
list of the device's channel slots with, for each, a detail view that shows the sharable
QR code and key, renames or re-keys it, opens it in chat, or clears it. New channels are
created four ways — a fresh private channel (random key), a public ``#`` channel (key
derived from the name), joining by pasting a key, or importing a scanned ``meshcore://``
link. Every change is written to the device immediately (like a phone app), so the list you
see always reflects the radio.

The module sits in the UI layer but, like the config editor and chat screen, is allowed to
depend on the context and services; it owns no persistence of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..core.channels import (
    CHANNEL_SLOT_PROBE_CAP,
    MAX_CHANNELS,
    channel_hash,
    channel_identity,
    derive_secret,
    full_channel_hash,
    is_name_derived,
    is_public_channel,
    normalize_secret,
    parse_share_url,
    random_secret,
    share_url,
)
from ..core.connection import Device
from ..core.models import Conversation
from .qr import qr_text
from .tui import Choice, Separator
from .widgets import channel_glyph

if TYPE_CHECKING:
    from ..context import AppContext

# Top-menu action sentinels (distinct from a plain slot index, which selects that channel).
_CREATE = "__create__"
_PUBLIC = "__public__"
_JOIN = "__join__"
_IMPORT = "__import__"
_REORDER = "__reorder__"
_BACK = "__back__"

# Channel-detail action sentinels.
_QR = "qr"
_KEY = "key"
_CHAT = "chat"
_EDIT = "edit"
_CLEAR = "clear"


@dataclass(slots=True)
class ChannelSlot:
    """One configured channel slot read back from the device.

    Attributes:
        idx: The 0-based slot index.
        name: The channel's name.
        secret: The channel's 16-byte shared secret.
    """

    idx: int
    name: str
    secret: bytes

    @property
    def is_name_derived(self) -> bool:
        """Whether this channel's key is reproducible from its name (so it needn't be stored)."""
        return is_name_derived(self.name, self.secret)

    @property
    def is_public(self) -> bool:
        """Whether this channel is public (shared meshwide) rather than a private one.

        Covers both name-derived channels and the firmware's fixed-key default ``Public``.
        """
        return is_public_channel(self.name, self.secret)

    @property
    def hash(self) -> str:
        """The channel's two-character hash fingerprint (the leading byte MeshCore shows)."""
        return channel_hash(self.secret)

    @property
    def full_hash(self) -> str:
        """The channel's complete ``sha256(secret)`` digest; its first byte is :attr:`hash`."""
        return full_channel_hash(self.secret)

    @property
    def identity(self) -> str:
        """The channel's slot-independent identity, used to key its chat history."""
        return channel_identity(self.name, self.secret)

    @property
    def conversation(self) -> Conversation:
        """A :class:`~meshterm.core.models.Conversation` for opening this channel in chat."""
        return Conversation(
            label=self.name,
            is_channel=True,
            channel_idx=self.idx,
            channel_id=self.identity,
            secret=self.secret,
        )


async def manage_channels(ctx: "AppContext") -> int:
    """Run the interactive channel manager until the user backs out.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).

    Returns:
        The number of channels created, changed, or cleared during the session.
    """
    device = await ctx.device()
    # The firmware's slot count is fixed for the session, so discover it once (a read-only
    # probe) rather than assuming a hard-coded 8; it drives the free-slot check and the
    # used/total display below. A device that can't report any slots falls back to the
    # standard count so the manager stays usable instead of showing zero capacity.
    capacity = await device.channel_capacity() or MAX_CHANNELS
    changes = 0
    highlight: Optional[object] = None

    while True:
        slots = await _read_slots(device)
        choice = await _main_menu(ctx, slots, capacity, default=highlight)
        if choice in (None, _BACK):
            return changes
        highlight = choice
        before = changes
        if choice == _CREATE:
            changes += await _create_private(ctx, device, slots, capacity)
        elif choice == _PUBLIC:
            changes += await _add_public(ctx, device, slots, capacity)
        elif choice == _JOIN:
            changes += await _join_with_key(ctx, device, slots, capacity)
        elif choice == _IMPORT:
            changes += await _import_link(ctx, device, slots, capacity)
        elif choice == _REORDER:
            changes += await _reorder_channels(ctx, device, slots)
        else:  # an existing slot index
            slot = next((s for s in slots if s.idx == choice), None)
            if slot is not None:
                changes += await _channel_detail(ctx, device, slot)
        if changes > before:
            # A slot's occupant changed (created, re-keyed, cleared, or moved). Inbound
            # messages carry only a slot index, which the chat service maps to a channel
            # identity through a cache keyed by slot; refresh it now so a message on a
            # reused/re-keyed slot is filed under the channel that's actually there and not
            # the one that used to be — otherwise its transcript surfaces in the wrong chat.
            await _refresh_chat_channels(ctx)
    # unreachable


async def _refresh_chat_channels(ctx: "AppContext") -> None:
    """Rebuild the chat service's slot→identity cache after a channel mutation.

    Best-effort: a device read hiccup here must never break the channel manager, and the
    cache also self-heals on the next miss, so a failure is only logged.
    """
    try:
        await ctx.chat.refresh_channels()
    except Exception as exc:  # noqa: BLE001 - refresh is best-effort; never fatal here
        ctx.log.debug("channels: chat cache refresh failed: %s", exc)


# --- reading -----------------------------------------------------------------


async def _read_slots(device: Device) -> list[ChannelSlot]:
    """Probe the channel slots and return the configured ones, in index order.

    The scan runs up to :data:`CHANNEL_SLOT_PROBE_CAP` and stops as soon as the firmware
    rejects a slot index, so it reads exactly the slots the device actually has regardless
    of its capacity.

    Args:
        device: The connected device to query.

    Returns:
        One :class:`ChannelSlot` per configured slot (an empty slot is skipped).
    """
    slots: list[ChannelSlot] = []
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            payload = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - firmware may not support channel reads
            break
        if payload and payload.get("channel_name"):
            slots.append(
                ChannelSlot(
                    idx=idx,
                    name=str(payload["channel_name"]),
                    secret=bytes(payload.get("channel_secret") or b"\x00" * 16),
                )
            )
    return slots


def _next_free_slot(slots: list[ChannelSlot], capacity: int) -> Optional[int]:
    """Return the lowest unused slot index, or ``None`` when every slot is full."""
    used = {s.idx for s in slots}
    return next((i for i in range(capacity) if i not in used), None)


def _slot_label(slot: ChannelSlot) -> str:
    """Format a channel for a menu row: name, public/private, and hash (no slot index)."""
    glyph = channel_glyph(slot.name, slot.secret)  # ＃ / 🌐 / 🔒
    word = "private" if glyph == "🔒" else "public"
    return f"{slot.name:<18.18} {glyph} {word:<7}  hash {slot.hash}"


# --- menus -------------------------------------------------------------------


async def _main_menu(
    ctx: "AppContext", slots: list[ChannelSlot], capacity: int, *, default: object = None
) -> object:
    """Show the channel list and the add-a-channel actions; return the chosen value."""
    items: list = [Separator(f"── Channels ({len(slots)}/{capacity}) ──", style="accent")]
    if slots:
        for slot in slots:
            items.append(Choice(title=_slot_label(slot), value=slot.idx))
    else:
        items.append(Separator("  (no channels configured yet)"))

    if len(slots) > 1:
        items.append(Separator("── Organize ──", style="accent"))
        items.append(Choice(title="↕ Reorder channels", value=_REORDER))

    items.append(Separator("── Add a channel ──", style="accent"))
    items.append(Choice(title="＋ New private channel (random key)", value=_CREATE))
    items.append(Choice(title="＃ Public channel (key from its name)", value=_PUBLIC))
    items.append(Choice(title="🔑 Join a channel with its key", value=_JOIN))
    items.append(Choice(title="🔗 Import a meshcore:// link", value=_IMPORT))
    items.append(Separator(" "))
    items.append(Choice(title="Back", value=_BACK))

    choice = await ctx.ui.select("Channels", items, default=default)
    return _BACK if choice is None else choice


async def _channel_detail(ctx: "AppContext", device: Device, slot: ChannelSlot) -> int:
    """Show one channel's actions (QR, key, chat, rename, clear); return changes made."""
    kind = "public" if slot.is_public else "private"
    while True:
        choice = await ctx.ui.select(
            f"{slot.name}  ({kind}, hash {slot.hash})",
            [
                Choice(title="📱 Show QR code (share)", value=_QR),
                Choice(title="🔑 Show key", value=_KEY),
                Choice(title="💬 Open in chat", value=_CHAT),
                Choice(title="✎ Rename / change key", value=_EDIT),
                Choice(title="🗑 Clear this slot", value=_CLEAR),
                Separator(" "),
                Choice(title="Back", value=_BACK),
            ],
        )
        if choice in (None, _BACK):
            return 0
        if choice == _QR:
            await _show_share(ctx, slot.name, slot.secret)
        elif choice == _KEY:
            await _show_key(ctx, slot)
        elif choice == _CHAT:
            await _open_chat(ctx, slot)
        elif choice == _EDIT:
            if await _edit(ctx, device, slot):
                return 1
        elif choice == _CLEAR:
            if await _clear(ctx, device, slot):
                return 1


# --- create / join flows -----------------------------------------------------


async def _create_private(
    ctx: "AppContext", device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Create a private channel with a fresh random key on the next free slot."""
    idx = await _pick_free_slot(ctx, slots, capacity)
    if idx is None:
        return 0
    name = await ctx.ui.text("Channel name:", validate=_nonblank)
    if not name:
        return 0
    secret = random_secret()
    await device.set_channel(idx, name.strip(), secret)
    ctx.ui.note(f"[ok]✓[/ok] created private channel [brand]{name.strip()}[/brand]")
    await _show_share(ctx, name.strip(), secret, intro="Share this channel:")
    return 1


async def _add_public(
    ctx: "AppContext", device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Create a public channel whose key is derived from its (``#``-prefixed) name."""
    idx = await _pick_free_slot(ctx, slots, capacity)
    if idx is None:
        return 0
    raw = await ctx.ui.text(
        "Public channel name:",
        help_text="A leading # is added automatically; the key is derived from the name",
        validate=_nonblank,
    )
    if not raw:
        return 0
    name = raw.strip()
    if not name.startswith("#"):
        name = f"#{name}"
    secret = derive_secret(name)  # what the firmware will compute; kept for the QR/share
    await device.set_channel(idx, name, None)  # None => firmware derives the key from name
    ctx.ui.note(f"[ok]✓[/ok] added public channel [brand]{name}[/brand]")
    await _show_share(ctx, name, secret, intro="Share this channel:")
    return 1


async def _join_with_key(
    ctx: "AppContext", device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Join an existing private channel by entering its name and 16-byte key."""
    idx = await _pick_free_slot(ctx, slots, capacity)
    if idx is None:
        return 0
    name = await ctx.ui.text("Channel name:", validate=_nonblank)
    if not name:
        return 0
    key = await ctx.ui.text(
        "Channel key (32 hex characters / 16 bytes):", validate=_valid_secret
    )
    if not key:
        return 0
    secret = normalize_secret(key)
    await device.set_channel(idx, name.strip(), secret)
    ctx.ui.note(f"[ok]✓[/ok] joined [brand]{name.strip()}[/brand]")
    return 1


async def _import_link(
    ctx: "AppContext", device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Import a channel from a pasted ``meshcore://channel/add`` link."""
    idx = await _pick_free_slot(ctx, slots, capacity)
    if idx is None:
        return 0
    url = await ctx.ui.text(
        "Paste a meshcore:// channel link:", validate=_valid_link
    )
    if not url:
        return 0
    parsed = parse_share_url(url)
    if parsed is None:  # pragma: no cover - guarded by the validator
        ctx.ui.note("[err]not a valid channel link[/err]")
        return 0
    name, secret = parsed
    await device.set_channel(idx, name, secret)
    ctx.ui.note(f"[ok]✓[/ok] imported [brand]{name}[/brand]")
    return 1


async def _edit(ctx: "AppContext", device: Device, slot: ChannelSlot) -> bool:
    """Rename and/or re-key an existing channel; return whether it changed."""
    name = await ctx.ui.text("Channel name:", default=slot.name, validate=_nonblank)
    if name is None:
        return False
    name = name.strip()
    key = await ctx.ui.text(
        "Channel key (32 hex chars; blank to derive from the name):",
        default="" if slot.is_name_derived else slot.secret.hex(),
        validate=_optional_secret,
    )
    if key is None:
        return False
    secret = normalize_secret(key) if key.strip() else None
    await device.set_channel(slot.idx, name, secret)
    ctx.ui.note(f"[ok]✓[/ok] updated channel [brand]{name}[/brand]")
    return True


async def _clear(ctx: "AppContext", device: Device, slot: ChannelSlot) -> bool:
    """Clear a channel slot after confirmation; return whether it was cleared."""
    if not await ctx.ui.confirm(
        f"Clear [brand]{slot.name}[/brand]? This removes the channel from this device.",
        default=False,
    ):
        return False
    await device.set_channel(slot.idx, "", None)  # empty name => the slot reads as unused
    ctx.ui.note(f"[warn]cleared channel {slot.name}[/warn]")
    return True


# --- reordering --------------------------------------------------------------


async def _write_slot(device: Device, idx: int, slot: ChannelSlot) -> None:
    """Write ``slot``'s contents into slot ``idx`` (name-derived channels re-derive their key)."""
    secret = None if slot.is_name_derived else slot.secret
    await device.set_channel(idx, slot.name, secret)


async def _apply_order(
    device: Device, slots: list[ChannelSlot], order: list[int]
) -> int:
    """Rewrite the channel slots so they display in ``order``; return the writes made.

    ``slots`` is the current list in slot-index order and ``order`` is a permutation of its
    positions (as returned by the reorder screen). The channels are re-laid across the same
    physical slot indices, lowest first, so their on-device order matches the new display
    order. Only slots whose occupant actually changes are written. Reads come from the
    in-memory snapshot, so the interleaved writes never clobber a not-yet-placed channel.
    """
    indices = sorted(s.idx for s in slots)  # the physical slots to fill, ascending
    changes = 0
    for target_idx, pos in zip(indices, order, strict=True):
        slot = slots[pos]
        if slot.idx == target_idx:
            continue  # already in place; no write needed
        await _write_slot(device, target_idx, slot)
        changes += 1
    return changes


async def _reorder_channels(
    ctx: "AppContext", device: Device, slots: list[ChannelSlot]
) -> int:
    """Let the user drag channels into a new order with the arrows; return writes made."""
    if len(slots) < 2:  # pragma: no cover - the menu only offers reorder with 2+ channels
        return 0
    labels = [_slot_label(slot) for slot in slots]
    order = await ctx.ui.reorder("Reorder channels", labels)
    if not order or order == list(range(len(slots))):
        return 0  # cancelled or left unchanged
    # Chat history is keyed by each channel's intrinsic identity, not its slot, so it follows
    # the channels automatically — reordering needs no history migration. The slot→identity
    # cache the chat service resolves inbound messages through is refreshed centrally by
    # manage_channels once any change lands (see :func:`_refresh_chat_channels`).
    return await _apply_order(device, slots, order)


# --- shared views ------------------------------------------------------------


async def _show_share(
    ctx: "AppContext", name: str, secret: bytes, *, intro: str = "Scan to add this channel:"
) -> None:
    """Show the channel's QR code and its share URL in a dismissable window."""
    url = share_url(name, secret)
    body = Group(
        Text(intro, style="muted"),
        Text(""),
        qr_text(url),
        Text(""),
        Text(url, style="accent"),
    )
    await ctx.ui.view(body, title=f"Share {name}", footer_hint="Esc back")


def _hash_text(full_hash: str) -> Text:
    """Render a full channel hash with its first byte (the MeshCore fingerprint) highlighted."""
    text = Text(no_wrap=True)
    text.append(full_hash[:2], style="brand")  # the leading byte the companion app reports
    text.append(full_hash[2:], style="muted")
    return text


async def _show_key(ctx: "AppContext", slot: ChannelSlot) -> None:
    """Show a channel's name, key, hash, and share URL in a small table."""
    table = Table(border_style="muted", expand=False, show_header=False)
    table.add_column("", style="muted")
    table.add_column("")
    table.add_row("Name", Text(slot.name, style="brand"))
    if slot.is_name_derived:
        kind = "public (key from name)"
    elif slot.is_public:
        kind = "public (default channel)"
    else:
        kind = "private"
    table.add_row("Type", kind)
    table.add_row("Hash", _hash_text(slot.full_hash))
    table.add_row("Key", Text(slot.secret.hex(), style="warn"))
    table.add_row("Link", Text(share_url(slot.name, slot.secret), style="accent"))
    await ctx.ui.view(table, title=f"Key — {slot.name}", footer_hint="Esc back")


async def _open_chat(ctx: "AppContext", slot: ChannelSlot) -> None:
    """Open this channel in the live chat screen."""
    from .chat import open_chat

    await open_chat(ctx, slot.conversation)


async def _pick_free_slot(
    ctx: "AppContext", slots: list[ChannelSlot], capacity: int
) -> Optional[int]:
    """Return the next free slot, warning (and returning ``None``) if all are full."""
    idx = _next_free_slot(slots, capacity)
    if idx is None:
        await ctx.ui.view(
            Text.from_markup(
                "[warn]All channel slots are full.[/warn] Clear one first, then try again."
            ),
            title="No free slot",
        )
    return idx


# --- validators --------------------------------------------------------------


def _nonblank(text: str) -> bool | str:
    """Require a non-empty name."""
    return bool(text.strip()) or "Enter a channel name."


def _valid_secret(text: str) -> bool | str:
    """Require a valid 16-byte hex key."""
    try:
        normalize_secret(text)
        return True
    except ValueError as exc:
        return str(exc)


def _optional_secret(text: str) -> bool | str:
    """Accept a blank key (derive from name) or a valid 16-byte hex key."""
    return True if not text.strip() else _valid_secret(text)


def _valid_link(text: str) -> bool | str:
    """Require a parseable ``meshcore://channel/add`` link."""
    return parse_share_url(text) is not None or "Not a valid meshcore:// channel link."
