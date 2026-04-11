"""
DecisionLog — lightweight ring buffer for Nova AI decision events.

Events are broadcast to all active SSE subscribers (admin panel live log)
AND persisted to MetricsDB (SQLite) so they survive server restarts.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any

_MAX_ENTRIES = 300   # keep last 300 decisions in memory


class DecisionLog:
    def __init__(self) -> None:
        self._entries: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        self._db = None   # set via set_db() once MetricsDB is available

    def set_db(self, db) -> None:
        """Wire up the MetricsDB for persistence. Call after DB is initialised."""
        self._db = db
        # Bootstrap in-memory cache from DB so recent() works immediately
        try:
            self._entries = db.recent_decisions(_MAX_ENTRIES)
        except Exception:
            pass  # DB might not have the table yet on first boot

    # ── Public API ────────────────────────────────────────────────────────

    def record(self, kind: str, **fields: Any) -> dict:
        """Append a decision event, persist to DB, and fan out to SSE subscribers."""
        entry: dict = {
            "ts":   datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "kind": kind,
            **fields,
        }
        self._entries.append(entry)
        if len(self._entries) > _MAX_ENTRIES:
            self._entries.pop(0)

        # Persist to SQLite (non-blocking; ignore errors)
        if self._db is not None:
            try:
                self._db.insert_decision(entry)
            except Exception:
                pass

        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)
        return entry

    def recent(self, n: int = 200) -> list[dict]:
        # If DB is wired, query it directly so we always get the freshest persistent view
        if self._db is not None:
            try:
                return self._db.recent_decisions(n)
            except Exception:
                pass
        return list(self._entries[-n:])

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass
