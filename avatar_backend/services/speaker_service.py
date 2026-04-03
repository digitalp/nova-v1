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
import re

import httpx
import structlog

_LOGGER = structlog.get_logger()

_ALEXA_RE = re.compile(r"echo|alexa|amazon", re.IGNORECASE)


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

        # Each entry: (entity_id, use_alexa_notify)
        self._speakers: list[tuple[str, bool]] = []
        for raw in speakers:
            if raw.startswith("alexa:"):
                self._speakers.append((raw[len("alexa:"):], True))
            else:
                self._speakers.append((raw, bool(_ALEXA_RE.search(raw))))

    @property
    def is_configured(self) -> bool:
        return bool(self._speakers)

    async def speak(self, text: str) -> None:
        """Play *text* on all configured speakers concurrently."""
        if not text or not text.strip() or not self._speakers:
            return

        tasks = [
            self._speak_on(entity_id, text, alexa)
            for entity_id, alexa in self._speakers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (entity_id, _), result in zip(self._speakers, results):
            if isinstance(result, Exception):
                _LOGGER.warning("speaker.error",
                                entity_id=entity_id, exc=str(result))

    # ── Private ───────────────────────────────────────────────────────────────

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
