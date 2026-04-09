#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path("/opt/avatar-server")
PROGRESS_DOC = ROOT / "docs" / "NOVA_V2_IMPLEMENTATION_PROGRESS.md"
MAIN_PY = ROOT / "avatar_backend" / "main.py"
METRICS_DB_PY = ROOT / "avatar_backend" / "services" / "metrics_db.py"


MILESTONE_ROW_RE = re.compile(
    r"^\|\s*`(?P<name>Milestone [1-6]|Overall)`\s*\|.*?\|\s*`(?P<pct>\d+)%`\s*\|",
    re.MULTILINE,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_progress_table(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for match in MILESTONE_ROW_RE.finditer(text):
        values[match.group("name")] = int(match.group("pct"))
    return values


def _rounded_average(values: list[int]) -> int:
    return int((sum(values) / len(values)) + 0.5)


def _has_file(relative_path: str) -> bool:
    return (ROOT / relative_path).exists()


def _contains(path: Path, needle: str) -> bool:
    return needle in _read(path)


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    progress_text = _read(PROGRESS_DOC)
    values = _parse_progress_table(progress_text)
    required_rows = [f"Milestone {index}" for index in range(1, 7)] + ["Overall"]
    missing_rows = [row for row in required_rows if row not in values]
    if missing_rows:
        for row in missing_rows:
            errors.append(f"Missing tracker table row: {row}")
        _report(errors, warnings)
        return 1

    milestone_values = [values[f"Milestone {index}"] for index in range(1, 7)]
    computed_overall = _rounded_average(milestone_values)
    if values["Overall"] != computed_overall:
        errors.append(
            f"Overall is {values['Overall']}%, but the rounded average of milestone rows is {computed_overall}%."
        )

    # Plan-based caps to prevent compatibility-first work from being scored as architecture-complete.
    if values["Milestone 1"] > 25:
        missing = [
            path
            for path in (
                "avatar_backend/services/event_bus.py",
                "avatar_backend/services/event_store.py",
                "avatar_backend/models/events.py",
            )
            if not _has_file(path)
        ]
        if missing:
            errors.append(
                "Milestone 1 exceeds 25% without core planned shared-event artifacts: "
                + ", ".join(missing)
            )

    if values["Milestone 2"] > 20 and not _has_file("avatar_backend/services/camera_event_service.py"):
        errors.append(
            "Milestone 2 exceeds 20% without the planned avatar_backend/services/camera_event_service.py."
        )

    if values["Milestone 3"] > 60 and not _has_file("avatar_backend/models/surface_messages.py"):
        errors.append(
            "Milestone 3 exceeds 60% without the planned avatar_backend/models/surface_messages.py."
        )

    if values["Milestone 4"] > 85:
        if not _contains(MAIN_PY, "ConversationService") or not _contains(MAIN_PY, "RealtimeVoiceService"):
            errors.append(
                "Milestone 4 exceeds 85% without both ConversationService and RealtimeVoiceService wired in main.py."
            )

    if values["Milestone 5"] > 70:
        missing = []
        if not _has_file("avatar_backend/routers/actions.py"):
            missing.append("avatar_backend/routers/actions.py")
        if not _contains(METRICS_DB_PY, "event_actions"):
            missing.append("event_actions persistence")
        if missing:
            errors.append(
                "Milestone 5 exceeds 70% without planned action-audit deliverables: "
                + ", ".join(missing)
            )

    if values["Milestone 6"] > 50:
        missing = []
        if not _contains(PROGRESS_DOC, "event timeline"):
            warnings.append(
                "Milestone 6 is above 50%; verify the admin event timeline and productization scope are actually landed."
            )
        if not _contains(METRICS_DB_PY, "conversation_sessions"):
            missing.append("conversation_sessions persistence")
        if not _contains(METRICS_DB_PY, "conversation_turn_summaries"):
            missing.append("conversation_turn_summaries persistence")
        if missing:
            errors.append(
                "Milestone 6 exceeds 50% without planned productization/persistence deliverables: "
                + ", ".join(missing)
            )

    if errors or warnings:
        _report(errors, warnings)
    else:
        print(
            "Tracker check passed: overall matches milestone math and no plan-based guardrail thresholds were exceeded."
        )
    return 1 if errors else 0


def _report(errors: list[str], warnings: list[str]) -> None:
    if errors:
        print("Tracker check failed:")
        for item in errors:
            print(f"- {item}")
    if warnings:
        print("Tracker check warnings:")
        for item in warnings:
            print(f"- {item}")


if __name__ == "__main__":
    sys.exit(main())
