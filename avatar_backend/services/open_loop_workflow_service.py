from __future__ import annotations

from typing import Any

from avatar_backend.services.open_loop_service import OpenLoopService


class OpenLoopWorkflowService:
    """Summarize due reminder/escalation work for unresolved incidents."""

    def __init__(self, *, open_loop_service: OpenLoopService | None = None) -> None:
        self._open_loop_service = open_loop_service or OpenLoopService()

    def summarize_due_work(
        self,
        items: list[dict[str, Any]],
        *,
        limit: int = 10,
    ) -> dict[str, Any]:
        normalized = self._normalize_items(items)

        normalized.sort(key=self._sort_key, reverse=True)
        reminder_due = [item for item in normalized if item.get("open_loop_reminder_due")]
        escalation_due = [item for item in normalized if item.get("open_loop_escalation_due")]
        stale = [item for item in normalized if item.get("open_loop_stale")]

        return {
            "counts": {
                "total_open_loops": sum(1 for item in normalized if item.get("open_loop_active")),
                "stale": len(stale),
                "reminder_due": len(reminder_due),
                "escalation_due": len(escalation_due),
                "high_priority": sum(1 for item in normalized if item.get("open_loop_priority") == "high"),
            },
            "next_actions": {
                "reminder_due": [self._serialize_item(item) for item in reminder_due[: max(1, limit)]],
                "escalation_due": [self._serialize_item(item) for item in escalation_due[: max(1, limit)]],
            },
        }

    def plan_due_actions(
        self,
        items: list[dict[str, Any]],
        *,
        include_reminders: bool = True,
        include_escalations: bool = True,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        normalized = self._normalize_items(items)
        planned: list[dict[str, Any]] = []
        for item in normalized:
            if include_escalations and item.get("open_loop_escalation_due"):
                level = str(item.get("open_loop_escalation_level") or "high")
                action = f"escalate_{level if level in {'medium', 'high'} else 'high'}"
                planned.append(self._serialize_planned_action(item, workflow_action=action))
            elif include_reminders and item.get("open_loop_reminder_due"):
                planned.append(self._serialize_planned_action(item, workflow_action="send_reminder"))
            if len(planned) >= max(1, limit):
                break
        return planned

    def _normalize_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            merged = dict(item)
            merged.update(
                self._open_loop_service.extract_summary_fields(
                    item.get("data") or {},
                    status=str(item.get("status") or ""),
                    fallback_ts=str(item.get("ts") or ""),
                )
            )
            normalized.append(merged)
        normalized.sort(key=self._sort_key, reverse=True)
        return normalized

    @staticmethod
    def _sort_key(item: dict[str, Any]) -> tuple[int, int, int]:
        priority_score = {"high": 3, "medium": 2, "normal": 1, "resolved": 0}.get(
            str(item.get("open_loop_priority") or "normal"),
            1,
        )
        escalation_score = 1 if item.get("open_loop_escalation_due") else 0
        age_s = int(item.get("open_loop_age_s") or 0)
        return (priority_score, escalation_score, age_s)

    @staticmethod
    def _serialize_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": item.get("event_id", ""),
            "title": item.get("title", ""),
            "summary": item.get("summary", ""),
            "status": item.get("status", ""),
            "event_type": item.get("event_type", ""),
            "event_source": item.get("event_source", ""),
            "open_loop_note": item.get("open_loop_note", ""),
            "open_loop_priority": item.get("open_loop_priority", ""),
            "open_loop_age_s": item.get("open_loop_age_s"),
            "open_loop_reminder_due": bool(item.get("open_loop_reminder_due")),
            "open_loop_escalation_due": bool(item.get("open_loop_escalation_due")),
            "open_loop_escalation_level": item.get("open_loop_escalation_level", ""),
        }

    def _serialize_planned_action(self, item: dict[str, Any], *, workflow_action: str) -> dict[str, Any]:
        payload = self._serialize_item(item)
        payload["workflow_action"] = workflow_action
        payload["status"] = str(item.get("status") or "active")
        payload["open_loop_note"] = self._open_loop_service.default_note_for_workflow_action(workflow_action)
        return payload
