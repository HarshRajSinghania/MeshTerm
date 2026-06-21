"""Repository: the single gateway between the app and the database.

Tools and services never issue SQL directly; they call typed methods here. This keeps
persistence concerns in one place and makes the storage backend swappable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from datetime import datetime

from ..core.models import Hop, TraceResult, TxLevelResult, utcnow
from . import db


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
