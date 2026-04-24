"""
GeminiKeyPool — round-robin Gemini API key rotation with 429 cooldown.

Distributes vision API calls across multiple free-tier Gemini API keys.
When a key hits 429 (rate limit), it enters cooldown and the next key is used.
Optionally pin specific cameras to specific keys for guaranteed quota.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field

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
    last_used: float = 0.0
    pinned_cameras: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def is_available(self) -> bool:
        return self.enabled and time.monotonic() > self.cooldown_until

    @property
    def masked_key(self) -> str:
        if len(self.key) <= 8:
            return "****"
        return self.key[:4] + "…" + self.key[-4:]

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "masked_key": self.masked_key,
            "available": self.is_available,
            "cooldown_remaining_s": max(0, round(self.cooldown_until - time.monotonic())),
            "total_calls": self.total_calls,
            "total_429s": self.total_429s,
            "last_used": self.last_used,
            "pinned_cameras": self.pinned_cameras,
            "enabled": self.enabled,
        }


class GeminiKeyPool:
    """Round-robin Gemini API key pool with 429 cooldown and camera pinning."""

    def __init__(self, cooldown_s: float = _DEFAULT_COOLDOWN_S) -> None:
        self._keys: list[KeyState] = []
        self._lock = threading.Lock()
        self._robin_idx = 0
        self._cooldown_s = cooldown_s

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
                    "label": k.label, "enabled": k.enabled,
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
                        k.total_calls += 1
                        k.last_used = time.monotonic()
                        return k.key

            # Round-robin across available keys
            n = len(self._keys)
            for _ in range(n):
                k = self._keys[self._robin_idx]
                self._robin_idx = (self._robin_idx + 1) % n
                if k.is_available:
                    k.total_calls += 1
                    k.last_used = time.monotonic()
                    return k.key

        return None

    def report_429(self, key: str) -> None:
        """Mark a key as rate-limited. Enters cooldown."""
        with self._lock:
            for k in self._keys:
                if k.key == key:
                    k.cooldown_until = time.monotonic() + self._cooldown_s
                    k.total_429s += 1
                    _LOGGER.warning("gemini_pool.key_rate_limited",
                                    label=k.label, cooldown_s=self._cooldown_s,
                                    available=sum(1 for k2 in self._keys if k2.is_available))
                    return

    def report_success(self, key: str) -> None:
        """Mark a successful call (clears any residual state)."""
        pass  # No action needed — cooldown expires naturally

    def get_status(self) -> list[dict]:
        """Return status of all keys for the admin UI."""
        with self._lock:
            return [k.to_dict() for k in self._keys]

    def get_stats(self) -> dict:
        """Return pool-level stats."""
        with self._lock:
            return {
                "pool_size": len(self._keys),
                "available": sum(1 for k in self._keys if k.is_available),
                "total_calls": sum(k.total_calls for k in self._keys),
                "total_429s": sum(k.total_429s for k in self._keys),
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
