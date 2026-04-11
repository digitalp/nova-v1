from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class OpenLoopService:
    """Compatibility-first lifecycle metadata for unresolved event loops."""

    _UNRESOLVED_STATUSES = {"active", "acknowledged", "dismissed", "snoozed"}
    _STALE_AFTER_S = 4 * 3600
    _REMINDER_REPEAT_AFTER_S = 6 * 3600
    _HIGH_PRIORITY_AFTER_S = 24 * 3600
    _REMINDER_NOTE = "Reminder sent for follow-up"
    _ESCALATION_NOTES = {
        "medium": "Escalated for follow-up",
        "high": "Urgent escalation required",
    }

    def enrich_event_data(
        self,
        *,
        ts: str,
        status: str,
        data: dict[str, Any] | None = None,
        open_loop_note: str | None = None,
        admin_note: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(data or {})
        started_ts = str(payload.get("open_loop_started_ts") or ts)
        updated_ts = str(payload.get("open_loop_updated_ts") or ts)
        resolved_ts = str(payload.get("open_loop_resolved_ts") or "")

        payload["open_loop_state"] = status
        payload["open_loop_active"] = status in self._UNRESOLVED_STATUSES
        payload["open_loop_started_ts"] = started_ts
        payload["open_loop_updated_ts"] = updated_ts
        if open_loop_note is not None:
            payload["open_loop_note"] = open_loop_note
        if admin_note:
            payload["admin_note"] = admin_note
            payload["admin_note_ts"] = updated_ts
        if status == "resolved":
            payload["open_loop_resolved_ts"] = resolved_ts or updated_ts
        else:
            payload.pop("open_loop_resolved_ts", None)
        return payload

    def apply_status_transition(
        self,
        *,
        status: str,
        data: dict[str, Any] | None = None,
        open_loop_note: str | None = None,
        admin_note: str | None = None,
        now_iso: str | None = None,
    ) -> dict[str, Any]:
        ts = now_iso or datetime.now(timezone.utc).isoformat()
        payload = dict(data or {})
        payload.setdefault("open_loop_started_ts", ts)
        payload["open_loop_updated_ts"] = ts
        return self.enrich_event_data(
            ts=payload["open_loop_started_ts"],
            status=status,
            data=payload,
            open_loop_note=open_loop_note,
            admin_note=admin_note,
        )

    def apply_policy_update(
        self,
        *,
        data: dict[str, Any] | None = None,
        reminder_sent: bool = False,
        escalation_level: str | None = None,
        now_iso: str | None = None,
    ) -> dict[str, Any]:
        ts = now_iso or datetime.now(timezone.utc).isoformat()
        payload = dict(data or {})
        payload.setdefault("open_loop_started_ts", ts)
        payload.setdefault("open_loop_updated_ts", ts)
        if reminder_sent:
            payload["open_loop_last_reminder_ts"] = ts
            payload["open_loop_reminder_count"] = int(payload.get("open_loop_reminder_count") or 0) + 1
        if escalation_level:
            payload["open_loop_escalation_level"] = escalation_level
            payload["open_loop_last_escalation_ts"] = ts
        return payload

    def extract_summary_fields(self, data: dict[str, Any] | None, *, status: str, fallback_ts: str) -> dict[str, Any]:
        payload = dict(data or {})
        started_ts = str(payload.get("open_loop_started_ts") or fallback_ts or "")
        updated_ts = str(payload.get("open_loop_updated_ts") or fallback_ts or "")
        resolved_ts = str(payload.get("open_loop_resolved_ts") or "")
        last_reminder_ts = str(payload.get("open_loop_last_reminder_ts") or "")
        last_escalation_ts = str(payload.get("open_loop_last_escalation_ts") or "")
        active = bool(payload.get("open_loop_active", status in self._UNRESOLVED_STATUSES))
        age_s = self._age_seconds(started_ts)
        stale = active and age_s is not None and age_s >= self._STALE_AFTER_S
        reminder_age_s = self._age_seconds(last_reminder_ts)
        reminder_due = bool(
            active and stale and (
                not last_reminder_ts
                or reminder_age_s is None
                or reminder_age_s >= self._REMINDER_REPEAT_AFTER_S
            )
        )
        reminder_state = self._reminder_state(active=active, reminder_due=reminder_due, last_reminder_ts=last_reminder_ts)
        escalation_level = self._escalation_level_for(
            active=active,
            age_s=age_s,
            persisted_level=str(payload.get("open_loop_escalation_level") or ""),
        )
        escalation_due = bool(active and escalation_level == "high" and str(payload.get("open_loop_escalation_level") or "") != "high")
        return {
            "open_loop_state": str(payload.get("open_loop_state") or status or ""),
            "open_loop_active": active,
            "open_loop_started_ts": started_ts,
            "open_loop_updated_ts": updated_ts,
            "open_loop_resolved_ts": resolved_ts,
            "open_loop_note": str(payload.get("open_loop_note") or ""),
            "open_loop_age_s": age_s,
            "open_loop_stale": stale,
            "open_loop_last_reminder_ts": last_reminder_ts,
            "open_loop_reminder_count": int(payload.get("open_loop_reminder_count") or 0),
            "open_loop_reminder_due": reminder_due,
            "open_loop_reminder_state": reminder_state,
            "open_loop_last_escalation_ts": last_escalation_ts,
            "open_loop_escalation_level": escalation_level,
            "open_loop_escalation_due": escalation_due,
            "open_loop_priority": self._priority_for(
                status=str(payload.get("open_loop_state") or status or ""),
                active=active,
                age_s=age_s,
                stale=stale,
            ),
        }

    def build_workflow_actions(self, data: dict[str, Any] | None, *, status: str, fallback_ts: str) -> list[dict[str, Any]]:
        summary = self.extract_summary_fields(data, status=status, fallback_ts=fallback_ts)
        if not summary["open_loop_active"]:
            return []

        actions: list[dict[str, Any]] = []
        if summary["open_loop_reminder_due"]:
            actions.append(
                {
                    "action": "send_reminder",
                    "label": "Send Reminder",
                    "tone": "warn",
                    "requires_confirmation": True,
                    "confirm_text": "Mark a reminder as sent for this unresolved incident?",
                    "open_loop_note": self.default_note_for_workflow_action("send_reminder"),
                }
            )

        persisted_level = str((data or {}).get("open_loop_escalation_level") or "none")
        next_level = ""
        if summary["open_loop_escalation_due"]:
            if persisted_level != "high":
                next_level = "high"
        elif persisted_level in {"", "none"}:
            next_level = "medium"
        elif persisted_level == "medium":
            next_level = "high"
        if next_level:
            label = "Escalate High" if next_level == "high" else "Escalate"
            actions.append(
                {
                    "action": f"escalate_{next_level}",
                    "label": label,
                    "tone": "danger" if next_level == "high" else "warn",
                    "requires_confirmation": True,
                    "confirm_text": f"Mark this unresolved incident as {next_level} priority?",
                    "open_loop_note": self.default_note_for_workflow_action(f"escalate_{next_level}"),
                }
            )
        return actions

    def default_note_for_workflow_action(self, action: str) -> str:
        if action == "send_reminder":
            return self._REMINDER_NOTE
        if action.startswith("escalate_"):
            level = action.split("_", 1)[1]
            return self._ESCALATION_NOTES.get(level, "")
        return ""

    @staticmethod
    def _age_seconds(started_ts: str) -> int | None:
        if not started_ts:
            return None
        try:
            started = datetime.fromisoformat(started_ts.replace("Z", "+00:00"))
        except Exception:
            return None
        return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))

    def _priority_for(self, *, status: str, active: bool, age_s: int | None, stale: bool) -> str:
        if not active:
            return "resolved"
        if age_s is not None and age_s >= self._HIGH_PRIORITY_AFTER_S:
            return "high"
        if status in {"dismissed", "snoozed"} or stale:
            return "medium"
        return "normal"

    @staticmethod
    def _reminder_state(*, active: bool, reminder_due: bool, last_reminder_ts: str) -> str:
        if not active:
            return "resolved"
        if reminder_due:
            return "due"
        if last_reminder_ts:
            return "sent"
        return "not_due"

    def _escalation_level_for(self, *, active: bool, age_s: int | None, persisted_level: str) -> str:
        if not active:
            return "resolved"
        if persisted_level == "high":
            return "high"
        if persisted_level == "medium":
            return "medium"
        if age_s is not None and age_s >= self._HIGH_PRIORITY_AFTER_S:
            return "high"
        return "none"
