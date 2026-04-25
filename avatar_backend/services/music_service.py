"""MusicService — media player control via Home Assistant + Music Assistant search."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from avatar_backend.services._shared_http import _http_client
import structlog

_LOGGER = structlog.get_logger()

_EXCLUDE_PATTERNS = ("plex_", "rk3566", "xbox", "wearos")

# Only show actual speaker/audio devices
_SPEAKER_KEYWORDS = ("sonos", "denon", "echo", "alexa")
_SPEAKER_DOMAINS = ("echo_dot", "echo_show", "fire_tv")


class MusicService:
    def __init__(self, ha_proxy, music_assistant_url: str = "") -> None:
        self._ha = ha_proxy
        self._ma_url = (music_assistant_url or "").rstrip("/")

    def _is_speaker(self, entity_id: str, friendly_name: str) -> bool:
        """Return True if this media_player is a real speaker/audio device."""
        eid = entity_id.lower()
        fname = friendly_name.lower()
        if any(p in eid for p in _EXCLUDE_PATTERNS):
            return False
        return any(k in eid or k in fname for k in _SPEAKER_KEYWORDS)

    @property
    def music_assistant_available(self) -> bool:
        return bool(self._ma_url)

    async def check_music_assistant(self) -> bool:
        if not self._ma_url:
            return False
        try:
            ws_url = self._ma_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
            import websockets
            async with websockets.connect(ws_url, open_timeout=3) as ws:
                info = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                return info.get("status") == "running"
        except Exception:
            return False

    # ── Music Assistant search & play ─────────────────────────────────────

    async def search(self, query: str, media_type: str = "track", limit: int = 10) -> list[dict]:
        """Search via HA's music_assistant.search service."""
        try:
            from avatar_backend.config import get_settings
            s = get_settings()
            # Get config_entry_id for music_assistant integration
            entry_id = await self._get_ma_config_entry_id(s)
            if not entry_id:
                return []
            async with httpx.AsyncClient(timeout=15.0, verify=False) as c:
                r = await c.post(
                    f"{s.ha_url}/api/services/music_assistant/search",
                    headers={"Authorization": f"Bearer {s.ha_token}"},
                    json={"config_entry_id": entry_id, "name": query, "media_type": media_type, "limit": limit},
                    params={"return_response": "true"},
                )
                if r.status_code == 200:
                    data = r.json()
                    sr = data.get("service_response", data)
                    if isinstance(sr, dict):
                        for key in ("tracks", "artists", "albums", "playlists", "radio"):
                            if key in sr and sr[key]:
                                return sr[key][:limit]
                    return sr if isinstance(sr, list) else []
        except Exception as exc:
            _LOGGER.warning("music.search_failed", query=query, error=str(exc)[:100])
        return []

    async def _get_ma_config_entry_id(self, settings) -> str:
        if hasattr(self, "_ma_entry_id") and self._ma_entry_id:
            return self._ma_entry_id
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=False) as c:
                r = await c.get(
                    f"{settings.ha_url}/api/config/config_entries/entry",
                    headers={"Authorization": f"Bearer {settings.ha_token}"},
                )
                for entry in r.json():
                    if entry.get("domain") == "music_assistant":
                        self._ma_entry_id = entry["entry_id"]
                        return self._ma_entry_id
        except Exception:
            pass
        return ""

    async def play_on_player(self, player_id: str, media_uri: str) -> dict:
        if not self._ma_url:
            return {"success": False, "message": "Music Assistant not configured"}
        try:
            r = await _http_client().post(f"{self._ma_url}/api/players/{player_id}/play_media", json={"uri": media_uri}, timeout=10.0)
            return {"success": r.status_code == 200, "message": r.text[:200]}
        except Exception as exc:
            return {"success": False, "message": str(exc)[:200]}

    async def get_ma_players(self) -> list[dict]:
        if not self._ma_url:
            return []
        try:
            r = await _http_client().get(f"{self._ma_url}/api/players", timeout=5.0)
            if r.status_code == 200:
                return r.json() if isinstance(r.json(), list) else []
        except Exception:
            pass
        return []

    # ── HA media_player control ───────────────────────────────────────────

    async def get_players(self) -> list[dict[str, Any]]:
        states = await self._ha.get_states_by_domain("media_player")
        players = []
        for s in states:
            eid = s.get("entity_id", "")
            attrs = s.get("attributes", {})
            fname = attrs.get("friendly_name", eid)
            if not self._is_speaker(eid, fname):
                continue
            players.append({
                "entity_id": eid,
                "friendly_name": attrs.get("friendly_name", eid),
                "state": s.get("state", "unknown"),
                "media_title": attrs.get("media_title", ""),
                "media_artist": attrs.get("media_artist", ""),
                "media_album_name": attrs.get("media_album_name", ""),
                "entity_picture": attrs.get("entity_picture", ""),
                "volume_level": attrs.get("volume_level"),
                "is_volume_muted": attrs.get("is_volume_muted", False),
                "source": attrs.get("source", ""),
                "source_list": attrs.get("source_list", []),
            })
        return sorted(players, key=lambda p: (p["state"] == "unavailable", p["friendly_name"]))

    async def get_now_playing(self) -> list[dict[str, Any]]:
        players = await self.get_players()
        return [p for p in players if p["state"] in ("playing", "paused", "buffering", "idle")]

    async def play(self, entity_id: str) -> dict:
        r = await self._ha.call_service("media_player", "media_play", entity_id)
        return {"success": r.success, "message": r.message}

    async def pause(self, entity_id: str) -> dict:
        r = await self._ha.call_service("media_player", "media_pause", entity_id)
        return {"success": r.success, "message": r.message}

    async def stop(self, entity_id: str) -> dict:
        r = await self._ha.call_service("media_player", "media_stop", entity_id)
        return {"success": r.success, "message": r.message}

    async def next_track(self, entity_id: str) -> dict:
        r = await self._ha.call_service("media_player", "media_next_track", entity_id)
        return {"success": r.success, "message": r.message}

    async def previous_track(self, entity_id: str) -> dict:
        r = await self._ha.call_service("media_player", "media_previous_track", entity_id)
        return {"success": r.success, "message": r.message}

    async def set_volume(self, entity_id: str, level: float) -> dict:
        level = max(0.0, min(1.0, float(level)))
        r = await self._ha.call_service("media_player", "volume_set", entity_id, service_data={"volume_level": level})
        return {"success": r.success, "message": r.message}

    async def mute(self, entity_id: str, mute: bool = True) -> dict:
        r = await self._ha.call_service("media_player", "volume_mute", entity_id, service_data={"is_volume_muted": mute})
        return {"success": r.success, "message": r.message}

    async def select_source(self, entity_id: str, source: str) -> dict:
        r = await self._ha.call_service("media_player", "select_source", entity_id, service_data={"source": source})
        return {"success": r.success, "message": r.message}

    async def play_media(self, entity_id: str, media_content_id: str, media_content_type: str = "music") -> dict:
        """Play media on a speaker. Routes library:// URIs through Music Assistant."""
        std_uri = media_content_id
        if "spotify" in std_uri and "://" in std_uri:
            parts = std_uri.split("://", 1)
            if len(parts) == 2:
                std_uri = "spotify:" + parts[1].replace("/", ":")
        _LOGGER.info("music.play_media", entity_id=entity_id, uri=std_uri[:80])
        # library:// URIs must go through music_assistant service — Sonos can't play them directly
        if std_uri.startswith("library://"):
            r = await self._ha.call_service("music_assistant", "play_media", entity_id,
                                            service_data={"media_id": std_uri, "media_type": "track"})
        else:
            r = await self._ha.call_service("media_player", "play_media", entity_id,
                                            service_data={"media_content_id": std_uri, "media_content_type": "music"})
        return {"success": r.success, "message": r.message}
