"""Persistence for muted channel notifications.

Some channels carry traffic you want *recorded* but not *notified* about — an automated
``#wardriving`` beacon, a chatty public relay. Muting a channel here means its new messages
stop raising the unread badge: they no longer bump the per-conversation unread count (so the
channel list, the conversation picker, and the header's unread total all skip them), while the
transcript is still written to history so the conversation is all there when you open it. The
opposite of muting is the default — every channel notifies until you say otherwise.

The mute is keyed by the channel's *intrinsic identity* (see
:func:`~meshterm.core.channels.channel_identity`), the same slot-independent key its chat
history and unread counter use — never a slot index — so a muted channel stays muted after it
is reordered to another slot, and two devices that share a channel's key share the mute. Like
the other operator preferences (remembered devices, watched nodes, admin passwords), this is
global machine state in a small JSON file (``<config_dir>/mutes.json``) rather than the
per-invocation SQLite database. Reads are served from memory after the first load — the chat
recorder consults it on every inbound channel message and the channel list on every repaint —
and the rare, user-driven writes are flushed immediately and atomically.
"""

from __future__ import annotations

import json
from pathlib import Path


class MuteStore:
    """Reads and writes the set of channels whose notifications are muted, memory-first.

    Interact through :meth:`is_muted` (the hot-path check), :meth:`set_muted` (the toggle),
    and :meth:`muted` (the whole set). The backing set is loaded once on first access and kept
    in memory thereafter; each mutation persists the whole set atomically.
    """

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on the first mute).
        """
        self._path = path
        self._muted: set[str] | None = None

    @property
    def _state(self) -> set[str]:
        """The muted-channel-identity set, loaded from disk on first access."""
        if self._muted is None:
            self._muted = self._load()
        return self._muted

    def _load(self) -> set[str]:
        """Parse the file into a set of channel identities, or empty on missing/corrupt file."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(data, dict):
            return set()
        return {str(x) for x in data.get("muted", []) if isinstance(x, str)}

    def is_muted(self, channel_id: str | None) -> bool:
        """Whether the channel with this intrinsic identity has its notifications muted.

        Args:
            channel_id: The channel's identity (``None`` — an unresolved channel — is never
                muted, so a message on it always notifies).

        Returns:
            ``True`` if the channel is muted.
        """
        return channel_id is not None and channel_id in self._state

    def muted(self) -> set[str]:
        """The muted channel identities (a copy, safe for the caller to keep)."""
        return set(self._state)

    def set_muted(self, channel_id: str, muted: bool) -> None:
        """Mute or unmute a channel's notifications, persisting only a real change.

        Args:
            channel_id: The channel's intrinsic identity.
            muted: ``True`` to mute, ``False`` to restore notifications.
        """
        if muted == (channel_id in self._state):
            return  # already in the requested state; nothing to write
        if muted:
            self._state.add(channel_id)
        else:
            self._state.discard(channel_id)
        self._save()

    def _save(self) -> None:
        """Persist the whole muted set atomically (a crash mid-write keeps the old file)."""
        data = {"muted": sorted(self._state)}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)
