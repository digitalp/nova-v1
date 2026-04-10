"""
Speaker service — broadcast AI responses to HA media players (Echo, Sonos, etc.)

Supports two playback strategies:

  1. Alexa notify  — Amazon Echo devices via Alexa Media Player integration.
                     Uses notify.alexa_media_<device> with type=tts.
                     Auto-detected when entity_id contains "echo", "alexa",
                     or "amazon", OR when prefixed with "alexa:" in SPEAKERS.

  2. HA TTS speak  — Everything else (Sonos, Google Home, Cast, …).
                     Uses tts.speak (HA 2023.6+).

Force Alexa mode by prefixing entity_id with "alexa:" in the SPEAKERS env var:
  SPEAKERS=alexa:media_player.living_room_3,media_player.sonos_kitchen

Both of the user's Echo devices map automatically:
  media_player.penn_s_2nd_echo_dot   ← contains "echo" → auto Alexa
  media_player.living_room_3        ← use "alexa:" prefix in .env
"""
from __future__ import annotations
import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import structlog
from avatar_backend.runtime_paths import config_dir

_LOGGER = structlog.get_logger()

_ALEXA_RE = re.compile(r"echo|alexa|amazon", re.IGNORECASE)
_DISCOVERY_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
_DISCOVERY_CACHE_TTL_S = 30.0
_AREA_FALLBACK = "Unassigned"
_OCCUPANCY_DEVICE_CLASSES = {"motion", "occupancy", "presence", "moving"}
_SPEAKER_SETTINGS_FILE = config_dir() / "speaker_settings.json"
_SYSTEM_PROMPT_FILE = config_dir() / "system_prompt.txt"
_SPEAKER_CATALOG_TEMPLATE = """
{% set ns = namespace(items=[]) %}
{% for entity in states.media_player %}
  {% set area = area_name(entity.entity_id) or 'Unassigned' %}
  {% set name = state_attr(entity.entity_id, 'friendly_name') or entity.name or entity.entity_id %}
  {% set ns.items = ns.items + [{
    'entity_id': entity.entity_id,
    'friendly_name': name,
    'area_name': area
  }] %}
{% endfor %}
{{ ns.items | tojson }}
""".strip()
_AREA_HEADER_RE = re.compile(r"^\s*AREA:\s*(.+?)\s*$")
_MEDIA_ENTITY_RE = re.compile(r"\b(media_player\.[a-z0-9_]+)\b", re.IGNORECASE)
_OCCUPIED_AREAS_TEMPLATE = """
{% set device_classes = ['motion', 'occupancy', 'presence', 'moving'] %}
{% set ns = namespace(areas=[]) %}
{% for entity in states.binary_sensor %}
  {% set dc = state_attr(entity.entity_id, 'device_class') %}
  {% if entity.state == 'on' and dc in device_classes %}
    {% set area = area_name(entity.entity_id) %}
    {% if area and area not in ns.areas %}
      {% set ns.areas = ns.areas + [area] %}
    {% endif %}
  {% endif %}
{% endfor %}
{{ ns.areas | tojson }}
""".strip()


def _notify_service_name(entity_id: str) -> str:
    """
    media_player.living_room_3       → alexa_media_living_room_3
    media_player.penn_s_2nd_echo_dot → alexa_media_penn_s_2nd_echo_dot
    """
    local = entity_id.split(".", 1)[-1]
    return f"alexa_media_{local}"


