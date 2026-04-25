"""
GeminiKeyPool — round-robin Gemini API key rotation with 429 cooldown.

Distributes vision API calls across multiple free-tier Gemini API keys.
When a key hits 429 (rate limit), it enters cooldown and the next key is used.
Optionally pin specific cameras to specific keys for guaranteed quota.
"""
from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path

import structlog

_LOGGER = structlog.get_logger()
_DEFAULT_COOLDOWN_S = 60


@dataclass
class KeyState:
    key: str
    label: str = ""
    cooldown_until: float = 0.0
    total_calls: int = 0
    total_429s: int = 0
    total_errors: int = 0
    last_used: float = 0.0
    consecutive_429s: int = 0
    pinned_cameras: list[str] = field(default_factory=list)
    enabled: bool = True
    # Observability
    _call_timestamps: list[float] = field(default_factory=list)
    _latencies: list[float] = field(default_factory=list)
    _tokens_used: int = 0

    @property
    def is_available(self) -> bool:
        return self.enabled and time.monotonic() > self.cooldown_until

    @property
    def masked_key(self) -> str:
        if len(self.key) <= 8:
            return "****"
        return self.key[:4] + "…" + self.key[-4:]

    @property
    def rpm(self) -> float:
        """Requests per minute over the last 60 seconds."""
        now = time.monotonic()
        cutoff = now - 60
        self._call_timestamps = [t for t in self._call_timestamps if t > cutoff]
        return len(self._call_timestamps)

    @property
    def avg_latency_ms(self) -> float:
        if not self._latencies:
            return 0
        recent = self._latencies[-20:]
        return round(sum(recent) / len(recent))

    def record_call(self) -> None:
        self.total_calls += 1
        self.last_used = time.monotonic()
        self._call_timestamps.append(time.monotonic())

    def record_latency(self, ms: float) -> None:
        self._latencies.append(ms)
        if len(self._latencies) > 100:
            self._latencies = self._latencies[-50:]

    def record_tokens(self, count: int) -> None:
        self._tokens_used += count

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "masked_key": self.masked_key,
            "available": self.is_available,
            "cooldown_remaining_s": max(0, round(self.cooldown_until - time.monotonic())),
            "total_calls": self.total_calls,
            "total_429s": self.total_429s,
            "total_errors": self.total_errors,
            "consecutive_429s": self.consecutive_429s,
            "last_used": self.last_used,
            "pinned_cameras": self.pinned_cameras,
            "enabled": self.enabled,
            "rpm": self.rpm,
            "avg_latency_ms": self.avg_latency_ms,
            "tokens_used": self._tokens_used,
        }


