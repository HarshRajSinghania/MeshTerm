"""SQLite database bootstrap and schema.

The schema is intentionally generic: a central ``runs`` table records every tool
execution (with arguments and a JSON summary), and measurement tables reference it so
new tools can persist data without schema churn.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Small key/value store for UI state that should survive across sessions (e.g. the last
-- map viewport), independent of any tool run. New keys need no schema change.
CREATE TABLE IF NOT EXISTS app_state (
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
    node_type   INTEGER,          -- advert type (repeater/chat/room/...), when carried
    snr         REAL,
    rssi        REAL,
    lat         REAL,
    lon         REAL,
    observed_at TEXT    NOT NULL
);

-- One chat message, sent or received, on a channel or with a contact. Unlike the other
-- measurement tables these are not tied to a single tool run (they arrive unsolicited via
-- the event hub), so run_id is nullable and detaches rather than cascades.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    outbound    INTEGER NOT NULL DEFAULT 0,
    is_channel  INTEGER NOT NULL DEFAULT 0,
    channel_id  TEXT,             -- a channel's slot-independent identity (its history key)
    channel_idx INTEGER,          -- the slot it went out on / arrived on (reference only)
    peer        TEXT,
    peer_name   TEXT,
    text        TEXT    NOT NULL,
    snr         REAL,
    acked       INTEGER,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traces_run ON traces(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_hops_trace ON trace_hops(trace_id);
CREATE INDEX IF NOT EXISTS idx_tx_samples_run ON tx_samples(run_id);
CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_observations_node ON observations(node);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer);
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
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database's tables up to the current schema.

    ``executescript(_SCHEMA)`` only *creates* missing tables (``IF NOT EXISTS``); it cannot
    add a column to a table an older version already created. Column additions are applied
    here, guarded so the migration is idempotent and safe to run on every open. Indexes on
    added columns are created here too (not in ``_SCHEMA``), so ``executescript`` never
    references a column an older database hasn't grown yet.
    """
    message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "channel_id" not in message_cols:
        # v3 -> v4: channel history moved from being keyed by slot index to a
        # slot-independent channel identity. Existing rows are backfilled lazily at runtime
        # (see Repository.backfill_channel_ids) once the device's channels can be read.
        conn.execute("ALTER TABLE messages ADD COLUMN channel_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel_id)"
    )

    observation_cols = {row["name"] for row in conn.execute("PRAGMA table_info(observations)")}
    if "node_type" not in observation_cols:
        # v4 -> v5: observations gained the transmitting node's advert type, so the map can
        # tell repeaters from leaf nodes. Older rows simply carry NULL (type unknown).
        conn.execute("ALTER TABLE observations ADD COLUMN node_type INTEGER")
