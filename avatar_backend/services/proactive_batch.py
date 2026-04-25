"""Mixin for ProactiveService: LLM field helpers, batch triage, and heating delegation."""
from __future__ import annotations
import asyncio
import json
import re as _re
import time

import structlog

_LOGGER = structlog.get_logger()


def _format_exc(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


_BATCH_WINDOW_S = 60
_MAX_CHANGES = 20
_GLOBAL_ANNOUNCE_COOLDOWN_S = 300  # 5 minutes

_HOUSE_NEEDS_ATTENTION_ENTITY = "binary_sensor.house_needs_attention"
_HOUSE_ATTENTION_SUMMARY_ENTITY = "sensor.house_attention_summary"
_HOUSE_ATTENTION_NORMAL_STATES = {"", "unknown", "unavailable", "home looks normal"}


class ProactiveBatchMixin:
    """LLM field helpers, batch triage loop, and heating loop — mixed into ProactiveService."""
    def _active_llm_fields(self) -> dict[str, str]:
        provider = getattr(self._llm, "provider_name", "unknown")
        model = getattr(self._llm, "model_name", "unknown")
        return {
            "llm_provider": provider,
            "llm_model": model,
            "llm_tag": f"{provider}:{model}",
        }

    def _local_llm_fields(self) -> dict[str, str]:
        provider = "ollama"
        model = getattr(self._llm, "local_text_model_name", "unknown")
        return {
            "llm_provider": provider,
            "llm_model": model,
            "llm_tag": f"{provider}:{model}",
        }

    def _fast_local_llm_fields(self) -> dict[str, str]:
        provider = "ollama"
        model = getattr(self._llm, "fast_local_text_model_name", getattr(self._llm, "local_text_model_name", "unknown"))
        return {
            "llm_provider": provider,
            "llm_model": model,
            "llm_tag": f"{provider}:{model}",
        }

    def _gemini_llm_fields(self) -> dict[str, str]:
        provider = getattr(self._llm, "gemini_vision_provider_name", "google")
        model = getattr(self._llm, "gemini_vision_effective_model_name", "gemini")
        return {
            "llm_provider": provider,
            "llm_model": model,
            "llm_tag": f"{provider}:{model}",
        }


    async def _batch_loop(self) -> None:
        while True:
            await asyncio.sleep(_BATCH_WINDOW_S)
            if not self._queue:
                continue
            batch = self._queue[:_MAX_CHANGES]
            self._queue.clear()
            try:
                await self._triage(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("proactive.triage_error", exc=str(exc))

    async def _triage(self, changes: list[dict]) -> None:
        """Ask the LLM if any of these state changes warrant a spoken announcement."""

        # Global rate limit — don't announce if we just announced recently
        since_last = time.monotonic() - self._last_announce_time
        if since_last < _GLOBAL_ANNOUNCE_COOLDOWN_S:
            _LOGGER.debug(
                "proactive.global_cooldown_active",
                seconds_remaining=int(_GLOBAL_ANNOUNCE_COOLDOWN_S - since_last),
            )
            return

        # Resolve current state for each queued entity.
        # If a binary_sensor that transitioned to "on" has already returned to "off",
        # the event is transient — drop it entirely to prevent stale announcements
        # (e.g. "back door opened" reported 90 seconds after the door was closed).
        resolved_changes = []
        for c in changes:
            current = await self._ha.get_entity_state(c["entity_id"])
            current_val = (current or {}).get("state", "")
            domain_c = c["entity_id"].split(".")[0]
            if domain_c == "binary_sensor" and c["new"] == "on" and current_val == "off":
                age_s = int(time.monotonic() - c.get("queued_at", time.monotonic()))
                _LOGGER.info(
                    "proactive.event_resolved",
                    entity_id=c["entity_id"],
                    age_s=age_s,
                    hint="binary_sensor returned to off before triage — skipping",
                )
                continue
            resolved_changes.append(c)

        if not resolved_changes:
            _LOGGER.debug("proactive.all_resolved", hint="all queued events already resolved")
            return
        changes = resolved_changes

        rendered_lines: list[str] = []
        for c in changes:
            rendered_lines.append(await self._render_change_for_triage(c))
        lines = "\n".join(rendered_lines)
        entity_ids = [c["entity_id"] for c in changes]
        _LOGGER.info("proactive.triaging", n_changes=len(changes), entities=entity_ids)

        prompt_ctx = self._system_prompt[:3000]

        prompt = (
            "You are Nova's proactive home monitor. Review these Home Assistant state "
            "changes and decide if any warrant a spoken announcement.\n\n"
            f"Home context:\n{prompt_ctx}\n\n"
            f"State changes:\n{lines}\n\n"
            "STRICT RULES — the default answer is NO. Only announce if the event:\n"
            "  • Is a genuine safety or security concern (alarm triggered, unexpected "
            "door/lock change, smoke/CO detector, flood)\n"
            "  • Requires immediate human action (door left open, critical alert)\n"
            "  • Is a clear exception or anomaly that the household would want to know "
            "RIGHT NOW and could not figure out themselves\n\n"
            "DO NOT ANNOUNCE any of the following — return {\"announce\": false}:\n"
            "  • Motion sensors, presence sensors, occupancy sensors detecting movement\n"
            "  • Routine door open/close during normal waking hours\n"
            "  • Any light, switch, or media player change\n"
            "  • Person/device_tracker arriving or leaving home\n"
            "  • Climate mode or setpoint changes\n"
            "  • Any binary_sensor that is merely reporting a normal condition\n"
            "  • Anything already handled by a dedicated HA automation\n\n"
            "Speak as Nova, naturally in first person. Keep it brief.\n\n"
            "Reply with JSON only (no markdown fences):\n"
            '{"announce": true, "message": "...", "priority": "normal"}\n'
            "or\n"
            '{"announce": false}'
        )

        try:
            raw = await self._llm.generate_text_local_fast_resilient(
                prompt,
                timeout_s=45.0,
                retry_delay_s=2.0,
                fallback_timeout_s=20.0,
                purpose="proactive_triage",
            )
        except Exception as exc:
            _LOGGER.warning("proactive.llm_failed", exc=_format_exc(exc))
            return

        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        # Extract first JSON object — llama3.1 often appends prose after the JSON
        import re as _re
        m = _re.search(r'\{.*?\}', raw, _re.DOTALL)
        if not m:
            _LOGGER.warning("proactive.bad_json", raw=raw[:300])
            return
        try:
            result = json.loads(m.group())
        except json.JSONDecodeError:
            _LOGGER.warning("proactive.bad_json", raw=raw[:300])
            return

        if not result.get("announce"):
            _LOGGER.debug("proactive.no_action")
            if self._decision_log:
                self._decision_log.record(
                    "triage_silence",
                    entities=[c["entity_id"] for c in changes],
                    reason="LLM: no announcement needed",
                    **self._active_llm_fields(),
                )
            return

        message = (result.get("message") or "").strip()
        priority = result.get("priority", "normal")
        if priority not in ("normal", "alert"):
            priority = "normal"

        if not message:
            return

        message = await self._augment_announcement_message(changes, message)

        _LOGGER.info("proactive.announcing", chars=len(message), priority=priority)
        if self._decision_log:
            self._decision_log.record(
                "triage_announce",
                entities=[c["entity_id"] for c in changes],
                priority=priority,
                message=message,
                **self._active_llm_fields(),
            )

        now = time.monotonic()
        self._last_announce_time = now
        for c in changes:
            self._cooldowns[c["entity_id"]] = now

        await self._announce(message, priority)

    async def _render_change_for_triage(self, change: dict) -> str:
        entity_id = change["entity_id"]
        friendly = change["friendly"]
        old_val = change["old"]
        new_val = change["new"]

        if entity_id == _HOUSE_NEEDS_ATTENTION_ENTITY:
            summary = await self._get_house_attention_summary()
            if summary:
                return (
                    f"- {friendly} ({entity_id}): {old_val} → {new_val}"
                    f" | concrete issue: {summary}"
                )

        return f"- {friendly} ({entity_id}): {old_val} → {new_val}"

    async def _get_house_attention_summary(self) -> str | None:
        summary_state = await self._ha.get_entity_state(_HOUSE_ATTENTION_SUMMARY_ENTITY)
        summary = str((summary_state or {}).get("state", "")).strip()
        if summary.lower() in _HOUSE_ATTENTION_NORMAL_STATES:
            return None
        return summary

    async def _augment_announcement_message(self, changes: list[dict], message: str) -> str:
        if not any(change.get("entity_id") == _HOUSE_NEEDS_ATTENTION_ENTITY for change in changes):
            return message

        summary = await self._get_house_attention_summary()
        if not summary:
            return message
        return self._direct_house_attention_message(summary)

    @staticmethod
    def _direct_house_attention_message(summary: str) -> str:
        clean = " ".join(str(summary or "").split()).strip()
        if not clean:
            return "I've noticed something at home that needs attention."
        if clean[-1] not in ".!?":
            clean += "."
        return f"I've noticed {clean[0].lower() + clean[1:] if len(clean) > 1 else clean.lower()}"


    # ── Autonomous Heating Control ────────────────────────────────────────────

    # ── Heating subsystem (delegated to HeatingController) ────────────────────

    async def _heating_control_loop(self) -> None:
        """Delegates to HeatingController.run_loop()."""
        await self._heating.run_loop()

    async def run_heating_shadow_force(self, *, scenario: str = "winter") -> list[dict]:
        """Admin-triggered shadow-only heating evaluation. Delegates to HeatingController."""
        return await self._heating.run_force(scenario=scenario)