class GeminiKeyPool:
    """Round-robin Gemini API key pool with 429 cooldown and camera pinning."""

    _SAVE_DEBOUNCE_S = 60  # minimum seconds between debounced saves

    def __init__(self, cooldown_s: float = _DEFAULT_COOLDOWN_S) -> None:
        self._keys: list[KeyState] = []
        self._lock = threading.Lock()
        self._robin_idx = 0
        self._cooldown_s = cooldown_s
        self._state_path: Path | None = None
        self._last_save: float = 0.0


    def set_state_path(self, path: Path) -> None:
        """Set the file path used for metric persistence and start the flush thread."""
        self._state_path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Background thread: flush every 5 minutes so total_calls persists even
        # when there are no 429s or explicit report_success calls.
        t = threading.Thread(target=self._flush_loop, daemon=True, name="gemini-pool-flush")
        t.start()

    def _flush_loop(self) -> None:
        while True:
            time.sleep(300)
            self._save_state(force=True)

    def _save_state(self, *, force: bool = False) -> None:
        """Write per-key metrics to disk. Debounced unless force=True."""
        if not self._state_path:
            return
        now = time.time()
        if not force and (now - self._last_save) < self._SAVE_DEBOUNCE_S:
            return
        self._last_save = now
        payload: dict = {"v": 1, "saved_at": now, "keys": {}}
        with self._lock:
            for k in self._keys:
                remaining = k.cooldown_until - time.monotonic()
                payload["keys"][k.key] = {
                    "label": k.label,
                    "total_calls": k.total_calls,
                    "total_429s": k.total_429s,
                    "total_errors": k.total_errors,
                    "consecutive_429s": k.consecutive_429s,
                    "tokens_used": k._tokens_used,
                    # Convert monotonic deadline → absolute wall-clock expiry
                    "cooldown_wall": (now + remaining) if remaining > 0 else 0.0,
                }
        try:
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self._state_path)
        except Exception as exc:
            _LOGGER.warning("gemini_pool.state_save_failed", exc=str(exc)[:120])

    def load_state(self) -> None:
        """Restore per-key metrics from disk. Called once after keys are loaded."""
        if not self._state_path or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text())
        except Exception as exc:
            _LOGGER.warning("gemini_pool.state_load_failed", exc=str(exc)[:120])
            return
        stored = payload.get("keys", {})
        now_wall = time.time()
        now_mono = time.monotonic()
        restored = 0
        with self._lock:
            for k in self._keys:
                entry = stored.get(k.key)
                if not entry:
                    continue
                k.total_calls = int(entry.get("total_calls", 0))
                k.total_429s = int(entry.get("total_429s", 0))
                k.total_errors = int(entry.get("total_errors", 0))
                k.consecutive_429s = int(entry.get("consecutive_429s", 0))
                k._tokens_used = int(entry.get("tokens_used", 0))
                cooldown_wall = float(entry.get("cooldown_wall", 0.0))
                remaining = cooldown_wall - now_wall
                k.cooldown_until = (now_mono + remaining) if remaining > 0 else 0.0
                restored += 1
        _LOGGER.info("gemini_pool.state_loaded",
                     restored=restored,
                     path=str(self._state_path),
                     saved_at=payload.get("saved_at", 0))

    def clear(self) -> None:
        """Remove all keys from the pool."""
        with self._lock:
            self._keys.clear()

    @property
    def all_keys(self) -> list[str]:
        """Return all raw API keys in the pool."""
        with self._lock:
            return [k.key for k in self._keys]

    def get_internal_state(self) -> list[dict]:
        """Return raw internal state for serialization."""
        with self._lock:
            return [
                {
                    "key": k.key,
                    "label": k.label,
                    "enabled": k.enabled,
                    "pinned_cameras": list(k.pinned_cameras),
                }
                for k in self._keys
            ]

    @property
    def size(self) -> int:
        return len(self._keys)

    @property
    def available_count(self) -> int:
        with self._lock:
            return sum(1 for k in self._keys if k.is_available)

    def add_key(self, key: str, label: str = "", enabled: bool = True) -> None:
        """Add a key to the pool. Deduplicates by key value."""
        key = key.strip()
        if not key:
            return
        with self._lock:
            if any(k.key == key for k in self._keys):
                return
            idx = len(self._keys)
            self._keys.append(KeyState(key=key, label=label or f"Key {idx + 1}", enabled=enabled))
        _LOGGER.info("gemini_pool.key_added", label=label or f"Key {idx + 1}", pool_size=len(self._keys))

    def toggle_key(self, index: int, enabled: bool) -> bool:
        """Enable or disable a key by index."""
        with self._lock:
            if 0 <= index < len(self._keys):
                self._keys[index].enabled = enabled
                _LOGGER.info("gemini_pool.key_toggled", label=self._keys[index].label, enabled=enabled)
                return True
        return False

    def remove_key(self, index: int) -> bool:
        """Remove a key by index."""
        with self._lock:
            if 0 <= index < len(self._keys):
                removed = self._keys.pop(index)
                if self._robin_idx >= len(self._keys):
                    self._robin_idx = 0
                _LOGGER.info("gemini_pool.key_removed", label=removed.label)
                return True
        return False

    def pin_camera(self, key_index: int, camera_id: str) -> None:
        """Pin a camera to a specific key."""
        with self._lock:
            # Remove camera from any existing pin
            for k in self._keys:
                if camera_id in k.pinned_cameras:
                    k.pinned_cameras.remove(camera_id)
            if 0 <= key_index < len(self._keys):
                self._keys[key_index].pinned_cameras.append(camera_id)

    def unpin_camera(self, camera_id: str) -> None:
        """Remove camera pin."""
        with self._lock:
            for k in self._keys:
                if camera_id in k.pinned_cameras:
                    k.pinned_cameras.remove(camera_id)

    def get_key(self, camera_id: str | None = None) -> str | None:
        """Get the next available API key. Returns None if all exhausted.

        If camera_id is pinned to a key and that key is available, use it.
        Otherwise round-robin across available keys.
        """
        with self._lock:
            if not self._keys:
                return None

            # Check camera pin first
            if camera_id:
                for k in self._keys:
                    if camera_id in k.pinned_cameras and k.is_available:
                        k.record_call()
                        return k.key

            # Round-robin across available keys
            n = len(self._keys)
            for _ in range(n):
                k = self._keys[self._robin_idx]
                self._robin_idx = (self._robin_idx + 1) % n
                if k.is_available:
                    k.record_call()
                    return k.key

        return None

    def report_429(self, key: str) -> None:
        """Mark a key as rate-limited. Exponential backoff: 60s, 120s, 240s, max 600s."""
        with self._lock:
            for k in self._keys:
                if k.key == key:
                    k.consecutive_429s += 1
                    k.total_429s += 1
                    backoff = min(self._cooldown_s * (2 ** (k.consecutive_429s - 1)), 600)
                    k.cooldown_until = time.monotonic() + backoff
                    _LOGGER.warning("gemini_pool.key_rate_limited",
                                    label=k.label, cooldown_s=round(backoff),
                                    consecutive=k.consecutive_429s,
                                    available=sum(1 for k2 in self._keys if k2.is_available))
                    break
        self._save_state(force=True)

    def report_success(self, key: str, latency_ms: float = 0, tokens: int = 0) -> None:
        """Record a successful call — resets backoff and tracks metrics."""
        with self._lock:
            for k in self._keys:
                if k.key == key:
                    k.consecutive_429s = 0
                    if latency_ms > 0:
                        k.record_latency(latency_ms)
                    if tokens > 0:
                        k.record_tokens(tokens)
                    break
        self._save_state()  # debounced

    def report_error(self, key: str) -> None:
        """Record a non-429 error."""
        with self._lock:
            for k in self._keys:
                if k.key == key:
                    k.total_errors += 1
                    break
        self._save_state(force=True)

    def get_status(self) -> list[dict]:
        """Return status of all keys for the admin UI."""
        with self._lock:
            return [k.to_dict() for k in self._keys]

    def get_stats(self) -> dict:
        """Return pool-level stats with observability metrics."""
        with self._lock:
            total_rpm = sum(k.rpm for k in self._keys)
            return {
                "pool_size": len(self._keys),
                "available": sum(1 for k in self._keys if k.is_available),
                "total_calls": sum(k.total_calls for k in self._keys),
                "total_429s": sum(k.total_429s for k in self._keys),
                "total_errors": sum(k.total_errors for k in self._keys),
                "total_tokens": sum(k._tokens_used for k in self._keys),
                "rpm": round(total_rpm, 1),
                "keys_in_cooldown": sum(1 for k in self._keys if k.enabled and not k.is_available),
            }


