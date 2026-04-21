"""
PromptSyncService — nightly background task that checks for new HA entities
and automatically integrates them into the system prompt at a configured hour.

Only runs if new entities are found (skips silently otherwise).
Uses the same logic as POST /admin/sync-prompt but with area enrichment.
"""
from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import structlog

_LOGGER = structlog.get_logger()
_SYNC_HOUR = 3   # 3am local time


class PromptSyncService:
    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        llm_service,
        prompt_file: Path,
        app,
    ) -> None:
        self._ha_url     = ha_url
        self._ha_token   = ha_token
        self._llm        = llm_service
        self._prompt_file = prompt_file
        self._app        = app
        self._task: asyncio.Task | None = None

    @property
    def _container(self):
        return self._app.state._container

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="prompt_sync_nightly")

    async def _loop(self) -> None:
        while True:
            now    = datetime.datetime.now()
            target = now.replace(hour=_SYNC_HOUR, minute=0, second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            wait_s = (target - now).total_seconds()
            _LOGGER.info("prompt_sync.sleeping", next_run=target.strftime("%Y-%m-%d %H:%M"), wait_h=round(wait_s / 3600, 1))
            await asyncio.sleep(wait_s)
            try:
                await self._run()
            except Exception as exc:
                _LOGGER.warning("prompt_sync.nightly_failed", exc=str(exc))

    async def _run(self) -> None:
        import httpx
        from avatar_backend.services.prompt_bootstrap import (
            extract_known_entity_ids,
            fetch_area_mapping,
            summarise_new_entities,
        )
        from avatar_backend.services.session_manager import SessionManager

        _LOGGER.info("prompt_sync.nightly_start")

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._ha_url}/api/states",
                headers={"Authorization": f"Bearer {self._ha_token}"},
            )
            resp.raise_for_status()
            all_states = resp.json()

        current_prompt = self._prompt_file.read_text() if self._prompt_file.exists() else ""
        known          = extract_known_entity_ids(current_prompt)
        area_by_entity = await fetch_area_mapping(self._ha_url, self._ha_token)
        new_summary    = summarise_new_entities(all_states, known, area_by_entity=area_by_entity)

        if not new_summary:
            _LOGGER.info("prompt_sync.nightly_up_to_date")
            return

        new_count = new_summary.count("\n  ")
        _LOGGER.info("prompt_sync.nightly_integrating", new_count=new_count)

        integration_request = (
            "You are updating the system prompt for Nova, an AI home automation controller.\n\n"
            "Here is the current system prompt:\n```\n" + current_prompt + "\n```\n\n"
            "The following new Home Assistant entities have been discovered. Each line shows:\n"
            "  entity_id | friendly name | current state [device_class] — Area\n\n"
            + new_summary + "\n\n"
            "Instructions:\n"
            "- Add each entity to the most appropriate existing section, guided by its Area.\n"
            "- Skip clear infrastructure noise (connectivity, cloud connection sensors).\n"
            "- Preserve the exact structure, tone, and formatting of the original prompt.\n"
            "- Return ONLY the complete updated system prompt — no explanation, no markdown fences."
        )

        updated_prompt = await self._llm.generate_text(integration_request, timeout_s=300.0)

        if not updated_prompt or len(updated_prompt) < len(current_prompt) // 2:
            _LOGGER.warning("prompt_sync.nightly_response_too_short")
            return
        if len(updated_prompt) > len(current_prompt) * 3:
            _LOGGER.warning("prompt_sync.nightly_response_too_long")
            return

        updated_prompt = "".join(c for c in updated_prompt if c >= " " or c in "\n\r\t")

        backup = self._prompt_file.parent / "system_prompt.txt.bak"
        backup.write_text(current_prompt)
        self._prompt_file.write_text(updated_prompt)

        # Hot-reload session manager + proactive service
        self._container.session_manager = SessionManager(updated_prompt)
        proactive = getattr(self._container, "proactive_service", None)
        if proactive is not None:
            proactive.update_system_prompt(updated_prompt)

        _LOGGER.info(
            "prompt_sync.nightly_done",
            new_entities=new_count,
            old_lines=len(current_prompt.splitlines()),
            new_lines=len(updated_prompt.splitlines()),
        )
