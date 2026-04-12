"""
LogStore — captures structlog output into a ring buffer, SQLite, and SSE stream.

Attach via a stdlib logging.Handler so it works alongside the existing
RotatingFileHandler without touching the structlog processor chain.
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

_MAX_ENTRIES = 1000


class LogStore:
    def __init__(self) -> None:
        self._entries: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        self._db = None

    def set_db(self, db) -> None:
        self._db = db
        try:
            self._entries = db.recent_logs(_MAX_ENTRIES)
        except Exception:
            pass

    def record(self, entry: dict) -> None:
        """Store a log entry and fan out to SSE subscribers."""
        self._entries.append(entry)
        if len(self._entries) > _MAX_ENTRIES:
            self._entries.pop(0)

        if self._db is not None:
            try:
                self._db.insert_log(dict(entry))
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

    def recent(self, n: int = 500, level: str | None = None) -> list[dict]:
        if self._db is not None:
            try:
                return self._db.recent_logs(n, level)
            except Exception:
                pass
        entries = list(self._entries[-n:])
        if level:
            entries = [e for e in entries if e.get("level") == level]
        return entries

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def make_handler(self) -> logging.Handler:
        """Return a stdlib logging.Handler that feeds this LogStore."""
        store = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    raw = record.getMessage()
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        data = {"event": raw}

                    level  = data.pop("level",  record.levelname.lower())
                    event  = data.pop("event",  raw)
                    logger = data.pop("logger", record.name)
                    ts     = data.pop("timestamp", datetime.now(timezone.utc).isoformat())

                    entry = {
                        "ts":     ts[11:19] if len(ts) > 8 else ts,
                        "level":  level,
                        "event":  event,
                        "logger": logger,
                        **{k: v for k, v in data.items()
                           if k not in ("exc_info", "stack_info", "_record")},
                    }
                    store.record(entry)
                except Exception:
                    pass  # never crash the logging chain

        h = _Handler()
        h.setLevel(logging.DEBUG)
        return h
