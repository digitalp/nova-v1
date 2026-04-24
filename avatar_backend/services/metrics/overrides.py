"""Parental override queue persistence mixin for MetricsDB."""
from __future__ import annotations
from datetime import datetime, timezone


class OverridesMixin:

    def ensure_overrides_table(self) -> None:
        with self._write_lock, self._write_conn as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS parental_overrides (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_ts   TEXT NOT NULL,
                    updated_ts   TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    subject      TEXT NOT NULL,
                    resource     TEXT NOT NULL DEFAULT '',
                    reason       TEXT NOT NULL DEFAULT '',
                    duration_m   INTEGER NOT NULL DEFAULT 30,
                    requested_by TEXT NOT NULL DEFAULT 'nova',
                    resolved_by  TEXT NOT NULL DEFAULT ''
                )
            """)

    def add_override_request(self, *, subject: str, resource: str = "",
                             reason: str = "", duration_m: int = 30,
                             requested_by: str = "nova") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._write_conn as conn:
            cur = conn.execute(
                """INSERT INTO parental_overrides
                   (created_ts, updated_ts, status, subject, resource, reason, duration_m, requested_by)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)""",
                (now, now, subject, resource, reason, int(duration_m), requested_by),
            )
            row = conn.execute(
                "SELECT * FROM parental_overrides WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return dict(row) if row else {}

    def list_overrides(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            sql = "SELECT * FROM parental_overrides WHERE status = ? ORDER BY created_ts DESC LIMIT ?"
            args = (status, limit)
        else:
            sql = "SELECT * FROM parental_overrides ORDER BY created_ts DESC LIMIT ?"
            args = (limit,)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def resolve_override(self, override_id: int, *, status: str, resolved_by: str) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._write_conn as conn:
            conn.execute(
                "UPDATE parental_overrides SET status=?, updated_ts=?, resolved_by=? WHERE id=?",
                (status, now, resolved_by, int(override_id)),
            )
            row = conn.execute(
                "SELECT * FROM parental_overrides WHERE id = ?", (int(override_id),)
            ).fetchone()
        return dict(row) if row else None

    def ensure_parental_audit_table(self) -> None:
        with self._write_lock, self._write_conn as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS parental_tool_audit (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         TEXT NOT NULL,
                    tool       TEXT NOT NULL,
                    args       TEXT,
                    success    INTEGER NOT NULL DEFAULT 1,
                    message    TEXT
                )
            """)

    def log_parental_tool(self, tool: str, args: dict, success: bool, message: str) -> None:
        from datetime import datetime as _dt
        import json as _json
        with self._write_lock, self._write_conn as conn:
            conn.execute(
                "INSERT INTO parental_tool_audit (ts, tool, args, success, message) VALUES (?,?,?,?,?)",
                (
                    _dt.utcnow().isoformat(),
                    tool,
                    _json.dumps(args, default=str)[:500],
                    1 if success else 0,
                    str(message)[:400],
                ),
            )

    def list_parental_audit(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM parental_tool_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

