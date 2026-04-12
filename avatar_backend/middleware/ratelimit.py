"""
Simple in-memory sliding-window rate limiter for auth endpoints.

Tracks failed attempts per IP.  No external dependencies required.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_attempts: dict[str, list[float]] = defaultdict(list)

_WINDOW      = 15 * 60   # 15-minute sliding window
_MAX_FAILS   = 10        # block after this many failures within the window


def is_rate_limited(ip: str) -> bool:
    """Return True if *ip* has exceeded the failure threshold."""
    now = time.monotonic()
    with _lock:
        _attempts[ip] = [t for t in _attempts[ip] if now - t < _WINDOW]
        return len(_attempts[ip]) >= _MAX_FAILS


def record_failure(ip: str) -> None:
    """Record one failed auth attempt for *ip*."""
    with _lock:
        _attempts[ip].append(time.monotonic())


def clear_failures(ip: str) -> None:
    """Reset the counter for *ip* after a successful auth."""
    with _lock:
        _attempts.pop(ip, None)
