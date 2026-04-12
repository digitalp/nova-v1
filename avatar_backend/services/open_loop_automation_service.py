from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from avatar_backend.services.open_loop_workflow_service import OpenLoopWorkflowService


class OpenLoopAutomationService:
    """Bounded background executor for due open-loop reminder and escalation actions."""

    def __init__(
        self,
        app,
        *,
        workflow_service: OpenLoopWorkflowService | None = None,
        interval_s: int = 15 * 60,
        startup_delay_s: int = 120,
        max_actions_per_run: int = 4,
    ) -> None:
        self._app = app
        self._workflow_service = workflow_service or OpenLoopWorkflowService()
        self._interval_s = max(60, int(interval_s))
        self._startup_delay_s = max(0, int(startup_delay_s))
        self._max_actions_per_run = max(1, int(max_actions_per_run))
        self._task: asyncio.Task | None = None
        self._log = structlog.get_logger()
        self._last_run_ts = ""
        self._last_run_summary: dict[str, Any] = {
            "planned": 0,
            "applied": 0,
            "applied_actions": [],
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="open_loop_automation")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def get_status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done()),
            "interval_s": self._interval_s,
            "startup_delay_s": self._startup_delay_s,
            "max_actions_per_run": self._max_actions_per_run,
            "last_run_ts": self._last_run_ts,
            "last_run_summary": dict(self._last_run_summary),
        }

    async def run_once(self) -> dict[str, Any]:
        db = getattr(self._app.state, "metrics_db", None)
        action_service = getattr(self._app.state, "action_service", None)
        ws_mgr = getattr(self._app.state, "ws_manager", None)
        if db is None or action_service is None:
            return {"planned": 0, "applied": 0, "applied_actions": []}

        persisted_rows = []
        for event in db.recent_event_history(self._max_actions_per_run * 20):
            persisted_rows.append(
                {
                    "kind": "persisted_event",
                    "ts": event.get("ts", ""),
                    "title": event.get("title", ""),
                    "summary": event.get("summary", ""),
                    "status": event.get("status", ""),
                    "event_id": event.get("event_id", ""),
                    "event_type": event.get("event_type", ""),
                    "event_source": event.get("event_source", ""),
                    "camera_entity_id": event.get("camera_entity_id", ""),
                    "data": event.get("data") or {},
                }
            )

        planned = self._workflow_service.plan_due_actions(
            persisted_rows,
            include_reminders=True,
            include_escalations=True,
            limit=self._max_actions_per_run,
        )
        applied_actions: list[dict[str, Any]] = []
        for item in planned:
            result = await action_service.handle_event_history_action(
                app=self._app,
                ws_mgr=ws_mgr,
                event_id=str(item.get("event_id") or ""),
                status=str(item.get("status") or "active"),
                workflow_action=str(item.get("workflow_action") or ""),
                title=str(item.get("title") or ""),
                summary=str(item.get("summary") or ""),
                event_type=str(item.get("event_type") or ""),
                event_source=str(item.get("event_source") or ""),
                open_loop_note=str(item.get("open_loop_note") or ""),
            )
            if result.get("ok"):
                applied_actions.append(
                    {
                        "event_id": result.get("event_id", ""),
                        "workflow_action": result.get("workflow_action"),
                        "status": result.get("status", ""),
                    }
                )

        summary = {
            "planned": len(planned),
            "applied": len(applied_actions),
            "applied_actions": applied_actions,
        }
        self._last_run_ts = datetime.now(timezone.utc).isoformat()
        self._last_run_summary = summary
        self._log.info("open_loop_automation.run", **summary)
        return summary

    async def _run(self) -> None:
        if self._startup_delay_s:
            await asyncio.sleep(self._startup_delay_s)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("open_loop_automation.failed", exc=str(exc))
            await asyncio.sleep(self._interval_s)
