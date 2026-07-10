"""Repository: the single gateway between the app and the database.

Tools and services never issue SQL directly; they call typed methods here. This keeps
persistence concerns in one place and makes the storage backend swappable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from ..core.models import (
    ChatMessage,
    HeardNode,
    Hop,
    Observation,
    TraceResult,
    TxLevelResult,
    utcnow,
)
from . import db


#: The trailing window (days) a channel's recent-activity tally is computed over. One week
#: smooths out the day-to-day lulls of a hobbyist mesh while still going quiet within days
#: of a channel actually dying down, which is the granularity the activity meter shows.
ACTIVITY_WINDOW_DAYS = 7


@dataclass(slots=True)
class ChannelStats:
    """Aggregated message history for one channel conversation.

    Attributes:
        total: Messages ever stored for the channel, sent and received alike.
        recent: Messages within the trailing :data:`ACTIVITY_WINDOW_DAYS` window — the
            basis of the channel manager's activity meter.
        last_at: When the channel's most recent message was stored, or ``None`` if the
            stored timestamp can't be parsed.
    """

    total: int
    recent: int
    last_at: Optional[datetime]


@dataclass(slots=True)
class RunRecord:
    """A summary row from the ``runs`` table.

    Attributes:
        id: Primary key of the run.
        tool: Name of the tool that executed.
        profile: Device profile used, if any.
        status: ``running``, ``ok``, or ``error``.
        started_at: ISO-8601 start timestamp.
        finished_at: ISO-8601 finish timestamp, or ``None`` if still running.
        summary: Decoded JSON summary, or ``None``.
    """

    id: int
    tool: str
    profile: Optional[str]
    status: str
    started_at: str
    finished_at: Optional[str]
    summary: Optional[dict]


class Repository:
    """Typed data-access layer over the SQLite database."""

    def __init__(self, db_path: Path) -> None:
        """Open the repository against a database file.

        Args:
            db_path: Location of the SQLite database (created if absent).
        """
        self._conn = db.connect(db_path)

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    # -- runs -------------------------------------------------------------------

    def start_run(
        self, tool: str, args: dict[str, Any], profile: Optional[str] = None
    ) -> int:
        """Record the start of a tool execution.

        Args:
            tool: Tool name.
            args: Arguments the tool was invoked with (must be JSON-serializable).
            profile: Active device profile name, if any.

        Returns:
            The new run's primary key.
        """
        cur = self._conn.execute(
            "INSERT INTO runs (tool, profile, args_json, status, started_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (tool, profile, json.dumps(args, default=str), utcnow().isoformat()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: Optional[dict] = None) -> None:
        """Mark a run as finished.

        Args:
            run_id: The run to update.
            status: Terminal status (``ok`` or ``error``).
            summary: Optional JSON-serializable result summary.
        """
        self._conn.execute(
            "UPDATE runs SET status = ?, summary_json = ?, finished_at = ? WHERE id = ?",
            (
                status,
                json.dumps(summary, default=str) if summary is not None else None,
                utcnow().isoformat(),
                run_id,
            ),
        )
        self._conn.commit()

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        """Return the most recent runs, newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of :class:`RunRecord`.
        """
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        """Map a ``runs`` row to a :class:`RunRecord`."""
        return RunRecord(
            id=row["id"],
            tool=row["tool"],
            profile=row["profile"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            summary=json.loads(row["summary_json"]) if row["summary_json"] else None,
        )

    # -- traces -----------------------------------------------------------------

    def record_trace(self, run_id: int, trace: TraceResult) -> int:
        """Persist a single trace and its per-hop SNR readings.

        Args:
            run_id: The owning run.
            trace: The trace result to store.

        Returns:
            The new trace's primary key.
        """
        cur = self._conn.execute(
            "INSERT INTO traces "
            "(run_id, target, success, hop_count, min_snr, round_trip_ms, tx_power, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                trace.target,
                int(trace.success),
                trace.hop_count,
                trace.min_snr,
                trace.round_trip_ms,
                trace.tx_power,
                trace.timestamp.isoformat(),
            ),
        )
        trace_id = int(cur.lastrowid)
        self._conn.executemany(
            "INSERT INTO trace_hops (trace_id, hop_index, node, snr) VALUES (?, ?, ?, ?)",
            [(trace_id, h.index, h.node, h.snr) for h in trace.hops],
        )
        self._conn.commit()
        return trace_id

    def latest_trace(
        self,
        target: str,
        *,
        exclude_run_id: Optional[int] = None,
        success_only: bool = True,
    ) -> Optional[TraceResult]:
        """Return the most recently recorded trace to ``target``, rehydrated with hops.

        Used to show the previous run's result before a new trace starts.

        Args:
            target: The trace destination to look up.
            exclude_run_id: A run to skip (typically the in-progress one) so we surface
                a genuinely prior result.
            success_only: When ``True``, ignore traces that timed out.

        Returns:
            The latest matching :class:`TraceResult`, or ``None`` if none is stored.
        """
        sql = "SELECT * FROM traces WHERE target = ?"
        params: list[Any] = [target]
        if success_only:
            sql += " AND success = 1"
        if exclude_run_id is not None:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None

        hop_rows = self._conn.execute(
            "SELECT hop_index, node, snr FROM trace_hops WHERE trace_id = ? "
            "ORDER BY hop_index",
            (row["id"],),
        ).fetchall()
        hops = [Hop(index=h["hop_index"], node=h["node"], snr=h["snr"]) for h in hop_rows]
        return TraceResult(
            target=row["target"],
            success=bool(row["success"]),
            hops=hops,
            round_trip_ms=row["round_trip_ms"],
            tx_power=row["tx_power"],
            timestamp=datetime.fromisoformat(row["created_at"]),
        )

    def recent_traces(self, target: str, *, limit: int = 200) -> list[TraceResult]:
        """Return recent traces to ``target``, newest first, rehydrated with hops.

        This is the history the link-quality baseline is computed over, so both timed-out
        and successful traces are included (a rise in failures is itself a regression).

        Args:
            target: The trace destination to load.
            limit: Maximum number of traces to return.

        Returns:
            The matching :class:`TraceResult` objects, newest first.
        """
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE target = ? ORDER BY id DESC LIMIT ?",
            (target, limit),
        ).fetchall()
        return [self._hydrate_trace(row) for row in rows]

    def all_traces(self, *, limit: int = 500) -> list[TraceResult]:
        """Return recent traces across every target, newest first, rehydrated with hops.

        Used to build a whole-mesh route graph rather than a single target's history.

        Args:
            limit: Maximum number of traces to return.

        Returns:
            The matching :class:`TraceResult` objects, newest first.
        """
        rows = self._conn.execute(
            "SELECT * FROM traces ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._hydrate_trace(row) for row in rows]

    def _hydrate_trace(self, row: sqlite3.Row) -> TraceResult:
        """Rebuild a :class:`TraceResult` (with hops) from a ``traces`` row.

        Args:
            row: A ``traces`` table row.

        Returns:
            The rehydrated trace.
        """
        hop_rows = self._conn.execute(
            "SELECT hop_index, node, snr FROM trace_hops WHERE trace_id = ? ORDER BY hop_index",
            (row["id"],),
        ).fetchall()
        hops = [Hop(index=h["hop_index"], node=h["node"], snr=h["snr"]) for h in hop_rows]
        return TraceResult(
            target=row["target"],
            success=bool(row["success"]),
            hops=hops,
            round_trip_ms=row["round_trip_ms"],
            tx_power=row["tx_power"],
            timestamp=datetime.fromisoformat(row["created_at"]),
        )

    def traced_targets(self) -> list[str]:
        """Return the distinct trace destinations recorded, most-traced first.

        Returns:
            Target names ordered by descending trace count.
        """
        rows = self._conn.execute(
            "SELECT target, COUNT(*) AS n FROM traces GROUP BY target ORDER BY n DESC"
        ).fetchall()
        return [row["target"] for row in rows]

    def record_tx_sample(self, run_id: int, level: TxLevelResult) -> None:
        """Persist one robust TX-power level from an optimization sweep.

        The ``median_min_snr`` column stores this optimizer's headline metric — the
        median SNR measured *at the target* — rather than a path bottleneck.

        Args:
            run_id: The owning run.
            level: Aggregated result for a single TX power level.
        """
        self._conn.execute(
            "INSERT INTO tx_samples "
            "(run_id, target, tx_power, median_min_snr, success_rate, samples, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                level.stats.target,
                level.tx_power,
                level.target_snr,
                level.success_rate,
                level.samples,
                utcnow().isoformat(),
            ),
        )
        self._conn.commit()

    # -- observations (passive monitoring) --------------------------------------

    def record_observation(self, run_id: int, obs: Observation) -> None:
        """Persist one overheard packet from a monitoring run.

        Args:
            run_id: The owning run.
            obs: The observation to store.
        """
        self._conn.execute(
            "INSERT INTO observations "
            "(run_id, node, name, kind, node_type, snr, rssi, lat, lon, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                obs.node,
                obs.name,
                obs.kind,
                obs.node_type,
                obs.snr,
                obs.rssi,
                obs.lat,
                obs.lon,
                obs.observed_at.isoformat(),
            ),
        )
        self._conn.commit()

    def observation_count(self) -> int:
        """Return the total number of overheard packets stored across every run.

        Backs the monitor's "total" counter, so it counts all observations ever logged,
        not just the current session's.

        Returns:
            The row count of the ``observations`` table.
        """
        row = self._conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()
        return int(row["n"]) if row else 0

    def heard_nodes(self, *, since: Optional[datetime] = None) -> list[HeardNode]:
        """Aggregate stored observations into per-node reception statistics.

        Spans every monitoring run (optionally limited to recent history), so the result
        is a longitudinal view of which nodes have been heard, how strongly, and where —
        the substrate for the monitor summary and the coverage map.

        Args:
            since: Only include observations at or after this time, if given.

        Returns:
            One :class:`HeardNode` per distinct node, ordered by most-recently heard.
        """
        sql = "SELECT node, name, node_type, snr, rssi, lat, lon, observed_at FROM observations"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE observed_at >= ?"
            params.append(since.isoformat())
        rows = self._conn.execute(sql, params).fetchall()

        grouped: dict[Optional[str], list[Observation]] = {}
        for row in rows:
            obs = Observation(
                node=row["node"],
                name=row["name"],
                node_type=row["node_type"],
                snr=row["snr"],
                rssi=row["rssi"],
                lat=row["lat"],
                lon=row["lon"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
            )
            grouped.setdefault(row["node"], []).append(obs)

        nodes = [HeardNode.from_observations(node, obs) for node, obs in grouped.items()]
        return sorted(nodes, key=lambda n: n.last_seen, reverse=True)

    # -- chat messages ----------------------------------------------------------

    def record_chat_message(
        self, msg: ChatMessage, *, run_id: Optional[int] = None
    ) -> int:
        """Persist one chat message (sent or received).

        Args:
            msg: The message to store. Its ``peer`` is normalized to lowercase so a
                direct conversation queries back consistently.
            run_id: The owning background ``chat`` run, if any (inbound messages log to
                one; outbound sends may not).

        Returns:
            The new message's primary key.
        """
        peer = msg.peer.lower() if msg.peer else None
        cur = self._conn.execute(
            "INSERT INTO messages "
            "(run_id, outbound, is_channel, channel_id, channel_idx, peer, peer_name, text, "
            "snr, acked, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                int(msg.outbound),
                int(msg.is_channel),
                msg.channel_id,
                msg.channel_idx,
                peer,
                msg.peer_name,
                msg.text,
                msg.snr,
                None if msg.acked is None else int(msg.acked),
                msg.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_chat_ack(self, message_id: int, acked: bool) -> None:
        """Update one outbound message's delivery acknowledgement (used on retry).

        Args:
            message_id: The ``messages`` row to update.
            acked: The new delivery state — ``True`` acknowledged, ``False`` not.
        """
        self._conn.execute(
            "UPDATE messages SET acked = ? WHERE id = ?", (int(acked), message_id)
        )
        self._conn.commit()

    def recent_chat_messages(
        self,
        *,
        is_channel: bool,
        channel_id: Optional[str] = None,
        peer: Optional[str] = None,
        limit: int = 200,
    ) -> list[ChatMessage]:
        """Return a conversation's most recent messages, oldest-first.

        Args:
            is_channel: Whether to load a channel conversation.
            channel_id: The channel's slot-independent identity (channel conversations).
            peer: The contact key prefix (direct conversations).
            limit: Maximum number of messages to return.

        Returns:
            The messages in chronological order (ready to render as a transcript).
        """
        if is_channel:
            where, params = "is_channel = 1 AND channel_id = ?", [channel_id]
        else:
            where, params = "is_channel = 0 AND peer = ?", [(peer or "").lower()]
        rows = self._conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [self._row_to_chat(row) for row in reversed(rows)]

    def last_chat_messages(self) -> dict[str, ChatMessage]:
        """Return the latest message per conversation, keyed by conversation key.

        Backs the conversation picker's preview snippets. One row per distinct
        conversation, taken as the highest-id (most recent) message in each.

        Returns:
            A mapping of :func:`~meshterm.core.models.conversation_key` to its latest
            :class:`ChatMessage`.
        """
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE id IN ("
            "  SELECT MAX(id) FROM messages GROUP BY "
            "  CASE WHEN is_channel = 1 THEN 'chan:' || channel_id "
            "       ELSE 'dm:' || peer END)"
        ).fetchall()
        return {msg.key: msg for msg in (self._row_to_chat(r) for r in rows)}

    def channel_stats(self) -> dict[str, ChannelStats]:
        """Aggregate stored channel messages into per-channel statistics.

        Backs the channel manager's list lanes: each configured channel's row shows its
        total message count, an activity meter over the trailing
        :data:`ACTIVITY_WINDOW_DAYS`, and the age of its last message. One SQL pass groups
        every channel message by the channel's intrinsic identity, so the cost stays flat
        no matter how many channels the device carries.

        Timestamps are compared as strings: every ``created_at`` is written by
        ``utcnow().isoformat()`` (a fixed-width UTC ISO-8601 form), so lexicographic order
        *is* chronological order and the recent-window cutoff needs no per-row parsing.
        Messages predating identity-keyed history (a ``NULL`` ``channel_id``; see
        :meth:`backfill_channel_ids`) have no channel to be counted under and are skipped.

        Returns:
            A mapping of channel identity to its :class:`ChannelStats`. Channels with no
            stored messages simply have no entry.
        """
        cutoff = (utcnow() - timedelta(days=ACTIVITY_WINDOW_DAYS)).isoformat()
        rows = self._conn.execute(
            "SELECT channel_id, COUNT(*) AS total, MAX(created_at) AS last_at, "
            "SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS recent "
            "FROM messages WHERE is_channel = 1 AND channel_id IS NOT NULL "
            "GROUP BY channel_id",
            (cutoff,),
        ).fetchall()
        stats: dict[str, ChannelStats] = {}
        for row in rows:
            try:
                last_at = datetime.fromisoformat(row["last_at"])
            except (TypeError, ValueError):
                last_at = None  # a malformed stray must not hide the channel's counts
            stats[row["channel_id"]] = ChannelStats(
                total=int(row["total"]),
                recent=int(row["recent"] or 0),
                last_at=last_at,
            )
        return stats

    def backfill_channel_ids(self, mapping: dict[int, str]) -> int:
        """Give legacy channel messages an identity, keyed by the slot they were stored on.

        Messages written before channel history was keyed by identity have a ``NULL``
        ``channel_id``. There is no record of which channel occupied each slot back then, so
        the best available guess is the channel *currently* at that slot. ``mapping`` maps a
        slot index to the identity of the channel now there; only rows still missing an
        identity are touched, so this is safe to run on every startup.

        Args:
            mapping: Slot index to the current channel identity at that slot.

        Returns:
            The number of legacy rows given an identity.
        """
        changed = 0
        for idx, channel_id in mapping.items():
            cur = self._conn.execute(
                "UPDATE messages SET channel_id = ? "
                "WHERE is_channel = 1 AND channel_id IS NULL AND channel_idx = ?",
                (channel_id, idx),
            )
            changed += cur.rowcount
        if changed:
            self._conn.commit()
        return changed

    # -- persisted UI state -----------------------------------------------------

    def get_map_view(self) -> Optional[tuple[float, float, int]]:
        """Return the last saved map viewport as ``(center_lat, center_lon, zoom)``.

        Lets the interactive map reopen exactly where the user left it. Returns ``None``
        when no view has been saved yet, or a stored value can't be parsed (treated as
        absent rather than an error, so a corrupt row just refits to the nodes).
        """
        row = self._conn.execute(
            "SELECT value FROM app_state WHERE key = 'map_view'"
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["value"])
            return float(data["lat"]), float(data["lon"]), int(data["zoom"])
        except (ValueError, KeyError, TypeError):
            return None

    def set_map_view(self, lat: float, lon: float, zoom: int) -> None:
        """Persist the map viewport so the next session reopens on the same spot.

        Args:
            lat: Latitude at the centre of the view.
            lon: Longitude at the centre of the view.
            zoom: Display zoom level.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO app_state(key, value) VALUES ('map_view', ?)",
            (json.dumps({"lat": lat, "lon": lon, "zoom": zoom}),),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_chat(row: sqlite3.Row) -> ChatMessage:
        """Rebuild a :class:`ChatMessage` from a ``messages`` row."""
        return ChatMessage(
            text=row["text"],
            outbound=bool(row["outbound"]),
            is_channel=bool(row["is_channel"]),
            channel_id=row["channel_id"],
            channel_idx=row["channel_idx"],
            peer=row["peer"],
            peer_name=row["peer_name"],
            snr=row["snr"],
            acked=None if row["acked"] is None else bool(row["acked"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            row_id=row["id"],
        )
