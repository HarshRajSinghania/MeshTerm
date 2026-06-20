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

from ..core.models import TraceResult, TraceStats, utcnow
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

    def record_tx_sample(self, run_id: int, stats: TraceStats) -> None:
        """Persist one robust TX-power sample from an optimization sweep.

        Args:
            run_id: The owning run.
            stats: Aggregated trace statistics for a single TX power level.
        """
        self._conn.execute(
            "INSERT INTO tx_samples "
            "(run_id, target, tx_power, median_min_snr, success_rate, samples, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                stats.target,
                stats.tx_power,
                stats.median_min_snr,
                stats.success_rate,
                stats.samples,
                utcnow().isoformat(),
            ),
        )
        self._conn.commit()
