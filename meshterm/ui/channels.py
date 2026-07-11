"""The interactive channel manager, rendered in the full-screen session.

This is the user-facing channel-management experience for the ``channels`` tool: a live
list of the device's channel slots with, for each, a detail view that shows the sharable
QR code and key, renames or re-keys it, opens it in chat, or clears it. New channels are
created four ways — a fresh private channel (random key), a public ``#`` channel (key
derived from the name), joining by pasting a key, or importing a scanned ``meshcore://``
link. Every change is written to the device immediately (like a phone app), so the list you
see always reflects the radio.

The list is laid out like the config editor: fixed, column-aligned lanes under one header
line — the openness glyph and name, unread badge, total messages, last-message age, and a
braille sparkline of the trailing two hours' traffic — so a glance shows not just *which*
channels exist but which ones are alive. Openness beyond the glyph, and the hash, live in
the detail views (the title line and Show key), keeping the list lean enough that the
activity lane survives a 72-column terminal. The message statistics come from
:meth:`~meshterm.persistence.repository.Repository.channel_stats` (read through a small
TTL cache) and the unread counts from the live chat service, and each row is a callable
title re-resolved on repaint, so a message arriving while the list sits open updates its
row in place — the same trick the conversation picker uses. The menus also follow the
config editor's persistent-backdrop pattern: the list stays pushed while every sub-prompt
floats over it as a modal popup, rather than replacing the screen.

The module sits in the UI layer but, like the config editor and chat screen, is allowed to
depend on the context and services; it owns no persistence of its own.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from rich.cells import cell_len
from rich.console import Group
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
from ..persistence.repository import ACTIVITY_BUCKETS
from .braillechart import activity_sparkline
from .qr import qr_text
from .tui import CANCEL, Choice, SelectScreen, Separator
from .widgets import _age_seconds, _format_age, channel_glyph

if TYPE_CHECKING:
    from ..context import AppContext
    from ..persistence.repository import ChannelStats

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
    stats = _LiveStats(ctx)
    changes = 0
    highlight: Optional[object] = None

    while True:
        slots = await read_channel_slots(device)
        title, items = _menu_items(ctx, slots, capacity, stats)

        async def handle(choice: object) -> bool:
            """Dispatch one menu choice (over the still-pushed list); ``False`` exits."""
            nonlocal changes, highlight
            if choice in (None, _BACK):
                return False
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
                    changes += await _channel_detail(ctx, device, slot, stats)
            if changes > before:
                # A slot's occupant changed (created, re-keyed, cleared, or moved). Inbound
                # messages carry only a slot index, which the chat service maps to a channel
                # identity through a cache keyed by slot; refresh it now so a message on a
                # reused/re-keyed slot is filed under the channel that's actually there and
                # not the one that used to be — otherwise its transcript surfaces in the
                # wrong chat.
                await _refresh_chat_channels(ctx)
            return True

        if not await _menu_round(ctx, title, items, default=highlight, handle=handle):
            return changes
    # unreachable


async def _menu_round(
    ctx: "AppContext",
    title: str,
    items: list,
    *,
    handle: Callable[[object], Awaitable],
    default: object = None,
    prompt: str = "",
) -> object:
    """Show one round of a channels menu and dispatch the choice over it.

    In the full-screen session the menu stays *pushed* while ``handle`` runs, so every
    sub-prompt floats over the list as a modal popup with its own border — the config
    editor's persistent-backdrop pattern (see
    :func:`~meshterm.ui.config_editor.edit_config`) — instead of replacing the screen. A
    surface without a session (the plain CLI surface, scripted tests) simply selects and
    then dispatches; ``prompt`` is a session-only nicety and is dropped there.

    Args:
        ctx: Shared application context.
        title: The menu's border heading.
        items: The menu's :class:`Choice`/:class:`Separator` rows.
        handle: Async dispatcher awaited with the chosen value (``None`` for Esc).
        default: A choice value to re-highlight, so the menu reopens where it was left.
        prompt: An optional summary line drawn inside the box above the rows.

    Returns:
        Whatever ``handle`` returns.
    """
    session = getattr(ctx.ui, "session", None)
    if session is None:
        return await handle(await ctx.ui.select(title, items, default=default))
    menu = SelectScreen(title, items, prompt=prompt, default=default, wrap=False)
    menu.future = asyncio.get_running_loop().create_future()
    session.push(menu)
    try:
        choice = await menu.future
        return await handle(None if choice is CANCEL else choice)
    finally:
        session.pop(menu)


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


async def read_channel_slots(device: Device) -> list[ChannelSlot]:
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
    """Format a channel for a compact row (the reorder screen): just its glyph and name.

    The reorder screen is about *position*, not vitals — the openness, hash, and message
    lanes of the manager list would only widen the popup — so each row is the channel
    exactly as the manager's first lane shows it: glyph, gap, name.
    """
    return f"{channel_glyph(slot.name, slot.secret)} {slot.name}"


# --- message statistics --------------------------------------------------------


class _LiveStats:
    """A self-refreshing view of every channel's stored-message statistics.

    The conversation picker's ``_LiveLasts`` pattern applied to
    :meth:`~meshterm.persistence.repository.Repository.channel_stats`: the list rows read
    through this on every repaint (their titles are callables), so a message arriving while
    the manager sits open updates that channel's counts, age, and sparkline in place —
    but the repository is re-queried at most once per ``ttl`` seconds rather than once per
    row per repaint, so a full slot table stays cheap at the session's ~1 Hz repaint.
    """

    def __init__(self, ctx: "AppContext", *, ttl: float = 1.0) -> None:
        """Bind to a context; the first read populates the cache."""
        self._ctx = ctx
        self._ttl = ttl
        self._cache: Optional[dict[str, "ChannelStats"]] = None
        self._at = 0.0

    def get(self, channel_id: str) -> Optional["ChannelStats"]:
        """Return the stats for one channel identity, refreshing once the TTL lapses."""
        now = time.monotonic()
        if self._cache is None or now - self._at >= self._ttl:
            try:
                self._cache = self._ctx.repo.channel_stats()
            except Exception:  # noqa: BLE001 - keep the last good snapshot on a read error
                self._cache = self._cache or {}
            self._at = now
        return self._cache.get(channel_id)


#: Widest the name lane grows (longer names are ellipsized so the lanes stay put).
_NAME_WIDTH_MAX = 18
#: Width of the unread-badge lane (fits ``● 999``), matching the conversation picker's.
_BADGE_WIDTH = 5
#: Width of the right-aligned total-messages lane.
_COUNT_WIDTH = 5
#: Width of the right-aligned last-message-age lane (fits ``never``-length ages).
_AGE_WIDTH = 5
#: Message counts a bucket must reach for each extra dot of bar height (Fibonacci-ish, so
#: each dot roughly means "a conversation tier up"): 1 message lights one dot, 3 light two,
#: 8 light three, and 21 or more max the column out.
_ACTIVITY_LEVELS = (1, 3, 8, 21)


def _activity_sparkline(histogram: "tuple[int, ...]") -> Text:
    """The channel's braille activity sparkline over the trailing two hours, now at the right.

    The shared :func:`~meshterm.ui.braillechart.activity_sparkline`, drawn at this list's
    message-count thresholds over the repository histogram's :data:`ACTIVITY_BUCKETS`
    five-minute buckets.
    """
    return activity_sparkline(histogram, _ACTIVITY_LEVELS, ACTIVITY_BUCKETS)


def _fit(text: str, width: int) -> str:
    """Left-justify ``text`` to ``width`` columns, ellipsizing anything that would overflow."""
    if len(text) > width:
        return text[: width - 1] + "…"
    return text.ljust(width)


# --- menus -------------------------------------------------------------------


def _lanes_header(name_w: int) -> str:
    """Column headers over the channel list's fixed lanes (see :func:`_slot_text`).

    The leading spaces cover the select screen's pointer column (2 cells) plus the glyph
    lane (3 cells), so each header lands exactly over its column. ``UNREAD`` borrows its
    lane's trailing gap — the badge lane itself is one cell too narrow for the word — which
    still leaves a space before the message count. (No TYPE or HASH lane: the glyph already
    carries the openness and the hash lives in Show key, which buys the activity sparkline
    its room on a 72-column terminal.)
    """
    return (
        "     "
        + "CHANNEL".ljust(name_w + 2)
        + "UNREAD".ljust(_BADGE_WIDTH + 2)
        + f"{'MSGS':>{_COUNT_WIDTH}}"
        + "  "
        + f"{'LAST':>{_AGE_WIDTH}}"
        + "  ACTIVITY"
    )


def _slot_row(
    ctx: "AppContext", slot: ChannelSlot, stats: _LiveStats, name_w: int
) -> Callable[[], Text]:
    """Return a list-row title *callable* the select screen re-renders on each repaint.

    The unread badge, counts, age, and activity sparkline are all read live (see
    :class:`_LiveStats`), so a message arriving while the list sits open updates the row on
    the next repaint — exactly the conversation picker's behavior.
    """
    return lambda: _slot_text(ctx, slot, stats, name_w)


def _slot_text(
    ctx: "AppContext", slot: ChannelSlot, stats: _LiveStats, name_w: int
) -> Text:
    """Build one channel's list row as fixed-width, colour-coded lanes.

    Alignment carries the readability — glyph, name, unread badge, total messages,
    last-message age, and the activity sparkline each sit in their own lane under the
    :func:`_lanes_header` line. Colour stays light and purposeful: the name is the row's
    focus in the base colour, the descriptive lanes are muted, the unread ``●`` badge is
    red with its count in warn (the conversation picker's language), and the sparkline
    draws in the ok green over a faint flatline. The row is always a Rich
    :class:`~rich.text.Text` so those spans
    survive under the select screen's row highlight.
    """
    st = stats.get(slot.identity)
    unread = ctx.chat.unread(slot.conversation.key)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{channel_glyph(slot.name, slot.secret)} ")  # ＃ / 🌐 / 🔒 (2 cells) + gap
    text.append(_fit(slot.name, name_w))
    text.append("  ")
    if unread:
        text.append("●", style="err")
        text.append(f" {unread}".ljust(_BADGE_WIDTH - 1), style="warn")
    else:
        text.append(" " * _BADGE_WIDTH)
    text.append("  ")
    total = st.total if st is not None else 0
    if total:
        # Clamped so a pathological backlog can't push the row out of its lanes.
        text.append(f"{min(total, 99999):>{_COUNT_WIDTH}}")
    else:
        text.append(f"{'·':>{_COUNT_WIDTH}}", style="muted")
    text.append("  ")
    age = _format_age(_age_seconds(st.last_at)) if st is not None and st.last_at else ""
    text.append(f"{age:>{_AGE_WIDTH}}", style="muted")
    text.append("  ")
    text.append_text(_activity_sparkline(st.histogram if st is not None else ()))
    return text


def _menu_items(
    ctx: "AppContext", slots: list[ChannelSlot], capacity: int, stats: _LiveStats
) -> tuple[str, list]:
    """Build the channel manager's title and rows for the current slot table.

    Returns the ``(title, items)`` for one :func:`_menu_round`: the channel rows in their
    aligned lanes under a column-header line (the config editor's presentation), then the
    Organize and Add-a-channel action sections. The slot usage lives in the title, so the
    header line is free to be pure column labels.
    """
    items: list = []
    if slots:
        name_w = min(_NAME_WIDTH_MAX, max(len("CHANNEL"), *(len(s.name) for s in slots)))
        items.append(Separator(_lanes_header(name_w)))
        for slot in slots:
            items.append(Choice(title=_slot_row(ctx, slot, stats, name_w), value=slot.idx))
    else:
        items.append(Separator("  (no channels configured yet)"))

    if len(slots) > 1:
        items.append(Separator("── Organize ──", style="accent"))
        # Two spaces after the arrow: ↕ (East-Asian-ambiguous width) renders one cell where
        # the sibling rows' glyphs (＋ ＃ 🔑 🔗) render two, so the extra space keeps this
        # label's text column-aligned with theirs.
        items.append(Choice(title="↕  Reorder channels", value=_REORDER))

    items.append(Separator("── Add a channel ──", style="accent"))
    items.append(Choice(title="＋ New private channel (random key)", value=_CREATE))
    items.append(Choice(title="＃ Public channel (key from its name)", value=_PUBLIC))
    items.append(Choice(title="🔑 Join a channel with its key", value=_JOIN))
    items.append(Choice(title="🔗 Import a meshcore:// link", value=_IMPORT))
    items.append(Separator(" "))
    items.append(Choice(title="Back", value=_BACK))

    return f"Channels — {len(slots)}/{capacity} slots", items


def _detail_summary(ctx: "AppContext", slot: ChannelSlot, stats: _LiveStats) -> str:
    """One line of vital signs for the detail screen: slot, totals, unread, last activity."""
    st = stats.get(slot.identity)
    unread = ctx.chat.unread(slot.conversation.key)
    if st is None or not st.total:
        return f"Slot {slot.idx} · no messages recorded yet"
    parts = [f"Slot {slot.idx}", f"{st.total} message{'' if st.total == 1 else 's'}"]
    if unread:
        parts.append(f"{unread} unread")
    if st.last_at is not None:
        age = _format_age(_age_seconds(st.last_at))
        parts.append("last message just now" if age == "now" else f"last message {age} ago")
    return " · ".join(parts)


def _detail_items(ctx: "AppContext", slot: ChannelSlot) -> list:
    """Build the channel-detail rows: label and description in two aligned lanes.

    The Device actions presentation (menu-style lanes, no header line — these are commands,
    not tabular data), padded in display cells so the double-width emoji can't skew the
    description column. Open-in-chat carries the channel's live unread badge, and the one
    destructive row keeps an err-tinted label so it reads as such.
    """
    unread = ctx.chat.unread(slot.conversation.key)
    chat_label = Text("💬 Open in chat")
    if unread:
        chat_label.append("  ●", style="err")
        chat_label.append(f" {unread}", style="warn")
    rows: list[tuple[Text, str, object]] = [
        (Text("📱 Show QR code"), "Share this channel as a scannable code", _QR),
        (Text("🔑 Show key"), "The name, key, hash, and share link", _KEY),
        (chat_label, "Read and send messages on this channel", _CHAT),
        (Text("✎ Rename / change key"), "Edit the name or paste a different key", _EDIT),
        (Text("🗑 Clear this slot", style="err"), "Remove the channel from this device", _CLEAR),
    ]
    width = max(cell_len(label.plain) for label, _, _ in rows)
    items: list = []
    for label, help_text, value in rows:
        row = Text()
        row.append_text(label)
        row.append(" " * (width - cell_len(label.plain) + 2))
        row.append(help_text, style="muted")
        items.append(Choice(title=row, value=value))
    items.append(Separator(" "))
    items.append(Choice(title="Back", value=_BACK))
    return items


async def _channel_detail(
    ctx: "AppContext", device: Device, slot: ChannelSlot, stats: _LiveStats
) -> int:
    """Show one channel's actions (QR, key, chat, rename, clear); return changes made.

    The screen leads with the channel's vital signs (see :func:`_detail_summary`) above the
    action rows, and — like the main list — stays pushed while each action's prompts float
    over it. A rename/re-key or clear closes the detail (the slot's occupant changed, so
    the caller re-reads the device); the read-only actions loop back here.
    """
    kind = "public" if slot.is_public else "private"
    title = f"{slot.name}  ({kind}, hash {slot.hash})"
    cursor: object = None

    async def handle(choice: object) -> Optional[int]:
        """Run one action; an int closes the detail with that many changes, ``None`` stays."""
        nonlocal cursor
        if choice in (None, _BACK):
            return 0
        cursor = choice
        if choice == _QR:
            await _show_share(ctx, slot.name, slot.secret)
        elif choice == _KEY:
            await _show_key(ctx, slot)
        elif choice == _CHAT:
            await _open_chat(ctx, slot)
        elif choice == _EDIT and await _edit(ctx, device, slot):
            return 1
        elif choice == _CLEAR and await _clear(ctx, device, slot):
            return 1
        return None

    while True:
        result = await _menu_round(
            ctx,
            title,
            _detail_items(ctx, slot),
            default=cursor,
            prompt=_detail_summary(ctx, slot, stats),
            handle=handle,
        )
        if result is not None:
            return result


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


async def _show_key(ctx: "AppContext", slot: ChannelSlot) -> None:
    """Show a channel's name, type, hash, key, and share link as labelled blocks.

    Deliberately not a table: a bordered grid inside a popup is visual noise and wastes
    the very columns the values need. Each field is instead a muted column-header-style
    label with its value on the line beneath, and the long values — the hash, key, and
    link exist to be copied out whole — *wrap* to the popup's width (``overflow="fold"``,
    since they are single unbreakable words) rather than being chopped at an ellipsis.
    The hash's first byte keeps the brand highlight: it is the two-character fingerprint
    the MeshCore companion app reports.
    """
    if slot.is_name_derived:
        kind = "public (key from name)"
    elif slot.is_public:
        kind = "public (default channel)"
    else:
        kind = "private"
    hash_text = Text(overflow="fold")
    hash_text.append(slot.full_hash[:2], style="brand")
    hash_text.append(slot.full_hash[2:], style="muted")
    fields: list[tuple[str, Text]] = [
        ("NAME", Text(slot.name, style="brand", overflow="fold")),
        ("TYPE", Text(kind)),
        ("HASH", hash_text),
        ("KEY", Text(slot.secret.hex(), style="warn", overflow="fold")),
        ("LINK", Text(share_url(slot.name, slot.secret), style="accent", overflow="fold")),
    ]
    blocks: list[Text] = []
    for label, value in fields:
        if blocks:
            blocks.append(Text())
        blocks.append(Text(label, style="muted"))
        blocks.append(value)
    await ctx.ui.view(Group(*blocks), title=f"Key — {slot.name}", footer_hint="Esc back")


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
