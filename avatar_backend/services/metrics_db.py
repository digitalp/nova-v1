"""
MetricsDB — SQLite persistence for LLM cost and system metrics.

Tables:
  llm_invocations  — one row per LLM call (immutable)
  system_samples   — one row per metrics poll (CPU/RAM/disk/GPU) — kept 7 days
  long_term_memories — stable household memories Nova can reuse across restarts
  motion_clips     — archived motion-triggered video clips + AI descriptions
"""
from __future__ import annotations
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from avatar_backend.services.open_loop_service import OpenLoopService
from avatar_backend.runtime_paths import data_dir

_DB_PATH = data_dir() / "metrics.db"
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
    fingerprint        TEXT NOT NULL UNIQUE
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
    status            TEXT NOT NULL DEFAULT 'ready',
    duration_s        INTEGER NOT NULL DEFAULT 0,
    llm_provider      TEXT NOT NULL DEFAULT '',
    llm_model         TEXT NOT NULL DEFAULT '',
    extra_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_motion_ts ON motion_clips(ts);
CREATE INDEX IF NOT EXISTS idx_motion_camera_ts ON motion_clips(camera_entity_id, ts);

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


class MetricsDB:
    def __init__(self, path: Path = _DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._lock = threading.Lock()
        self._open_loop_service = OpenLoopService()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── LLM invocations ───────────────────────────────────────────────────────

    def insert_invocation(self, entry: dict) -> None:
        sql = """INSERT INTO llm_invocations
                 (ts, provider, model, purpose, input_tokens, output_tokens, cost_usd, elapsed_ms)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(sql, (
                ts,
                entry.get("provider", ""),
                entry.get("model", ""),
                entry.get("purpose", "chat"),
                entry.get("input_tokens", 0),
                entry.get("output_tokens", 0),
                entry.get("cost_usd", 0.0),
                entry.get("elapsed_ms", 0),
            ))

    def cost_summary(self, period: str = "month") -> dict:
        """Return aggregated cost + token totals for the given period."""
        now = datetime.now(timezone.utc)
        if period == "day":
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            since = now - timedelta(days=now.weekday())
            since = since.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "month":
            since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            since = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            since = now - timedelta(hours=24)

        sql = """SELECT COUNT(*) calls,
                        COALESCE(SUM(input_tokens),0)  input_tokens,
                        COALESCE(SUM(output_tokens),0) output_tokens,
                        COALESCE(SUM(cost_usd),0)      cost_usd
                 FROM llm_invocations WHERE ts >= ?"""
        with self._conn() as conn:
            row = conn.execute(sql, (since.isoformat(),)).fetchone()
            return dict(row) if row else {}

    def cost_by_day(self, days: int = 30) -> list[dict]:
        """Return daily cost totals for the last N days."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sql = """SELECT strftime('%Y-%m-%d', ts) day,
                        COUNT(*) calls,
                        SUM(input_tokens)  input_tokens,
                        SUM(output_tokens) output_tokens,
                        SUM(cost_usd)      cost_usd
                 FROM llm_invocations WHERE ts >= ?
                 GROUP BY day ORDER BY day"""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (since,)).fetchall()]

    def cost_by_model(self, period: str = "month") -> list[dict]:
        """Return cost breakdown by model for the given period."""
        summary = self.cost_summary.__wrapped__ if hasattr(self.cost_summary, '__wrapped__') else None
        now = datetime.now(timezone.utc)
        if period == "month":
            since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            since = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            since = now - timedelta(hours=24)
        sql = """SELECT provider, model,
                        COUNT(*) calls,
                        SUM(input_tokens)  input_tokens,
                        SUM(output_tokens) output_tokens,
                        SUM(cost_usd)      cost_usd
                 FROM llm_invocations WHERE ts >= ?
                 GROUP BY provider, model ORDER BY cost_usd DESC"""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (since.isoformat(),)).fetchall()]

    def monthly_totals(self, months: int = 12) -> list[dict]:
        """Return monthly cost totals for the last N months."""
        since = (datetime.now(timezone.utc) - timedelta(days=months * 31)).isoformat()
        sql = """SELECT strftime('%Y-%m', ts) month,
                        COUNT(*) calls,
                        SUM(input_tokens)  input_tokens,
                        SUM(output_tokens) output_tokens,
                        SUM(cost_usd)      cost_usd
                 FROM llm_invocations WHERE ts >= ?
                 GROUP BY month ORDER BY month"""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (since,)).fetchall()]

    def recent_invocations(self, n: int = 200) -> list[dict]:
        """Return the most recent persisted LLM invocation rows."""
        sql = """SELECT ts, provider, model, purpose, input_tokens, output_tokens,
                        cost_usd, elapsed_ms
                 FROM llm_invocations
                 ORDER BY id DESC
                 LIMIT ?"""
        with self._conn() as conn:
            rows = conn.execute(sql, (n,)).fetchall()
        out: list[dict] = []
        for row in reversed(rows):
            entry = dict(row)
            ts = entry.get("ts", "")
            if isinstance(ts, str) and len(ts) >= 19:
                entry["ts"] = ts[11:19]
            out.append(entry)
        return out

    # ── System samples ────────────────────────────────────────────────────────

    def insert_sample(self, s: dict) -> None:
        sql = """INSERT INTO system_samples
                 (ts, cpu_pct, ram_used, ram_total, disk_used, disk_total,
                  gpu_util, gpu_mem_used, gpu_mem_total, ollama_gpu_pct)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(sql, (
                ts,
                s.get("cpu_pct"),
                s.get("ram_used"),
                s.get("ram_total"),
                s.get("disk_used"),
                s.get("disk_total"),
                s.get("gpu_util"),
                s.get("gpu_mem_used"),
                s.get("gpu_mem_total"),
                s.get("ollama_gpu_pct"),
            ))

    def recent_samples(self, minutes: int = 60) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        sql = "SELECT * FROM system_samples WHERE ts >= ? ORDER BY ts"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (since,)).fetchall()]

    def latest_sample(self) -> dict | None:
        sql = "SELECT * FROM system_samples ORDER BY id DESC LIMIT 1"
        with self._conn() as conn:
            row = conn.execute(sql).fetchone()
            return dict(row) if row else None

    def hourly_averages(self, hours: int = 24) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        sql = """SELECT strftime('%Y-%m-%dT%H:00:00Z', ts) hour,
                        AVG(cpu_pct) cpu_pct,
                        AVG(ram_used) ram_used, MAX(ram_total) ram_total,
                        AVG(gpu_util) gpu_util,
                        AVG(gpu_mem_used) gpu_mem_used, MAX(gpu_mem_total) gpu_mem_total
                 FROM system_samples WHERE ts >= ?
                 GROUP BY hour ORDER BY hour"""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (since,)).fetchall()]

    # ── Decision events ──────────────────────────────────────────────────────

    def insert_decision(self, entry: dict) -> None:
        import json as _json
        data = dict(entry)
        ts   = data.pop("ts", datetime.now(timezone.utc).strftime("%H:%M:%S"))
        kind = data.pop("kind", "unknown")
        full_ts = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO decision_events (ts, kind, data) VALUES (?, ?, ?)",
                (full_ts, kind, _json.dumps(data)),
            )

    def recent_decisions(self, n: int = 200) -> list[dict]:
        import json as _json
        sql = ("SELECT ts, kind, data FROM decision_events "
               "ORDER BY id DESC LIMIT ?")
        with self._conn() as conn:
            rows = conn.execute(sql, (n,)).fetchall()
        out = []
        for r in reversed(rows):
            entry = _json.loads(r["data"])
            entry["ts"]   = r["ts"][11:19]   # HH:MM:SS from ISO timestamp
            entry["kind"] = r["kind"]
            out.append(entry)
        return out

    # ── Event history ───────────────────────────────────────────────────────

    def insert_event_history(self, entry: dict[str, Any]) -> None:
        import json as _json

        ts = entry.get("ts") or datetime.now(timezone.utc).isoformat()
        data = dict(entry)
        payload = data.pop("data", {})
        payload = self._open_loop_service.enrich_event_data(
            ts=ts,
            status=str(data.get("status", "active")),
            data=payload,
            open_loop_note=(payload or {}).get("open_loop_note"),
            admin_note=(payload or {}).get("admin_note"),
        )
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO event_history
                (ts, event_id, event_type, title, summary, status, event_source, camera_entity_id, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    data.get("event_id", ""),
                    data.get("event_type", ""),
                    data.get("title", ""),
                    data.get("summary", ""),
                    data.get("status", "active"),
                    data.get("event_source", ""),
                    data.get("camera_entity_id", ""),
                    _json.dumps(payload or {}),
                ),
            )

    def recent_event_history(self, n: int = 100) -> list[dict[str, Any]]:
        import json as _json

        sql = """
        SELECT ts, event_id, event_type, title, summary, status, event_source, camera_entity_id, data_json
        FROM event_history
        ORDER BY id DESC
        LIMIT ?
        """
        with self._conn() as conn:
            rows = conn.execute(sql, (n,)).fetchall()
        out: list[dict[str, Any]] = []
        for row in reversed(rows):
            entry = dict(row)
            entry["data"] = _json.loads(entry.pop("data_json", "{}") or "{}")
            out.append(entry)
        return out

    def update_event_history_status(
        self,
        event_id: str,
        status: str,
        open_loop_note: str | None = None,
        admin_note: str | None = None,
    ) -> bool:
        import json as _json

        if not event_id:
            return False
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, data_json FROM event_history WHERE event_id = ?",
                (event_id,),
            ).fetchall()
            if not rows:
                return False
            for row in rows:
                data = _json.loads(row["data_json"] or "{}")
                data = self._open_loop_service.apply_status_transition(
                    status=status,
                    data=data,
                    open_loop_note=open_loop_note,
                    admin_note=admin_note,
                )
                conn.execute(
                    "UPDATE event_history SET status = ?, data_json = ? WHERE id = ?",
                    (status, _json.dumps(data), row["id"]),
                )
        return True

    def update_event_history_policy(
        self,
        event_id: str,
        *,
        reminder_sent: bool = False,
        escalation_level: str | None = None,
    ) -> bool:
        import json as _json

        if not event_id or (not reminder_sent and not escalation_level):
            return False
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, data_json FROM event_history WHERE event_id = ?",
                (event_id,),
            ).fetchall()
            if not rows:
                return False
            for row in rows:
                data = _json.loads(row["data_json"] or "{}")
                data = self._open_loop_service.apply_policy_update(
                    data=data,
                    reminder_sent=reminder_sent,
                    escalation_level=escalation_level,
                )
                conn.execute(
                    "UPDATE event_history SET data_json = ? WHERE id = ?",
                    (_json.dumps(data), row["id"]),
                )
        return True

    # ── Canonical event store ───────────────────────────────────────────────

    def insert_event_record(self, entry: dict[str, Any]) -> None:
        import json as _json

        event_id = str(entry.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("event_id is required")
        created_at = str(entry.get("created_at") or datetime.now(timezone.utc).isoformat())
        payload = dict(entry.get("data") or {})
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events
                (event_id, event_type, source, room, camera_entity_id, severity, summary, details,
                 confidence, status, created_at, expires_at, linked_session_id, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    entry.get("event_type", ""),
                    entry.get("source", ""),
                    entry.get("room", ""),
                    entry.get("camera_entity_id", ""),
                    entry.get("severity", "normal"),
                    entry.get("summary", ""),
                    entry.get("details", ""),
                    entry.get("confidence"),
                    entry.get("status", "active"),
                    created_at,
                    entry.get("expires_at", ""),
                    entry.get("linked_session_id", ""),
                    _json.dumps(payload),
                ),
            )

    def get_event_record(self, event_id: str) -> dict[str, Any] | None:
        import json as _json

        if not event_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT event_id, event_type, source, room, camera_entity_id, severity, summary, details,
                       confidence, status, created_at, expires_at, linked_session_id, data_json
                FROM events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if not row:
            return None
        entry = dict(row)
        entry["data"] = _json.loads(entry.pop("data_json", "{}") or "{}")
        return entry

    def list_event_records(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        status: str | None = None,
        source: str | None = None,
        camera_entity_id: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> list[dict[str, Any]]:
        import json as _json

        clauses: list[str] = []
        args: list[Any] = []
        if event_type:
            clauses.append("event_type = ?")
            args.append(event_type)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if source:
            clauses.append("source = ?")
            args.append(source)
        if camera_entity_id:
            clauses.append("camera_entity_id = ?")
            args.append(camera_entity_id)
        if created_after:
            clauses.append("created_at >= ?")
            args.append(created_after)
        if created_before:
            clauses.append("created_at <= ?")
            args.append(created_before)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(max(1, min(limit, 500)))
        sql = f"""
        SELECT event_id, event_type, source, room, camera_entity_id, severity, summary, details,
               confidence, status, created_at, expires_at, linked_session_id, data_json
        FROM events
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ?
        """
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            entry["data"] = _json.loads(entry.pop("data_json", "{}") or "{}")
            out.append(entry)
        return out

    def update_event_record_status(
        self,
        event_id: str,
        *,
        status: str,
        open_loop_note: str | None = None,
        admin_note: str | None = None,
        reminder_sent: bool = False,
        escalation_level: str | None = None,
    ) -> bool:
        import json as _json

        if not event_id:
            return False
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT data_json FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if not row:
                return False
            data = _json.loads(row["data_json"] or "{}")
            created_row = conn.execute(
                "SELECT created_at FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            fallback_ts = str(created_row["created_at"] if created_row else "") or datetime.now(timezone.utc).isoformat()
            data.setdefault("open_loop_started_ts", fallback_ts)
            data = self._open_loop_service.apply_status_transition(
                status=status,
                data=data,
                open_loop_note=open_loop_note,
                admin_note=admin_note,
            )
            data = self._open_loop_service.apply_policy_update(
                data=data,
                reminder_sent=reminder_sent,
                escalation_level=escalation_level,
            )
            conn.execute(
                "UPDATE events SET status = ?, data_json = ? WHERE event_id = ?",
                (status, _json.dumps(data), event_id),
            )
        return True

    def insert_event_action(
        self,
        *,
        event_id: str,
        action_id: str,
        action_type: str,
        status: str = "completed",
        result: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        import json as _json

        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO event_actions (ts, event_id, action_id, action_type, status, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ts or datetime.now(timezone.utc).isoformat(),
                    event_id,
                    action_id,
                    action_type,
                    status,
                    _json.dumps(result or {}),
                ),
            )

    def list_event_actions(self, event_id: str) -> list[dict[str, Any]]:
        import json as _json

        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT ts, event_id, action_id, action_type, status, result_json
                FROM event_actions
                WHERE event_id = ?
                ORDER BY id ASC
                """,
                (event_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            entry["result"] = _json.loads(entry.pop("result_json", "{}") or "{}")
            out.append(entry)
        return out

    def insert_event_media(
        self,
        *,
        event_id: str,
        media_type: str,
        url: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        import json as _json

        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO event_media (event_id, media_type, url, metadata_json)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, media_type, url, _json.dumps(metadata or {})),
            )

    def list_event_media(self, event_id: str) -> list[dict[str, Any]]:
        import json as _json

        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT event_id, media_type, url, metadata_json
                FROM event_media
                WHERE event_id = ?
                ORDER BY id ASC
                """,
                (event_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            entry["metadata"] = _json.loads(entry.pop("metadata_json", "{}") or "{}")
            out.append(entry)
        return out

    def upsert_conversation_session(
        self,
        *,
        session_id: str,
        surface: str = "",
        linked_event_id: str = "",
        metadata: dict[str, Any] | None = None,
        now_iso: str | None = None,
    ) -> None:
        import json as _json

        ts = now_iso or datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO conversation_sessions
                (session_id, started_at, updated_at, surface, linked_event_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    surface=excluded.surface,
                    linked_event_id=excluded.linked_event_id,
                    metadata_json=excluded.metadata_json
                """,
                (session_id, ts, ts, surface, linked_event_id, _json.dumps(metadata or {})),
            )

    def insert_conversation_turn_summary(
        self,
        *,
        session_id: str,
        role: str,
        summary: str,
        event_id: str = "",
        metadata: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> None:
        import json as _json

        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turn_summaries
                (session_id, ts, role, summary, event_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    ts or datetime.now(timezone.utc).isoformat(),
                    role,
                    summary,
                    event_id,
                    _json.dumps(metadata or {}),
                ),
            )

    # ── Server logs ──────────────────────────────────────────────────────────────────────────

    def insert_log(self, entry: dict) -> None:
        import json as _json
        data = dict(entry)
        ts     = data.pop("ts", datetime.now(timezone.utc).isoformat())
        level  = data.pop("level", "info")
        event  = data.pop("event", "")
        logger = data.pop("logger", "")
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO server_logs (ts, level, event, logger, data) VALUES (?, ?, ?, ?, ?)",
                (ts, level, event, logger, _json.dumps(data)),
            )

    def recent_logs(self, n: int = 500, level: str | None = None) -> list[dict]:
        import json as _json
        if level:
            sql  = "SELECT ts, level, event, logger, data FROM server_logs WHERE level=? ORDER BY id DESC LIMIT ?"
            args = (level, n)
        else:
            sql  = "SELECT ts, level, event, logger, data FROM server_logs ORDER BY id DESC LIMIT ?"
            args = (n,)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for r in reversed(rows):
            extra = _json.loads(r["data"])
            entry = {"ts": r["ts"][11:19], "level": r["level"], "event": r["event"], "logger": r["logger"]}
            entry.update(extra)
            out.append(entry)
        return out

    def purge_old_logs(self, keep_days: int = 7) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM server_logs WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def purge_old_decisions(self, keep_days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM decision_events WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def purge_old_samples(self, keep_days: int = 7) -> int:
        """Delete system samples older than keep_days. Returns rows deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM system_samples WHERE ts < ?", (cutoff,))
            return cur.rowcount

    # ── Persistent long-term memory ────────────────────────────────────────

    @staticmethod
    def _memory_fingerprint(summary: str, category: str) -> str:
        normalized = " ".join(summary.lower().split())
        return hashlib.sha1(f"{category}:{normalized}".encode("utf-8")).hexdigest()

    def upsert_memory(
        self,
        *,
        summary: str,
        category: str = "general",
        source: str = "chat",
        confidence: float = 0.5,
        pinned: bool = False,
    ) -> dict:
        summary = " ".join(summary.split()).strip()
        category = (category or "general").strip().lower()[:40] or "general"
        source = (source or "chat").strip().lower()[:40] or "chat"
        confidence = max(0.0, min(float(confidence), 1.0))
        now = datetime.now(timezone.utc).isoformat()
        fp = self._memory_fingerprint(summary, category)

        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM long_term_memories WHERE fingerprint = ?",
                (fp,),
            ).fetchone()
            if row:
                merged_conf = max(float(row["confidence"] or 0.0), confidence)
                conn.execute(
                    """
                    UPDATE long_term_memories
                    SET updated_ts = ?,
                        source = ?,
                        confidence = ?,
                        times_seen = times_seen + 1,
                        pinned = CASE WHEN pinned = 1 OR ? THEN 1 ELSE 0 END
                    WHERE fingerprint = ?
                    """,
                    (now, source, merged_conf, 1 if pinned else 0, fp),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO long_term_memories
                    (created_ts, updated_ts, last_referenced_ts, category, summary, source,
                     confidence, times_seen, pinned, fingerprint)
                    VALUES (?, ?, NULL, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (now, now, category, summary, source, confidence, 1 if pinned else 0, fp),
                )
            out = conn.execute(
                "SELECT * FROM long_term_memories WHERE fingerprint = ?",
                (fp,),
            ).fetchone()
        return dict(out) if out else {}

    def list_memories(self, limit: int = 200) -> list[dict]:
        sql = """
        SELECT * FROM long_term_memories
        ORDER BY pinned DESC, updated_ts DESC, id DESC
        LIMIT ?
        """
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]

    def update_memory(
        self,
        memory_id: int,
        *,
        summary: str,
        category: str = "general",
        confidence: float = 0.5,
        pinned: bool = False,
    ) -> dict | None:
        summary = " ".join(summary.split()).strip()
        if not summary:
            return None
        category = (category or "general").strip().lower()[:40] or "general"
        confidence = max(0.0, min(float(confidence), 1.0))
        now = datetime.now(timezone.utc).isoformat()
        fp = self._memory_fingerprint(summary, category)

        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM long_term_memories WHERE id = ?",
                (int(memory_id),),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE long_term_memories
                SET updated_ts = ?,
                    category = ?,
                    summary = ?,
                    confidence = ?,
                    pinned = ?,
                    fingerprint = ?
                WHERE id = ?
                """,
                (
                    now,
                    category,
                    summary,
                    confidence,
                    1 if pinned else 0,
                    fp,
                    int(memory_id),
                ),
            )
            out = conn.execute(
                "SELECT * FROM long_term_memories WHERE id = ?",
                (int(memory_id),),
            ).fetchone()
        return dict(out) if out else None

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM long_term_memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def clear_memories(self) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM long_term_memories")
            return cur.rowcount

    def mark_memories_referenced(self, memory_ids: list[int]) -> None:
        ids = [int(i) for i in memory_ids if str(i).isdigit()]
        if not ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._conn() as conn:
            conn.execute(
                f"UPDATE long_term_memories SET last_referenced_ts = ? WHERE id IN ({placeholders})",
                (now, *ids),
            )

    def import_memories_from(self, other_db_path: str) -> int:
        path = (other_db_path or "").strip()
        if not path:
            return 0
        other_path = Path(path)
        if not other_path.exists():
            return 0
        if str(other_path.resolve()) == str(Path(self._path).resolve()):
            return 0

        imported = 0
        with self._lock:
            src = sqlite3.connect(str(other_path), timeout=10)
            src.row_factory = sqlite3.Row
            try:
                rows = src.execute(
                    """
                    SELECT created_ts, updated_ts, last_referenced_ts, category, summary, source,
                           confidence, times_seen, pinned, fingerprint
                    FROM long_term_memories
                    ORDER BY id ASC
                    """
                ).fetchall()
            finally:
                src.close()

            if not rows:
                return 0

            with self._conn() as conn:
                for row in rows:
                    existing = conn.execute(
                        "SELECT * FROM long_term_memories WHERE fingerprint = ?",
                        (row["fingerprint"],),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            """
                            UPDATE long_term_memories
                            SET created_ts = MIN(created_ts, ?),
                                updated_ts = MAX(updated_ts, ?),
                                last_referenced_ts = CASE
                                    WHEN last_referenced_ts IS NULL THEN ?
                                    WHEN ? IS NULL THEN last_referenced_ts
                                    WHEN last_referenced_ts < ? THEN ?
                                    ELSE last_referenced_ts
                                END,
                                source = CASE
                                    WHEN source = '' THEN ?
                                    ELSE source
                                END,
                                confidence = MAX(confidence, ?),
                                times_seen = MAX(times_seen, ?),
                                pinned = CASE
                                    WHEN pinned = 1 OR ? THEN 1
                                    ELSE 0
                                END
                            WHERE fingerprint = ?
                            """,
                            (
                                row["created_ts"],
                                row["updated_ts"],
                                row["last_referenced_ts"],
                                row["last_referenced_ts"],
                                row["last_referenced_ts"],
                                row["last_referenced_ts"],
                                row["source"],
                                float(row["confidence"] or 0.0),
                                int(row["times_seen"] or 1),
                                1 if row["pinned"] else 0,
                                row["fingerprint"],
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO long_term_memories
                            (created_ts, updated_ts, last_referenced_ts, category, summary, source,
                             confidence, times_seen, pinned, fingerprint)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["created_ts"],
                                row["updated_ts"],
                                row["last_referenced_ts"],
                                row["category"],
                                row["summary"],
                                row["source"],
                                float(row["confidence"] or 0.0),
                                int(row["times_seen"] or 1),
                                1 if row["pinned"] else 0,
                                row["fingerprint"],
                            ),
                        )
                        imported += 1
        return imported

    # ── Motion clips ────────────────────────────────────────────────────────

    @staticmethod
    def _attach_motion_clip_event_fields(entry: dict[str, Any]) -> dict[str, Any]:
        extra = entry.get("extra") or {}
        canonical = extra.get("canonical_event") if isinstance(extra, dict) else None
        if isinstance(canonical, dict):
            entry["canonical_event_id"] = canonical.get("event_id", "")
            entry["canonical_event_type"] = canonical.get("event_type", "")
            entry["canonical_event"] = canonical
        else:
            entry["canonical_event_id"] = ""
            entry["canonical_event_type"] = ""
        return entry

    def insert_motion_clip(self, entry: dict[str, Any]) -> int:
        import json as _json

        sql = """
        INSERT INTO motion_clips
        (ts, camera_entity_id, trigger_entity_id, location, description,
         video_relpath, status, duration_s, llm_provider, llm_model, extra_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        ts = entry.get("ts") or datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as conn:
            cur = conn.execute(sql, (
                ts,
                entry.get("camera_entity_id", ""),
                entry.get("trigger_entity_id", ""),
                entry.get("location", ""),
                entry.get("description", ""),
                entry.get("video_relpath", ""),
                entry.get("status", "ready"),
                int(entry.get("duration_s", 0) or 0),
                entry.get("llm_provider", ""),
                entry.get("llm_model", ""),
                _json.dumps(entry.get("extra", {}) or {}),
            ))
            return int(cur.lastrowid)

    def recent_motion_clips(
        self,
        *,
        limit: int = 100,
        date: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        camera_entity_id: str | None = None,
        canonical_event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        import json as _json

        sql = """
        SELECT id, ts, camera_entity_id, trigger_entity_id, location, description,
               video_relpath, status, duration_s, llm_provider, llm_model, extra_json
        FROM motion_clips
        WHERE 1=1
        """
        args: list[Any] = []
        if camera_entity_id:
            sql += " AND camera_entity_id = ?"
            args.append(camera_entity_id)
        if date:
            sql += " AND substr(ts, 1, 10) = ?"
            args.append(date)
        if start_time:
            sql += " AND substr(ts, 12, 5) >= ?"
            args.append(start_time[:5])
        if end_time:
            sql += " AND substr(ts, 12, 5) <= ?"
            args.append(end_time[:5])
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))

        with self._conn() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            entry["extra"] = _json.loads(entry.pop("extra_json", "{}") or "{}")
            out.append(self._attach_motion_clip_event_fields(entry))
        if canonical_event_type:
            wanted = canonical_event_type.strip()
            out = [entry for entry in out if str(entry.get("canonical_event_type") or "") == wanted]
        return out

    def get_motion_clip(self, clip_id: int) -> dict[str, Any] | None:
        import json as _json

        sql = """
        SELECT id, ts, camera_entity_id, trigger_entity_id, location, description,
               video_relpath, status, duration_s, llm_provider, llm_model, extra_json
        FROM motion_clips
        WHERE id = ?
        """
        with self._conn() as conn:
            row = conn.execute(sql, (clip_id,)).fetchone()
        if not row:
            return None
        entry = dict(row)
        entry["extra"] = _json.loads(entry.pop("extra_json", "{}") or "{}")
        return self._attach_motion_clip_event_fields(entry)

    def delete_motion_clip(self, clip_id: int) -> str | None:
        """Delete a single motion clip. Returns video_relpath so the caller can remove the file."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT video_relpath FROM motion_clips WHERE id = ?", (clip_id,)
            ).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM motion_clips WHERE id = ?", (clip_id,))
        return row["video_relpath"] or None

    def delete_motion_clips_bulk(self, clip_ids: list[int]) -> list[str]:
        """Delete multiple clips by ID. Returns list of video_relpaths for file cleanup."""
        if not clip_ids:
            return []
        placeholders = ",".join("?" * len(clip_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT id, video_relpath FROM motion_clips WHERE id IN ({placeholders})",
                clip_ids,
            ).fetchall()
            conn.execute(
                f"DELETE FROM motion_clips WHERE id IN ({placeholders})", clip_ids
            )
        return [row["video_relpath"] for row in rows if row["video_relpath"]]

    def delete_all_motion_clips(self) -> list[str]:
        """Delete every motion clip row. Returns all video_relpaths for file cleanup."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT video_relpath FROM motion_clips WHERE video_relpath != ''"
            ).fetchall()
            conn.execute("DELETE FROM motion_clips")
        return [row["video_relpath"] for row in rows]
