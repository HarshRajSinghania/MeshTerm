"""The Watchtower screens: the alert log, the watchlist, and per-node rules.

The interactive face of the ``watchtower`` tool. One select-list screen carries the
whole feature (the persistent-backdrop pattern the config editors use): the alert log
newest-first — unacknowledged alerts lead with the header badge's red marker, Enter
acknowledges one — followed by the watchlist (Enter opens a node's rule popover) and
the actions: star another node, toggle the mesh-wide new-node rule, acknowledge or
clear in bulk.

Everything here reads and writes the :class:`~meshterm.core.watch_store.WatchStore`;
the rules themselves run in :mod:`meshterm.services.watchtower`, which the menu starts
with the other always-on services (and this screen nudges, idempotently, in case it
is opened before that ever happened). Nothing transmits.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

from rich.text import Text

from ..core.models import NODE_TYPE_REPEATER, Contact
from ..core.watch_store import OFF, SILENCE_CHOICES_H, Alert, WatchedNode
from .tui import Choice, Separator
from .widgets import _age_seconds, _format_age

if TYPE_CHECKING:
    from ..context import AppContext

#: Alert-kind display styles: alarms red, warnings amber, notes calm.
_KIND_STYLES = {
    "silence": "err",
    "snr": "warn",
    "new-node": "brand",
    "recovered": "ok",
}

#: How many alerts the screen lists (the store keeps more; the tail rarely matters).
_SHOWN_ALERTS = 40

# Menu action sentinels (tuples so they never collide with alert ids or node keys).
_WATCH = ("watch",)
_TOGGLE_NEW = ("toggle-new",)
_ACK_ALL = ("ack-all",)
_CLEAR = ("clear",)


def contact_watch_key(contact: Contact) -> Optional[str]:
    """The watch-store key for a contact: the 12-hex id observations carry."""
    ident = (contact.public_key or contact.key_prefix or "").lower().removeprefix("0x")
    return ident[:12] or None


async def open_watchtower(ctx: "AppContext") -> Optional[dict[str, Any]]:
    """Run the Watchtower screen until dismissed.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Returns:
        A summary of what happened (for the tool's log), or ``None`` on plain exit.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi
    from .tui import CANCEL, SelectScreen

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the watchtower is only available in the menu")
    session = ctx.ui.session
    await ctx.watchtower.start()  # idempotent; normally already running

    contacts: list[Contact] = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            device = await ctx.device()
            contacts = await device.get_contacts()
    except Exception:  # noqa: BLE001 - the log and rules render fine without contacts
        contacts = []

    store = ctx.watch_store
    loop = asyncio.get_running_loop()
    cursor: Any = None
    while True:
        items = _menu_items(store.alerts(), store.watched(), store.new_node_alerts)
        menu = SelectScreen(
            "🚨 Watchtower — alerts & watched nodes",
            items,
            default=cursor,
            wrap=False,
            footer_hint="↑↓ move · Enter select/acknowledge · Esc back",
        )
        menu.future = loop.create_future()
        session.push(menu)
        try:
            choice = await menu.future
            if choice in (None, CANCEL):
                return {"watched": len(store.watched()), "unacked": store.unacked_count()}
            cursor = choice
            if choice == _WATCH:
                await _pick_node(ctx, contacts)
            elif choice == _TOGGLE_NEW:
                store.set_new_node_alerts(not store.new_node_alerts)
            elif choice == _ACK_ALL:
                store.ack_all()
            elif choice == _CLEAR:
                store.clear_acked()
                cursor = None  # the row itself disappears
            elif isinstance(choice, tuple) and choice[0] == "ack":
                store.ack(int(choice[1]))
            elif isinstance(choice, tuple) and choice[0] == "node":
                await _node_rules(ctx, str(choice[1]))
        finally:
            session.pop(menu)


# --- the menu ---------------------------------------------------------------------------


def _menu_items(
    alerts: list[Alert], watched: dict[str, WatchedNode], new_node_alerts: bool
) -> list:
    """Build the screen's rows: alerts, then the watchlist, then the actions."""
    items: list = [Separator("Alerts", style="accent")]
    if not alerts:
        items.append(Separator("  nothing yet — tripped rules land here"))
    for alert in alerts[:_SHOWN_ALERTS]:
        items.append(Choice(_alert_row(alert), ("ack", alert.ident)))
    unacked = sum(1 for a in alerts if not a.acked)
    acked = len(alerts) - unacked
    if unacked:
        items.append(Choice(f"✔ Acknowledge all ({unacked})", _ACK_ALL))
    if acked:
        items.append(Choice("🗑 Clear acknowledged", _CLEAR))

    items.append(Separator(""))
    items.append(Separator("Watched nodes", style="accent"))
    if not watched:
        items.append(Separator("  none starred yet — silence and SNR rules need one"))
    for key in sorted(watched, key=lambda k: watched[k].name.casefold()):
        items.append(Choice(_watched_row(watched[key]), ("node", key)))
    items.append(Choice("⭐ Watch a node…", _WATCH))

    items.append(Separator(""))
    state = "[ok]on[/ok]" if new_node_alerts else "[muted]off[/muted]"
    items.append(
        Choice(
            Text.from_markup(f"🔔 New-node alerts: {state}  [muted]— announce first-ever appearances[/muted]"),
            _TOGGLE_NEW,
        )
    )
    return items


def _alert_row(alert: Alert) -> Text:
    """One alert as a row: marker, age, kind, node, and the message."""
    row = Text()
    if alert.acked:
        row.append("○ ", style="muted")
    else:
        row.append("● ", style="err")
    age = _format_age(_age_seconds(alert.when))
    row.append(f"{age:>5}  ", style="muted")
    row.append(alert.kind.ljust(10), style=_KIND_STYLES.get(alert.kind, "brand"))
    row.append(alert.label, style="muted" if alert.acked else None)
    row.append(f" — {alert.message}", style="muted")
    return row


def _watched_row(entry: WatchedNode) -> Text:
    """One watched node as a row: name, its rules, and when it was last heard."""
    row = Text()
    row.append("▲ ", style="err" if entry.silent_since is not None else "brand")
    row.append(entry.name)
    silence = "off" if entry.silence_hours == OFF else f"{entry.silence_hours} h"
    row.append(f"   silence {silence}", style="muted")
    row.append(" · SNR watch " + ("on" if entry.snr_watch else "off"), style="muted")
    if entry.silent_since is not None:
        row.append("  ·  ⚠ silent", style="err")
    elif entry.last_heard is not None:
        row.append(f"  ·  heard {_format_age(_age_seconds(entry.last_heard))}", style="muted")
    return row


# --- the flows --------------------------------------------------------------------------


async def _pick_node(ctx: "AppContext", contacts: list[Contact]) -> None:
    """Float the star-a-node picker: unwatched contacts, most recently heard first."""
    session = ctx.ui.session
    store = ctx.watch_store
    candidates = [
        (key, c)
        for c in contacts
        if (key := contact_watch_key(c)) is not None and not store.is_watched(key)
    ]
    if not candidates:
        message = (
            "Every known contact is already watched."
            if contacts
            else "No contacts available — connect a device that knows some nodes first."
        )
        await session.message_dialog(Text(message, style="muted"), title="watch a node")
        return

    def recency(pair: tuple[str, Contact]) -> float:
        seen = _age_seconds(pair[1].last_seen)
        return seen if seen is not None else float("inf")

    items: list = []
    for key, contact in sorted(candidates, key=recency):
        row = Text()
        row.append("▲ " if contact.node_type == NODE_TYPE_REPEATER else "● ", style="brand")
        row.append(contact.name)
        row.append(f"   heard {_format_age(_age_seconds(contact.last_seen))}", style="muted")
        items.append(Choice(row, (key, contact)))
    picked = await session.select(
        "⭐ Watch a node",
        items,
        prompt="Silence and SNR rules will watch it from now on:",
    )
    if picked is None:
        return
    key, contact = picked
    store.watch(key, contact.name, last_seen=contact.last_seen)


async def _node_rules(ctx: "AppContext", key: str) -> None:
    """Float one watched node's rule editor until dismissed (or the node is unstarred)."""
    session = ctx.ui.session
    store = ctx.watch_store
    while True:
        entry = store.watched().get(key)
        if entry is None:
            return
        silence = "off" if entry.silence_hours == OFF else f"after {entry.silence_hours} h"
        items = [
            Choice(f"🕐 Silence alarm      {silence}", "silence"),
            Choice("📶 SNR watch          " + ("on" if entry.snr_watch else "off"), "snr"),
            Separator(""),
            Choice("✖ Stop watching this node", "unwatch"),
        ]
        picked = await session.select(
            f"Rules — {entry.name}", items, filterable=False,
            footer_hint="↑↓ move · Enter change · Esc back",
        )
        if picked is None:
            return
        if picked == "silence":
            await _pick_silence(ctx, key, entry)
        elif picked == "snr":
            store.set_snr_watch(key, not entry.snr_watch)
        elif picked == "unwatch":
            store.unwatch(key)
            return


async def _pick_silence(ctx: "AppContext", key: str, entry: WatchedNode) -> None:
    """Float the silence-threshold picker for one watched node."""
    session = ctx.ui.session
    items = [Choice("Off — never alarm on silence", OFF)]
    for hours in SILENCE_CHOICES_H:
        label = f"After {hours} h of silence"
        if hours == entry.silence_hours:
            label += "   (current)"
        items.append(Choice(label, hours))
    picked = await session.select(
        f"Silence alarm — {entry.name}", items,
        default=entry.silence_hours, filterable=False,
        footer_hint="↑↓ move · Enter set · Esc keep",
    )
    if picked is not None:
        ctx.watch_store.set_silence(key, int(picked))