class SpeakerService:
    """Play text on one or more HA media players."""

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        speakers: list[str],
        tts_engine: str = "tts.google_translate_en_com",
    ) -> None:
        self._ha_url     = ha_url.rstrip("/")
        self._ha_token   = ha_token
        self._tts_engine = tts_engine
        self._settings_path = Path(_SPEAKER_SETTINGS_FILE)
        self._catalog_cache: tuple[float, list[dict]] = (0.0, [])
        self._occupied_cache: tuple[float, list[str]] = (0.0, [])

        # Default entries seeded from env config: entity_id -> use_alexa_notify
        self._default_speakers: dict[str, bool] = {}
        for raw in speakers:
            if raw.startswith("alexa:"):
                self._default_speakers[raw[len("alexa:"):]] = True
            else:
                self._default_speakers[raw] = bool(_ALEXA_RE.search(raw))
        self._prefs = self._load_preferences()

    @property
    def is_configured(self) -> bool:
        return bool(self._configured_speakers_sync())

    async def speak(
        self,
        text: str,
        *,
        target_areas: list[str] | None = None,
        area_aware: bool = False,
    ) -> None:
        """Play *text* on all configured speakers concurrently (via HA TTS engine)."""
        speakers = await self._resolve_speakers(
            target_areas=target_areas or [],
            area_aware=area_aware,
        )
        if not text or not text.strip() or not speakers:
            return

        tasks = [
            self._speak_on(entity_id, text, alexa)
            for entity_id, alexa in speakers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (entity_id, _), result in zip(speakers, results):
            if isinstance(result, Exception):
                _LOGGER.warning("speaker.error",
                                entity_id=entity_id, exc=str(result))

    async def speak_wav(
        self,
        text: str,
        audio_url: str,
        *,
        target_areas: list[str] | None = None,
        area_aware: bool = False,
    ) -> None:
        """Play synthesised audio on all speakers.

        Non-Alexa (Sonos, etc.) → media_player.play_media with Nova server URL.
        Alexa/Echo → notify.alexa_media TTS (Echo does not support custom audio streaming).
        """
        speakers = await self._resolve_speakers(
            target_areas=target_areas or [],
            area_aware=area_aware,
        )
        if not speakers:
            return

        tasks = []
        for entity_id, alexa in speakers:
            if alexa:
                tasks.append(self._speak_on(entity_id, text, alexa=True))
            else:
                tasks.append(self._play_media(entity_id, audio_url))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (entity_id, _), result in zip(speakers, results):
            if isinstance(result, Exception):
                _LOGGER.warning("speaker.error",
                                entity_id=entity_id, exc=str(result))

    async def get_speaker_catalog(self, *, force_refresh: bool = False) -> list[dict]:
        now = time.monotonic()
        cached_at, cached = self._catalog_cache
        if cached and not force_refresh and now - cached_at < _DISCOVERY_CACHE_TTL_S:
            return [dict(item) for item in cached]

        discovered = await self._fetch_speaker_catalog()
        if not discovered:
            discovered = [
                {
                    "entity_id": entity_id,
                    "friendly_name": entity_id.split(".", 1)[-1].replace("_", " ").title(),
                    "area_name": _AREA_FALLBACK,
                }
                for entity_id in self._default_speakers
            ]

        merged: list[dict] = []
        seen: set[str] = set()
        prompt_areas = self._load_prompt_area_map()
        for item in discovered:
            entity_id = str(item.get("entity_id") or "").strip()
            if not entity_id or entity_id in seen:
                continue
            seen.add(entity_id)
            area_name = str(item.get("area_name") or _AREA_FALLBACK)
            if area_name == _AREA_FALLBACK:
                area_name = prompt_areas.get(entity_id, area_name)
            merged.append({
                "entity_id": entity_id,
                "friendly_name": str(item.get("friendly_name") or entity_id),
                "area_name": area_name,
                "enabled": self._is_enabled(entity_id),
                "use_alexa": self._use_alexa(entity_id),
            })

        for entity_id in self._configured_entity_ids():
            if entity_id in seen:
                continue
            merged.append({
                "entity_id": entity_id,
                "friendly_name": entity_id.split(".", 1)[-1].replace("_", " ").title(),
                "area_name": prompt_areas.get(entity_id, _AREA_FALLBACK),
                "enabled": self._is_enabled(entity_id),
                "use_alexa": self._use_alexa(entity_id),
            })

        merged.sort(key=lambda item: (item["area_name"].lower(), item["friendly_name"].lower()))
        self._catalog_cache = (now, [dict(item) for item in merged])
        return merged

    async def get_occupied_areas(self, *, force_refresh: bool = False) -> list[str]:
        now = time.monotonic()
        cached_at, cached = self._occupied_cache
        if cached and not force_refresh and now - cached_at < _DISCOVERY_CACHE_TTL_S:
            return list(cached)
        areas = await self._fetch_occupied_areas()
        self._occupied_cache = (now, list(areas))
        return areas

    def set_speaker_preferences(self, entries: list[dict]) -> None:
        prefs: dict[str, bool] = {}
        for item in entries:
            entity_id = str(item.get("entity_id") or "").strip()
            if not entity_id.startswith("media_player."):
                continue
            prefs[entity_id] = bool(item.get("enabled"))
        self._prefs = prefs
        self._save_preferences()
        self._catalog_cache = (0.0, [])

    # ── Private ───────────────────────────────────────────────────────────────

    def _load_preferences(self) -> dict[str, bool]:
        try:
            if self._settings_path.exists():
                payload = json.loads(self._settings_path.read_text())
                speakers = payload.get("speakers", {})
                if isinstance(speakers, dict):
                    return {
                        str(entity_id): bool(enabled)
                        for entity_id, enabled in speakers.items()
                        if str(entity_id).startswith("media_player.")
                    }
        except Exception as exc:
            _LOGGER.warning("speaker.settings_load_failed", exc=str(exc))
        return {}

    def _save_preferences(self) -> None:
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(json.dumps({"speakers": self._prefs}, indent=2, sort_keys=True))
        except Exception as exc:
            _LOGGER.warning("speaker.settings_save_failed", exc=str(exc))

    def _configured_entity_ids(self) -> list[str]:
        if self._prefs:
            return [entity_id for entity_id, enabled in self._prefs.items() if enabled]
        return list(self._default_speakers.keys())

    def _load_prompt_area_map(self) -> dict[str, str]:
        try:
            if not _SYSTEM_PROMPT_FILE.exists():
                return {}
            current_area = ""
            mapping: dict[str, str] = {}
            for raw_line in _SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").splitlines():
                header = _AREA_HEADER_RE.match(raw_line.strip())
                if header:
                    current_area = header.group(1).strip()
                    continue
                if not current_area:
                    continue
                for match in _MEDIA_ENTITY_RE.findall(raw_line):
                    mapping[match.lower()] = current_area
            return mapping
        except Exception as exc:
            _LOGGER.warning("speaker.prompt_area_map_failed", exc=str(exc))
            return {}

    def _configured_speakers_sync(self) -> list[tuple[str, bool]]:
        return [
            (entity_id, self._use_alexa(entity_id))
            for entity_id in self._configured_entity_ids()
        ]

    def _is_enabled(self, entity_id: str) -> bool:
        if entity_id in self._prefs:
            return bool(self._prefs[entity_id])
        if self._prefs:
            return False
        return entity_id in self._default_speakers

    def _use_alexa(self, entity_id: str) -> bool:
        if entity_id in self._default_speakers:
            return self._default_speakers[entity_id]
        return bool(_ALEXA_RE.search(entity_id))

    async def _resolve_speakers(
        self,
        *,
        target_areas: list[str],
        area_aware: bool,
    ) -> list[tuple[str, bool]]:
        if not area_aware and not target_areas:
            return self._configured_speakers_sync()

        catalog = [item for item in await self.get_speaker_catalog() if item.get("enabled")]
        if not catalog:
            return self._configured_speakers_sync()

        target_lookup = {
            str(area).strip().lower(): str(area).strip()
            for area in target_areas
            if str(area).strip()
        }
        targeted = [
            item for item in catalog
            if str(item.get("area_name") or "").strip().lower() in target_lookup
        ]

        if target_lookup and targeted:
            return [(item["entity_id"], bool(item["use_alexa"])) for item in targeted]

        if area_aware:
            occupied_lookup = {
                area.strip().lower(): area.strip()
                for area in await self.get_occupied_areas()
                if area.strip()
            }
            occupied = [
                item for item in catalog
                if str(item.get("area_name") or "").strip().lower() in occupied_lookup
            ]
            if occupied:
                return [(item["entity_id"], bool(item["use_alexa"])) for item in occupied]

        return [(item["entity_id"], bool(item["use_alexa"])) for item in catalog]

    async def _fetch_speaker_catalog(self) -> list[dict]:
        payload = await self._render_template_json(_SPEAKER_CATALOG_TEMPLATE)
        return payload if isinstance(payload, list) else []

    async def _fetch_occupied_areas(self) -> list[str]:
        payload = await self._render_template_json(_OCCUPIED_AREAS_TEMPLATE)
        if not isinstance(payload, list):
            return []
        return sorted(
            {
                str(area).strip()
                for area in payload
                if str(area).strip()
            }
        )

    async def _render_template_json(self, template: str):
        try:
            async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._ha_url}/api/template",
                    headers=self._headers,
                    json={"template": template},
                )
            if resp.status_code != 200:
                raise RuntimeError(f"template HTTP {resp.status_code}")
            return json.loads(resp.text)
        except Exception as exc:
            _LOGGER.warning("speaker.discovery_failed", exc=str(exc))
            return []

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }

    async def _speak_on(self, entity_id: str, text: str, alexa: bool) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if alexa:
                await self._alexa_notify(client, entity_id, text)
            else:
                await self._tts_speak(client, entity_id, text)
        _LOGGER.info("speaker.spoke", entity_id=entity_id, alexa=alexa)

    async def _play_media(
        self,
        client_or_entity: str,
        audio_url: str,
    ) -> None:
        """Play an audio URL on a media player via media_player.play_media."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._ha_url}/api/services/media_player/play_media",
                headers=self._headers,
                json={
                    "entity_id": client_or_entity,
                    "media_content_id": audio_url,
                    "media_content_type": "music",
                },
            )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"play_media {client_or_entity} HTTP {resp.status_code}: {resp.text[:120]}"
            )
        _LOGGER.info("speaker.play_media", entity_id=client_or_entity, url=audio_url)

    async def _alexa_notify(
        self,
        client: httpx.AsyncClient,
        entity_id: str,
        text: str,
    ) -> None:
        """notify.alexa_media_<device> — works with Alexa Media Player integration."""
        svc = _notify_service_name(entity_id)
        resp = await client.post(
            f"{self._ha_url}/api/services/notify/{svc}",
            headers=self._headers,
            json={"message": text, "data": {"type": "tts"}},
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Alexa notify/{svc} returned HTTP {resp.status_code}: "
                f"{resp.text[:120]}"
            )

    async def _tts_speak(
        self,
        client: httpx.AsyncClient,
        entity_id: str,
        text: str,
    ) -> None:
        """tts.speak — HA 2023.6+ for non-Echo media players."""
        resp = await client.post(
            f"{self._ha_url}/api/services/tts/speak",
            headers=self._headers,
            json={
                "entity_id": self._tts_engine,
                "media_player_entity_id": entity_id,
                "message": text,
            },
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"tts.speak for {entity_id} returned HTTP {resp.status_code}: "
                f"{resp.text[:120]}"
            )
