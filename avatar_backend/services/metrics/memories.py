"""Long-term memory persistence mixin for MetricsDB."""
from __future__ import annotations
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_CATEGORY_CLASS: dict[str, str] = {
    'people': 'profile', 'household': 'profile', 'location': 'profile', 'device': 'profile',
    'preference': 'preference', 'comfort': 'preference', 'media': 'preference', 'travel': 'preference',
    'policy': 'policy', 'security': 'policy',
    'routine': 'episodic', 'general': 'episodic',
}

class MemoriesMixin:

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
        source_detail: str = "",
        confidence: float = 0.5,
        pinned: bool = False,
        expires_ts: str | None = None,
    ) -> dict:
        summary = " ".join(summary.split()).strip()
        category = (category or "general").strip().lower()[:40] or "general"
        source = (source or "chat").strip().lower()[:40] or "chat"
        confidence = max(0.0, min(float(confidence), 1.0))
        now = datetime.now(timezone.utc).isoformat()
        fp = self._memory_fingerprint(summary, category)
        mem_class = _CATEGORY_CLASS.get(category, 'episodic')

        with self._write_lock, self._write_conn as conn:
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
                     confidence, times_seen, pinned, fingerprint, expires_ts)
                    VALUES (?, ?, NULL, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (now, now, category, summary, source, confidence, 1 if pinned else 0, fp, expires_ts),
                )
                # Ensure new columns exist before writing them
                cols = {r[1] for r in conn.execute('PRAGMA table_info(long_term_memories)').fetchall()}
                if 'source_detail' in cols:
                    conn.execute(
                        'UPDATE long_term_memories SET source_detail=?, mem_class=? WHERE fingerprint=?',
                        (source_detail or '', mem_class, fp),
                    )
            out = conn.execute(
                "SELECT * FROM long_term_memories WHERE fingerprint = ?",
                (fp,),
            ).fetchone()
        return dict(out) if out else {}

    def ensure_memory_columns(self) -> None:
        """Idempotent migration: add stale/expires_ts/superseded_by/source_detail/mem_class if missing."""
        with self._write_lock, self._write_conn as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(long_term_memories)").fetchall()}
            if 'stale' not in cols:
                conn.execute("ALTER TABLE long_term_memories ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")
            if 'expires_ts' not in cols:
                conn.execute("ALTER TABLE long_term_memories ADD COLUMN expires_ts TEXT")
            if 'superseded_by' not in cols:
                conn.execute("ALTER TABLE long_term_memories ADD COLUMN superseded_by INTEGER")
            if 'source_detail' not in cols:
                conn.execute("ALTER TABLE long_term_memories ADD COLUMN source_detail TEXT NOT NULL DEFAULT ''")
            if 'mem_class' not in cols:
                conn.execute("ALTER TABLE long_term_memories ADD COLUMN mem_class TEXT NOT NULL DEFAULT 'episodic'")
                # Back-fill existing rows
                for cat, cls in _CATEGORY_CLASS.items():
                    conn.execute("UPDATE long_term_memories SET mem_class=? WHERE category=?", (cls, cat))

    def log_memory_usage(self, memory_ids: list[int], query: str, session_id: str = "") -> None:
        if not memory_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        q_excerpt = (query or "")[:120]
        with self._write_lock, self._write_conn as conn:
            for mid in memory_ids:
                conn.execute(
                    "INSERT INTO memory_usage_log (ts, memory_id, query, session_id) VALUES (?,?,?,?)",
                    (now, int(mid), q_excerpt, session_id or ""),
                )

    def list_memory_usage(self, memory_id: int, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, query, session_id FROM memory_usage_log WHERE memory_id=? ORDER BY id DESC LIMIT ?",
                (int(memory_id), limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_stale(self, memory_id: int, superseded_by: int | None = None) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._write_conn as conn:
            cur = conn.execute(
                "UPDATE long_term_memories SET stale=1, updated_ts=?, superseded_by=COALESCE(?,superseded_by) WHERE id=?",
                (now, superseded_by, int(memory_id)),
            )
            return cur.rowcount > 0

    def restore_memory(self, memory_id: int) -> bool:
        with self._connect() as conn:
            conn.execute(
                "UPDATE long_term_memories SET stale=0, superseded_by=NULL, updated_ts=? WHERE id=?",
                (datetime.utcnow().isoformat(), memory_id),
            )
            return conn.execute("SELECT changes()").fetchone()[0] > 0

    def expire_stale_memories(self) -> int:
        """Mark expired memories as stale. Called on startup and periodically."""
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._write_conn as conn:
            cur = conn.execute(
                "UPDATE long_term_memories SET stale=1 WHERE expires_ts IS NOT NULL AND expires_ts < ? AND stale=0",
                (now,),
            )
            return cur.rowcount

    def list_memories(self, limit: int = 200, include_stale: bool = False) -> list[dict]:
        sql = """
        SELECT * FROM long_term_memories
        WHERE (? OR stale = 0)
        ORDER BY pinned DESC, updated_ts DESC, id DESC
        LIMIT ?
        """
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, (1 if include_stale else 0, limit)).fetchall()]

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

        with self._write_lock, self._write_conn as conn:
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
        with self._write_lock, self._write_conn as conn:
            cur = conn.execute("DELETE FROM long_term_memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def clear_memories(self) -> int:
        with self._write_lock, self._write_conn as conn:
            cur = conn.execute("DELETE FROM long_term_memories")
            return cur.rowcount

    def mark_memories_referenced(self, memory_ids: list[int]) -> None:
        ids = [int(i) for i in memory_ids if str(i).isdigit()]
        if not ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in ids)
        with self._write_lock, self._write_conn as conn:
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
        with self._write_lock:
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
