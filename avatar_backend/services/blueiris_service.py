"""BlueIrisService — direct camera access fallback when HA is unavailable.

Connects to Blue Iris at BI_URL (e.g. http://192.168.0.33:81) for:
- Snapshots: /image/{camera}?q=60
- MJPEG streams: /mjpg/{camera}
- PTZ control via JSON API (requires BLUEIRIS_USER / BLUEIRIS_PASSWORD)

Camera name mapping is configured in home_runtime.json under
"blueiris_camera_map": {"camera.ha_entity_id": "bi_short_name"}.
"""
from __future__ import annotations

import hashlib

import httpx
from avatar_backend.services._shared_http import _http_client
import structlog

from avatar_backend.services.home_runtime import load_home_runtime_config

_LOGGER = structlog.get_logger()


class BlueIrisService:
    def __init__(self, bi_url: str = "", bi_user: str = "", bi_password: str = "") -> None:
        self._bi_url = bi_url.rstrip("/")
        self._bi_user = bi_user
        self._bi_password = bi_password
        self._session: str | None = None
        runtime = load_home_runtime_config()
        self._camera_map: dict[str, str] = runtime.blueiris_camera_map

    @property
    def available(self) -> bool:
        return bool(self._bi_url)

    def resolve_camera(self, ha_entity_id: str) -> str | None:
        """Map an HA camera entity ID to a Blue Iris short name."""
        return self._camera_map.get(ha_entity_id)

    async def _authenticate(self) -> str | None:
        """Authenticate with Blue Iris JSON API; returns session token or None."""
        if not self._bi_user or not self._bi_password:
            return None
        url = f"{self._bi_url}/json"
        try:
            r1 = await _http_client().post(url, json={"cmd": "login"}, timeout=5.0)
            data1 = r1.json()
            if data1.get("result") == "fail" and "IP banned" in str(data1.get("data", "")):
                _LOGGER.warning("blueiris.ptz_ip_banned")
                return None
            session = data1.get("session", "")
            if not session:
                return None
            pw_md5 = hashlib.md5(self._bi_password.encode()).hexdigest()
            response = hashlib.md5(f"{self._bi_user}:{pw_md5}:{session}".encode()).hexdigest()
            r2 = await _http_client().post(url, json={"cmd": "login", "session": session, "response": response}, timeout=5.0)
            data2 = r2.json()
            if data2.get("result") == "success":
                self._session = session
                return session
            _LOGGER.warning("blueiris.auth_failed", reason=data2.get("data", {}).get("reason", "unknown"))
        except Exception as exc:
            _LOGGER.warning("blueiris.auth_error", exc=str(exc)[:100])
        return None

    async def ptz_preset(self, bi_camera: str, preset: int) -> bool:
        """Move camera to PTZ preset (0-indexed). Returns True on success."""
        if not self._bi_url:
            return False
        # BI preset button numbers: 100=preset1, 101=preset2, ...
        button = 100 + preset
        url = f"{self._bi_url}/json"
        try:
            session = await self._authenticate()
            if not session:
                return False
            payload = {"cmd": "ptz", "camera": bi_camera, "button": button, "session": session}
            r = await _http_client().post(url, json=payload, timeout=5.0)
            result = r.json().get("result")
            if result == "success":
                _LOGGER.info("blueiris.ptz_preset_ok", camera=bi_camera, preset=preset)
                return True
            _LOGGER.warning("blueiris.ptz_preset_failed", camera=bi_camera, preset=preset, result=result)
        except Exception as exc:
            _LOGGER.warning("blueiris.ptz_error", camera=bi_camera, exc=str(exc)[:100])
        return False

    async def fetch_snapshot_by_name(self, bi_name: str) -> bytes | None:
        """Fetch a JPEG snapshot by Blue Iris short name (no auth needed from LAN)."""
        if not self._bi_url or not bi_name:
            return None
        url = f"{self._bi_url}/image/{bi_name}?q=70"
        try:
            resp = await _http_client().get(url, timeout=6.0)
            if resp.status_code == 200 and len(resp.content) > 2000:
                return resp.content
            _LOGGER.warning("blueiris.snapshot_failed", camera=bi_name, status=resp.status_code)
        except Exception as exc:
            _LOGGER.warning("blueiris.snapshot_error", camera=bi_name, exc=str(exc)[:100])
        return None

    async def fetch_snapshot(self, ha_entity_id: str) -> bytes | None:
        """Fetch a JPEG snapshot directly from Blue Iris."""
        if not self._bi_url:
            return None
        bi_name = self.resolve_camera(ha_entity_id)
        if not bi_name:
            return None
        url = f"{self._bi_url}/image/{bi_name}?q=60"
        try:
            resp = await _http_client().get(url, timeout=5.0)
            if resp.status_code == 200 and resp.content and len(resp.content) > 1000:
                _LOGGER.info("blueiris.snapshot_ok", camera=bi_name, bytes=len(resp.content))
                return resp.content
            _LOGGER.warning("blueiris.snapshot_failed", camera=bi_name, status=resp.status_code)
        except Exception as exc:
            _LOGGER.warning("blueiris.snapshot_error", camera=bi_name, exc=str(exc)[:100])
        return None

    async def is_reachable(self) -> bool:
        """Check if Blue Iris is reachable."""
        if not self._bi_url:
            return False
        try:
            resp = await _http_client().get(f"{self._bi_url}/image/index?q=10", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    def mjpeg_url(self, ha_entity_id: str) -> str | None:
        """Return the MJPEG stream URL for a camera."""
        if not self._bi_url:
            return None
        bi_name = self.resolve_camera(ha_entity_id)
        if not bi_name:
            return None
        return f"{self._bi_url}/mjpg/{bi_name}"
