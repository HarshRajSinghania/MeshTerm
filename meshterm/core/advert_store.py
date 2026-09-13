# SPDX-License-Identifier: Apache-2.0
"""Persistence for the background-advert schedule.

MeshTerm announces the connected node on a cadence — a zero-hop (direct) advert for the
immediate neighbourhood and a flood advert for the wider mesh — so the node stays fresh in
other nodes' contact lists without anyone thinking about it. This store remembers, per
device (keyed by its public key), the chosen cadence for each advert type and when one was
last sent, so the countdown survives restarts and *every* advert counts: a manual send from
the advert menu resets the same clock the scheduler reads (see
:class:`~meshterm.services.advert_scheduler.AdvertScheduler`).

Like the remembered devices (:mod:`meshterm.core.device_store`), this is global machine
state in a small JSON file (``<config_dir>/adverts.json``) rather than the per-invocation
SQLite database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .atomicwrite import write_atomically
from .models import utcnow

#: The direct (zero-hop) cadence choices offered in the editor, in hours.
DIRECT_CADENCE_HOURS = (1, 2, 3, 4)

#: The flood cadence choices offered in the editor, in hours (3 h .. 1 week).
FLOOD_CADENCE_HOURS = (3, 6, 12, 24, 48, 168)

#: Defaults applied when a device has no stored policy: announce to neighbours hourly,
#: flood the wider mesh daily.
DEFAULT_DIRECT_HOURS = 1
DEFAULT_FLOOD_HOURS = 24

#: The stored cadence meaning "this advert type is off" (0 hours).
OFF = 0


@dataclass(slots=True)
class AdvertPolicy:
    """One device's background-advert schedule and its last-sent marks.

    Attributes:
        direct_hours: Hours between zero-hop adverts (:data:`OFF` disables them).
        flood_hours: Hours between flood adverts (:data:`OFF` disables them).
        last_direct: When a zero-hop advert (scheduled *or* manual) last went out.
        last_flood: When a flood advert last went out.
    """

    direct_hours: int = DEFAULT_DIRECT_HOURS
    flood_hours: int = DEFAULT_FLOOD_HOURS
    last_direct: datetime | None = None
    last_flood: datetime | None = None

    def cadence(self, flood: bool) -> int:
        """The cadence in hours for one advert type (:data:`OFF` when disabled)."""
        return self.flood_hours if flood else self.direct_hours

    def last_sent(self, flood: bool) -> datetime | None:
        """When an advert of one type last went out, or ``None`` if never recorded."""
        return self.last_flood if flood else self.last_direct

    def due(self, flood: bool, now: datetime | None = None) -> bool:
        """Whether an advert of one type is due at ``now``.

        Never-sent is *not* due: the scheduler arms the clock by recording "now" on its
        first look (see :meth:`AdvertStore.arm`), so a fresh install waits a full cadence
        instead of transmitting the moment it starts — adverts are routine, not urgent,
        and the quiet default is the polite one on a shared mesh.

        Args:
            flood: ``True`` for the flood clock, ``False`` for the zero-hop one.
            now: The current time (defaults to :func:`~meshterm.core.models.utcnow`).

        Returns:
            ``True`` when the type is enabled, armed, and its cadence has elapsed.
        """
        hours = self.cadence(flood)
        last = self.last_sent(flood)
        if hours == OFF or last is None:
            return False
        return ((now or utcnow()) - last).total_seconds() >= hours * 3600


def cadence_label(hours: int) -> str:
    """Render a cadence for display: ``"off"``, ``"every hour"``, ``"every 2 h"``, ``"weekly"``."""
    if hours == OFF:
        return "off"
    if hours == 1:
        return "every hour"
    if hours == 24:
        return "daily"
    if hours == 168:
        return "weekly"
    return f"every {hours} h"


class AdvertStore:
    """Reads and writes per-device background-advert schedules."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path

    @staticmethod
    def _key(public_key: str) -> str:
        """Normalize a device public key into the storage key."""
        return public_key.lower().removeprefix("0x")

    def _load_all(self) -> dict[str, dict]:
        """Return the raw key -> record mapping, or empty on missing/corrupt file."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def load(self, public_key: str) -> AdvertPolicy:
        """Return the stored policy for a device, or the on-by-default policy.

        Args:
            public_key: The device's public key (hex).

        Returns:
            The device's :class:`AdvertPolicy`; defaults apply for anything unset.
        """
        record = self._load_all().get(self._key(public_key))
        if not isinstance(record, dict):
            return AdvertPolicy()
        return AdvertPolicy(
            direct_hours=_as_hours(record.get("direct_hours"), DEFAULT_DIRECT_HOURS),
            flood_hours=_as_hours(record.get("flood_hours"), DEFAULT_FLOOD_HOURS),
            last_direct=_as_time(record.get("last_direct")),
            last_flood=_as_time(record.get("last_flood")),
        )

    def set_cadence(self, public_key: str, *, flood: bool, hours: int) -> None:
        """Store one advert type's cadence for a device.

        Args:
            public_key: The device's public key (hex).
            flood: ``True`` to set the flood cadence, ``False`` for the zero-hop one.
            hours: The new cadence in hours (:data:`OFF` disables the type).
        """
        self._update(public_key, {"flood_hours" if flood else "direct_hours": int(hours)})

    def mark_sent(self, public_key: str, *, flood: bool, when: datetime | None = None) -> None:
        """Record that an advert went out, resetting that type's countdown.

        Called for scheduled *and* manual sends alike, so the next background advert
        counts from the most recent announcement of that type, whoever triggered it.

        Args:
            public_key: The device's public key (hex).
            flood: Which advert type went out.
            when: The send time (defaults to now).
        """
        stamp = (when or utcnow()).isoformat()
        self._update(public_key, {"last_flood" if flood else "last_direct": stamp})

    def arm(self, public_key: str, *, flood: bool, when: datetime | None = None) -> None:
        """Start a never-sent advert type's countdown from ``when`` without sending.

        Only writes when no last-sent mark exists, so it is safe to call on every
        scheduler pass.

        Args:
            public_key: The device's public key (hex).
            flood: Which advert type to arm.
            when: The countdown start (defaults to now).
        """
        if self.load(public_key).last_sent(flood) is None:
            self.mark_sent(public_key, flood=flood, when=when)

    def _update(self, public_key: str, fields: dict) -> None:
        """Merge ``fields`` into a device's record and persist the store."""
        records = self._load_all()
        key = self._key(public_key)
        record = records.get(key)
        if not isinstance(record, dict):
            record = {}
        record.update(fields)
        records[key] = record
        self._write(records)

    def _write(self, records: dict[str, dict]) -> None:
        """Persist ``records`` atomically (crash mid-write keeps the previous file)."""
        write_atomically(self._path, json.dumps(records, indent=2))


def _as_hours(value: object, default: int) -> int:
    """Coerce a stored cadence to a non-negative int, falling back to ``default``."""
    try:
        hours = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return hours if hours >= 0 else default


def _as_time(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or ``None`` if absent/corrupt.

    A timestamp without a zone is treated as corrupt too: the store only ever writes
    aware UTC stamps, and a naive one would poison the ``due`` arithmetic.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
