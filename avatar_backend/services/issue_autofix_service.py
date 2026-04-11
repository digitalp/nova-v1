from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import structlog

_LOGGER = structlog.get_logger()


@dataclass(frozen=True)
class AutoFixPolicy:
    threshold: int
    window_s: int
    cooldown_s: int
    default_action: str
    allowed_actions: tuple[str, ...]


_POLICIES: dict[str, AutoFixPolicy] = {
    "home_assistant_timeout": AutoFixPolicy(
        threshold=3,
        window_s=300,
        cooldown_s=600,
        default_action="restart_watchers",
        allowed_actions=("restart_watchers", "recheck_home_assistant", "noop"),
    ),
    "sensor_watch_ws_disconnected": AutoFixPolicy(
        threshold=3,
        window_s=300,
        cooldown_s=600,
        default_action="restart_sensor_watch",
        allowed_actions=("restart_sensor_watch", "restart_watchers", "noop"),
    ),
    "proactive_ws_disconnected": AutoFixPolicy(
        threshold=3,
        window_s=300,
        cooldown_s=600,
        default_action="restart_proactive",
        allowed_actions=("restart_proactive", "restart_watchers", "noop"),
    ),
    "motion_clip_storage_unavailable": AutoFixPolicy(
        threshold=2,
        window_s=600,
        cooldown_s=900,
        default_action="refresh_motion_clip_storage",
        allowed_actions=("refresh_motion_clip_storage", "noop"),
    ),
}


