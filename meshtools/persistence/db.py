"""SQLite database bootstrap and schema.

The schema is intentionally generic: a central ``runs`` table records every tool
execution (with arguments and a JSON summary), and measurement tables reference it so
new tools can persist data without schema churn.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per tool execution; the spine that measurements hang off of.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool        TEXT    NOT NULL,
    profile     TEXT,
    args_json   TEXT    NOT NULL DEFAULT '{}',
    status      TEXT    NOT NULL DEFAULT 'running',  -- running | ok | error
    summary_json TEXT,
    started_at  TEXT    NOT NULL,
    finished_at TEXT
);

-- Aggregated outcome of a single path trace, linked to its run.
CREATE TABLE IF NOT EXISTS traces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target        TEXT    NOT NULL,
    success       INTEGER NOT NULL,
    hop_count     INTEGER NOT NULL DEFAULT 0,
    min_snr       REAL,
    round_trip_ms REAL,
    tx_power      INTEGER,
    created_at    TEXT    NOT NULL
);

-- Per-hop SNR for a trace.
CREATE TABLE IF NOT EXISTS trace_hops (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id  INTEGER NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    hop_index INTEGER NOT NULL,
    node      TEXT,
    snr       REAL    NOT NULL
);

-- One robust sample per (tx_power) tried during TX-power optimization.
CREATE TABLE IF NOT EXISTS tx_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target        TEXT    NOT NULL,
    tx_power      INTEGER NOT NULL,
    median_min_snr REAL,
    success_rate  REAL,
    samples       INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);

-- A candidate path evaluated during path optimization.
CREATE TABLE IF NOT EXISTS path_candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target        TEXT    NOT NULL,
    path_json     TEXT    NOT NULL,
    bottleneck_snr REAL,
    success_rate  REAL,
    median_rtt_ms REAL,
    created_at    TEXT    NOT NULL
);

-- One packet overheard while passively monitoring the mesh (advert/telemetry/...).
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node        TEXT,
    name        TEXT,
    kind        TEXT    NOT NULL DEFAULT 'advert',
    snr         REAL,
    rssi        REAL,
    lat         REAL,
    lon         REAL,
    observed_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traces_run ON traces(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_hops_trace ON trace_hops(trace_id);
CREATE INDEX IF NOT EXISTS idx_tx_samples_run ON tx_samples(run_id);
CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_observations_node ON observations(node);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database and ensure the schema exists.

    Args:
        db_path: Filesystem location of the database. Parent directories are created.

    Returns:
        An open connection with ``Row`` factory and foreign keys enabled.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn
