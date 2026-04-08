"""
MetricsDB — SQLite persistence for LLM cost and system metrics.

Tables:
  llm_invocations  — one row per LLM call (immutable)
  system_samples   — one row per metrics poll (CPU/RAM/disk/GPU) — kept 7 days
  long_term_memories — stable household memories Nova can reuse across restarts
"""
from __future__ import annotations
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_DB_PATH = Path("/opt/avatar-server/data/metrics.db")
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
"""


class MetricsDB:
    def __init__(self, path: Path = _DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._lock = threading.Lock()
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