class IssueAutoFixService:
    def __init__(self, app) -> None:
        self._app = app
        self._events: dict[str, deque[float]] = {}
        self._last_action_ts: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def report_issue(
        self,
        issue_kind: str,
        *,
        source: str = "",
        summary: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        policy = _POLICIES.get(issue_kind)
        if policy is None:
            return {"triggered": False, "reason": "unknown_issue"}

        async with self._lock:
            now = time.monotonic()
            bucket = self._events.setdefault(issue_kind, deque())
            bucket.append(now)
            cutoff = now - policy.window_s
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            count = len(bucket)
            if count < policy.threshold:
                return {"triggered": False, "count": count, "threshold": policy.threshold}
            last_action = self._last_action_ts.get(issue_kind, 0.0)
            if now - last_action < policy.cooldown_s:
                return {"triggered": False, "count": count, "reason": "cooldown"}
            self._last_action_ts[issue_kind] = now

        recommendation = await self._recommend_action(
            issue_kind,
            policy=policy,
            summary=summary,
            source=source,
            details=details or {},
        )
        action = recommendation.get("action") or policy.default_action
        if action not in policy.allowed_actions:
            action = policy.default_action

        outcome = await self._execute_action(
            issue_kind,
            action=action,
            source=source,
            summary=summary,
            details=details or {},
            count=count,
            ai_reason=recommendation.get("reason", ""),
        )
        return {"triggered": True, "count": count, **outcome}

    async def resolve_issue(self, issue_kind: str, *, source: str = "") -> None:
        async with self._lock:
            had_state = issue_kind in self._events or issue_kind in self._last_action_ts
            self._events.pop(issue_kind, None)
            self._last_action_ts.pop(issue_kind, None)
        if not had_state:
            return
        self._record_decision("auto_fix_issue_resolved", issue_kind=issue_kind, source=source)

    async def _recommend_action(
        self,
        issue_kind: str,
        *,
        policy: AutoFixPolicy,
        summary: str,
        source: str,
        details: dict[str, Any],
    ) -> dict[str, str]:
        llm = getattr(self._app.state, "llm_service", None)
        if llm is None:
            return {"action": policy.default_action, "reason": "llm_unavailable"}

        prompt = (
            "You are selecting a backend auto-remediation from a strict allowlist.\n"
            f"Issue kind: {issue_kind}\n"
            f"Source: {source or 'unknown'}\n"
            f"Summary: {summary or 'n/a'}\n"
            f"Details: {json.dumps(details, sort_keys=True)[:800]}\n"
            f"Allowed actions: {', '.join(policy.allowed_actions)}\n\n"
            "Return JSON only in the form "
            "{\"action\": \"one_of_the_allowed_actions\", \"reason\": \"short\"}.\n"
            "Never invent a new action."
        )
        try:
            raw = (await llm.generate_text_local(prompt, timeout_s=8.0)).strip()
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
            payload = json.loads(raw)
        except Exception as exc:
            return {"action": policy.default_action, "reason": f"fallback:{type(exc).__name__}"}
        action = str(payload.get("action") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if action not in policy.allowed_actions:
            return {"action": policy.default_action, "reason": "fallback:invalid_action"}
        return {"action": action, "reason": reason}

    async def _execute_action(
        self,
        issue_kind: str,
        *,
        action: str,
        source: str,
        summary: str,
        details: dict[str, Any],
        count: int,
        ai_reason: str,
    ) -> dict[str, Any]:
        success = False
        action_detail = ""

        if action == "noop":
            success = True
            action_detail = "no_remediation_applied"
        elif action == "recheck_home_assistant":
            ha = getattr(self._app.state, "ha_proxy", None)
            success = bool(ha and await ha.is_connected())
            action_detail = "ha_rechecked"
        elif action == "restart_sensor_watch":
            service = getattr(self._app.state, "sensor_watch", None)
            success = await self._restart_service(service)
            action_detail = "sensor_watch_restarted"
        elif action == "restart_proactive":
            service = getattr(self._app.state, "proactive_service", None)
            success = await self._restart_service(service)
            action_detail = "proactive_restarted"
        elif action == "restart_watchers":
            proactive = getattr(self._app.state, "proactive_service", None)
            sensor_watch = getattr(self._app.state, "sensor_watch", None)
            success = await self._restart_services([proactive, sensor_watch])
            action_detail = "watchers_restarted"
        elif action == "refresh_motion_clip_storage":
            service = getattr(self._app.state, "motion_clip_service", None)
            success = bool(service and await service.refresh_storage_status())
            action_detail = "motion_clip_storage_rechecked"

        self._record_decision(
            "auto_fix_issue_attempt",
            issue_kind=issue_kind,
            source=source,
            count=count,
            action=action,
            success=success,
            detail=action_detail,
            ai_reason=ai_reason,
        )
        self._record_event_history(
            issue_kind=issue_kind,
            source=source,
            summary=summary,
            details=details,
            action=action,
            success=success,
            ai_reason=ai_reason,
            action_detail=action_detail,
        )
        if success:
            await self.resolve_issue(issue_kind, source=f"autofix:{action}")
        return {"action": action, "success": success, "detail": action_detail}

    async def _restart_service(self, service: Any) -> bool:
        if service is None:
            return False
        await service.stop()
        await service.start()
        return True

    async def _restart_services(self, services: list[Any]) -> bool:
        alive = [service for service in services if service is not None]
        if not alive:
            return False
        for service in alive:
            await service.stop()
        for service in alive:
            await service.start()
        return True

    def _record_decision(self, kind: str, **fields: Any) -> None:
        log = getattr(self._app.state, "decision_log", None)
        if log is not None:
            log.record(kind, **fields)
        else:
            _LOGGER.info(kind, **fields)

    def _record_event_history(
        self,
        *,
        issue_kind: str,
        source: str,
        summary: str,
        details: dict[str, Any],
        action: str,
        success: bool,
        ai_reason: str,
        action_detail: str,
    ) -> None:
        db = getattr(self._app.state, "metrics_db", None)
        if db is None:
            return
        status = "resolved" if success else "active"
        db.insert_event_history(
            {
                "event_id": f"autofix-{issue_kind}-{time.time_ns()}",
                "event_type": "auto_fix_issue",
                "title": f"Auto-fix: {issue_kind}",
                "summary": summary or f"Auto-remediation attempted for {issue_kind}",
                "status": status,
                "event_source": source or "issue_autofix",
                "camera_entity_id": "",
                "data": {
                    "issue_kind": issue_kind,
                    "action": action,
                    "success": success,
                    "ai_reason": ai_reason,
                    "action_detail": action_detail,
                    "details": details,
                },
            }
        )
