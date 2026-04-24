"""
MetricsDB base — SQLite persistence core: connection management, schema, init.
"""
from __future__ import annotations
import sqlite3
import threading
from pathlib import Path
from avatar_backend.services.open_loop_service import OpenLoopService
from avatar_backend.runtime_paths import data_dir

_DB_PATH: Path | None = None


def _default_db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = data_dir() / "metrics.db"
    return _DB_PATH
_SCHEMA  = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS llm_invocations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,          -- ISO-8601 UTC
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    purpose       TEXT    NOT NULL DEFAULT 'chat',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0.0,
    elapsed_ms    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_invocations(ts);

CREATE TABLE IF NOT EXISTS system_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    cpu_pct    REAL,
    ram_used   INTEGER,   -- bytes
    ram_total  INTEGER,   -- bytes
    disk_used  INTEGER,   -- bytes
    disk_total INTEGER,   -- bytes
    gpu_util   REAL,
    gpu_mem_used  INTEGER,
    gpu_mem_total INTEGER,
    ollama_gpu_pct REAL
);

CREATE INDEX IF NOT EXISTS idx_sys_ts ON system_samples(ts);

CREATE TABLE IF NOT EXISTS decision_events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,
    kind  TEXT NOT NULL,
    data  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_dec_ts ON decision_events(ts);

CREATE TABLE IF NOT EXISTS server_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,          -- ISO-8601 UTC
    level   TEXT NOT NULL,          -- info / warning / error / debug / critical
    event   TEXT NOT NULL,          -- the log message
    logger  TEXT NOT NULL DEFAULT '',
    data    TEXT NOT NULL DEFAULT '{}'  -- JSON of extra fields
);
CREATE INDEX IF NOT EXISTS idx_log_ts    ON server_logs(ts);
CREATE INDEX IF NOT EXISTS idx_log_level ON server_logs(level);