def _parse_pool_entry(raw_value: str, default_label: str) -> tuple[str, str, bool]:
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return "", default_label, True
    if "|" not in raw_value:
        return raw_value, default_label, True
    parts = raw_value.split("|")
    key = parts[0].strip()
    label = parts[1].strip() if len(parts) > 1 and parts[1].strip() else default_label
    enabled = True
    if len(parts) > 2:
        enabled = parts[2].strip().lower() not in {"0", "false", "off", "no"}
    return key, label, enabled


def _parse_pin_entry(raw_value: str) -> tuple[str, str]:
    raw_value = (raw_value or "").strip()
    if not raw_value or "|" not in raw_value:
        return "", ""
    camera_id, _, key = raw_value.partition("|")
    return camera_id.strip(), key.strip()


def load_pool_from_settings(pool: GeminiKeyPool, settings) -> None:
    """Rebuild a Gemini key pool from settings, preserving labels and enabled flags."""
    pool.clear()
    if settings.google_api_key:
        pool.add_key(
            settings.google_api_key,
            "Primary",
            enabled=getattr(settings, "google_api_key_enabled", True),
        )
    for i, raw_value in enumerate(k.strip() for k in settings.gemini_api_keys.split(",") if k.strip()):
        key, label, enabled = _parse_pool_entry(raw_value, f"Pool {i + 1}")
        if key:
            pool.add_key(key, label, enabled=enabled)
    pins_raw = getattr(settings, "gemini_camera_pins", "") or ""
    for raw_value in (entry.strip() for entry in pins_raw.split(",") if entry.strip()):
        camera_id, key = _parse_pin_entry(raw_value)
        if not camera_id or not key:
            continue
        for idx, state in enumerate(pool.get_internal_state()):
            if state["key"] == key:
                pool.pin_camera(idx, camera_id)
                break


def serialize_pool_for_env(pool: GeminiKeyPool, primary_key: str) -> tuple[str | None, str]:
    """Serialize pool state back into GOOGLE_API_KEY_ENABLED and GEMINI_API_KEYS values."""
    primary_enabled: str | None = None
    pool_entries: list[str] = []
    for state in pool.get_internal_state():
        key = state["key"]
        label = state["label"]
        enabled = "1" if state["enabled"] else "0"
        if key == primary_key:
            primary_enabled = "true" if state["enabled"] else "false"
        else:
            pool_entries.append(f"{key}|{label}|{enabled}")
    return primary_enabled, ",".join(pool_entries)


def serialize_pins_for_env(pool: GeminiKeyPool) -> str:
    """Serialize camera pin state back into GEMINI_CAMERA_PINS."""
    entries: list[str] = []
    for state in pool.get_internal_state():
        key = state["key"]
        for camera_id in state.get("pinned_cameras") or []:
            entries.append(f"{camera_id}|{key}")
    return ",".join(entries)
