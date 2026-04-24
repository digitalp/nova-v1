"""Per-child state machine persistence mixin for MetricsDB."""
from __future__ import annotations
from datetime import datetime, timezone


class ChildStatesMixin:

    def get_child_state(self, person_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM child_states WHERE person_id = ?", (person_id,)
            ).fetchone()
            if row:
                return dict(row)
            return {
                "person_id": person_id,
                "state": "allowed",
                "reason": "",
                "entered_ts": None,
                "expires_ts": None,
            }

    def set_child_state(self, person_id: str, state: str,
                        reason: str = "", expires_ts: str | None = None) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_lock, self._write_conn as conn:
            conn.execute(
                """INSERT INTO child_states (person_id, state, reason, entered_ts, expires_ts)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(person_id) DO UPDATE SET
                       state      = excluded.state,
                       reason     = excluded.reason,
                       entered_ts = excluded.entered_ts,
                       expires_ts = excluded.expires_ts""",
                (person_id, state, reason, now, expires_ts),
            )
            row = conn.execute(
                "SELECT * FROM child_states WHERE person_id = ?", (person_id,)
            ).fetchone()
            return dict(row) if row else {}

    def list_child_states(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM child_states ORDER BY person_id"
            ).fetchall()
            return [dict(r) for r in rows]