CREATE TABLE IF NOT EXISTS long_term_memories (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts         TEXT NOT NULL,
    updated_ts         TEXT NOT NULL,
    last_referenced_ts TEXT,
    category           TEXT NOT NULL DEFAULT 'general',
    summary            TEXT NOT NULL,
    source             TEXT NOT NULL DEFAULT 'chat',
    confidence         REAL NOT NULL DEFAULT 0.5,
    times_seen         INTEGER NOT NULL DEFAULT 1,
    pinned             INTEGER NOT NULL DEFAULT 0,
    fingerprint        TEXT NOT NULL UNIQUE,
    stale              INTEGER NOT NULL DEFAULT 0,
    expires_ts         TEXT,
    superseded_by      INTEGER REFERENCES long_term_memories(id)
);
CREATE TABLE IF NOT EXISTS parental_tool_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    tool       TEXT NOT NULL,
    args       TEXT,
    success    INTEGER NOT NULL DEFAULT 1,
    message    TEXT
);
CREATE TABLE IF NOT EXISTS parental_overrides (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts  TEXT NOT NULL,
    updated_ts  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    subject     TEXT NOT NULL,
    resource    TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    duration_m  INTEGER NOT NULL DEFAULT 30,
    requested_by TEXT NOT NULL DEFAULT 'nova',
    resolved_by  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mem_updated   ON long_term_memories(updated_ts);
CREATE INDEX IF NOT EXISTS idx_mem_category  ON long_term_memories(category);
CREATE INDEX IF NOT EXISTS idx_mem_referenced ON long_term_memories(last_referenced_ts);

CREATE TABLE IF NOT EXISTS motion_clips (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    camera_entity_id  TEXT NOT NULL,
    trigger_entity_id TEXT NOT NULL DEFAULT '',
    location          TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    video_relpath     TEXT NOT NULL DEFAULT '',
    thumb_relpath     TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'ready',
    duration_s        INTEGER NOT NULL DEFAULT 0,
    flagged           INTEGER NOT NULL DEFAULT 0,
    llm_provider      TEXT NOT NULL DEFAULT '',
    llm_model         TEXT NOT NULL DEFAULT '',
    extra_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_motion_ts ON motion_clips(ts);
CREATE INDEX IF NOT EXISTS idx_motion_camera_ts ON motion_clips(camera_entity_id, ts);
CREATE INDEX IF NOT EXISTS idx_motion_flagged ON motion_clips(flagged);

CREATE TABLE IF NOT EXISTS event_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    event_id          TEXT NOT NULL DEFAULT '',
    event_type        TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'active',
    event_source      TEXT NOT NULL DEFAULT '',
    camera_entity_id  TEXT NOT NULL DEFAULT '',
    data_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_history_ts ON event_history(ts);
CREATE INDEX IF NOT EXISTS idx_event_history_event_id ON event_history(event_id);

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL UNIQUE,
    event_type        TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT '',
    room              TEXT NOT NULL DEFAULT '',
    camera_entity_id  TEXT NOT NULL DEFAULT '',
    severity          TEXT NOT NULL DEFAULT 'normal',
    summary           TEXT NOT NULL DEFAULT '',
    details           TEXT NOT NULL DEFAULT '',
    confidence        REAL,
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL DEFAULT '',
    linked_session_id TEXT NOT NULL DEFAULT '',
    data_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_camera ON events(camera_entity_id);

CREATE TABLE IF NOT EXISTS event_actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    action_id         TEXT NOT NULL,
    action_type       TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'completed',
    result_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_actions_event_id ON event_actions(event_id);
CREATE INDEX IF NOT EXISTS idx_event_actions_ts ON event_actions(ts);

CREATE TABLE IF NOT EXISTS event_media (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL,
    media_type        TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL DEFAULT '',
    metadata_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_event_media_event_id ON event_media(event_id);

CREATE TABLE IF NOT EXISTS health_checks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    component TEXT NOT NULL,
    status    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_component_ts ON health_checks(component, ts);

CREATE TABLE IF NOT EXISTS conversation_audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    session_id       TEXT    NOT NULL DEFAULT '',
    user_text        TEXT    NOT NULL DEFAULT '',
    context_summary  TEXT    NOT NULL DEFAULT '',
    llm_response     TEXT    NOT NULL DEFAULT '',
    tool_calls_json  TEXT    NOT NULL DEFAULT '[]',
    final_reply      TEXT    NOT NULL DEFAULT '',
    processing_ms    INTEGER NOT NULL DEFAULT 0,
    model            TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_conv_audit_session ON conversation_audit(session_id);
CREATE INDEX IF NOT EXISTS idx_conv_audit_ts ON conversation_audit(ts);

CREATE TABLE IF NOT EXISTS conversation_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL UNIQUE,
    started_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    surface           TEXT NOT NULL DEFAULT '',
    linked_event_id   TEXT NOT NULL DEFAULT '',
    metadata_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_conversation_sessions_updated_at ON conversation_sessions(updated_at);

CREATE TABLE IF NOT EXISTS conversation_turn_summaries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    ts                TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT '',
    summary           TEXT NOT NULL DEFAULT '',
    event_id          TEXT NOT NULL DEFAULT '',
    metadata_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_conversation_turn_summaries_session_id ON conversation_turn_summaries(session_id);
CREATE INDEX IF NOT EXISTS idx_conversation_turn_summaries_ts ON conversation_turn_summaries(ts);
"""


class MetricsDBBase:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = _default_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._open_loop_service = OpenLoopService()

        # Dedicated write connection — serializes all writes without blocking reads
        self._write_conn = sqlite3.connect(self._path, timeout=30)
        self._write_conn.row_factory = sqlite3.Row
        self._write_conn.execute("PRAGMA journal_mode=WAL")
        self._write_conn.execute("PRAGMA busy_timeout=5000")
        self._write_lock = threading.Lock()

        self._init_db()
        self.ensure_overrides_table()

    def _conn(self) -> sqlite3.Connection:
        """Return a connection for read operations. WAL allows concurrent reads."""
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a single write statement on the dedicated write connection."""
        with self._write_lock:
            cur = self._write_conn.execute(sql, params)
            self._write_conn.commit()
            return cur

    def _write_many(self, statements: list[tuple[str, tuple]]) -> None:
        """Execute multiple write statements in a single transaction."""
        with self._write_lock:
            for sql, params in statements:
                self._write_conn.execute(sql, params)
            self._write_conn.commit()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._path, timeout=10)
        try:
            # Run column migrations FIRST so executescript can safely create
            # indexes on columns that may not exist in older databases.
            for col, typedef in [
                ("thumb_relpath", "TEXT NOT NULL DEFAULT ''"),
                ("flagged", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE motion_clips ADD COLUMN {col} {typedef}")
                    conn.commit()
                except sqlite3.OperationalError:
                    pass  # column already exists or table not yet created
            conn.executescript(_SCHEMA)
        finally:
            conn.close()
