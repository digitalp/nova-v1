"""Media Fun Fact Watcher — monitors media players via WS state mirror and triggers fun facts."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

_LOGGER = structlog.get_logger(__name__)

# Map media_player entity → (app_name, room area for announcements)
_WATCHED_PLAYERS = {
    "media_player.shield_living_room": ("Channels DVR", ["LIVING ROOM"]),
    "media_player.plex_plex_for_android_tv_shield_android_tv_2": ("Plex", ["LIVING ROOM"]),
    # Add bedroom players when available:
    # "media_player.shield_bedroom": ("Channels DVR", ["BEDROOM"]),
}

_COOLDOWN_S = 120  # Don't re-trigger within 2 minutes of last announcement


class MediaFunFactWatcher:
    def __init__(self, container: Any):
        self._container = container
        self._last_titles: dict[str, str] = {}
        self._last_announced: dict[str, float] = {}
        self._pending_task: asyncio.Task | None = None

    def start(self, ws_manager: Any) -> None:
        ws_manager.register("media_fun_fact_watcher", self._on_state_change)
        _LOGGER.info("media_fun_fact_watcher.started", players=list(_WATCHED_PLAYERS.keys()))

    def stop(self, ws_manager: Any) -> None:
        ws_manager.unregister("media_fun_fact_watcher")

    def _on_state_change(self, msg: dict) -> None:
        if msg.get("type") != "event":
            return
        event_data = msg.get("event", {}).get("data", {})
        new_state = event_data.get("new_state")
        if not new_state:
            return

        entity_id = new_state.get("entity_id", "")
        if entity_id not in _WATCHED_PLAYERS:
            return

        state = new_state.get("state", "")
        if state != "playing":
            # Clear title tracking when stopped
            self._last_titles.pop(entity_id, None)
            if self._pending_task and not self._pending_task.done():
                self._pending_task.cancel()
            return

        title = (new_state.get("attributes") or {}).get("media_title", "")
        if not title or title in ("unavailable", "unknown", ""):
            return

        prev_title = self._last_titles.get(entity_id)
        if title == prev_title:
            return  # Same title, no change

        self._last_titles[entity_id] = title

        # Check cooldown
        last = self._last_announced.get(entity_id, 0)
        if time.time() - last < _COOLDOWN_S:
            _LOGGER.info("media_fun_fact_watcher.cooldown", entity=entity_id, title=title[:40])
            return

        _LOGGER.info("media_fun_fact_watcher.title_changed", entity=entity_id, title=title[:60])

        # Cancel any pending announcement (mode: restart)
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()

        app_name, target_areas = _WATCHED_PLAYERS[entity_id]
        self._pending_task = asyncio.ensure_future(
            self._delayed_fun_fact(entity_id, title, app_name, target_areas)
        )

    async def _delayed_fun_fact(self, entity_id: str, title: str, app_name: str, target_areas: list[str]) -> None:
        try:
            await asyncio.sleep(90)

            # Verify still playing same title
            ws = getattr(self._container, "ha_ws_manager", None)
            if ws:
                current = ws.get_state(entity_id)
                if not current or current.get("state") != "playing":
                    _LOGGER.info("media_fun_fact_watcher.cancelled_not_playing", entity=entity_id)
                    return
                current_title = (current.get("attributes") or {}).get("media_title", "")
                if current_title != title:
                    _LOGGER.info("media_fun_fact_watcher.cancelled_title_changed", entity=entity_id)
                    return

            # Call the fun fact endpoint internally
            llm = self._container.llm_service
            prompt = (
                f"Share one short, genuinely interesting fun fact about this movie or TV show. "
                f"Title: \"{title}\". App: {app_name}. "
                f"Keep it to 2 sentences max. Be specific and surprising."
            )

            try:
                # Use operational backend (Gemini) for grounded search
                fun_fact = (await llm.generate_text_grounded(prompt, timeout_s=30.0)).strip()
            except Exception:
                try:
                    fun_fact = (await llm.generate_text(prompt, timeout_s=30.0)).strip()
                except Exception as exc:
                    _LOGGER.warning("media_fun_fact_watcher.llm_failed", title=title, exc=str(exc)[:80])
                    return

            if not fun_fact or len(fun_fact) < 20:
                _LOGGER.warning("media_fun_fact_watcher.empty_response", title=title)
                return

            _LOGGER.info("media_fun_fact_watcher.announcing", title=title[:40], chars=len(fun_fact), areas=target_areas)

            # Announce via speaker service
            speaker = getattr(self._container, "speaker_service", None)
            if speaker:
                await speaker.speak(fun_fact, target_areas=target_areas, area_aware=True)

            self._last_announced[entity_id] = time.time()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.error("media_fun_fact_watcher.error", exc=str(exc)[:120])
