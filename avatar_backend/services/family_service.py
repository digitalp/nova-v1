"""Typed household model: people, resources, policies, per-child state machine."""
from __future__ import annotations

import json
import structlog
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_LOGGER = structlog.get_logger()


@dataclass
class Person:
    id: str
    role: str  # child | guardian | adult
    display_name: str
    guardian_ids: list[str] = field(default_factory=list)
    bedtime_weekday: str = ""   # HH:MM, empty = no bedtime rule
    bedtime_weekend: str = ""
    school_nights: list[str] = field(
        default_factory=lambda: ["monday", "tuesday", "wednesday", "thursday", "sunday"]
    )


@dataclass
class Resource:
    id: str
    kind: str  # mdm_device | ha_entity | app_group
    owner_id: str
    device_number: str = ""          # mdm_device: Headwind device number
    entity_id: str = ""              # ha_entity
    package_names: list[str] = field(default_factory=list)  # app_group


@dataclass
class Policy:
    id: str
    subject_id: str
    resource_id: str
    rule_type: str  # requires_task_before_entertainment | bedtime_block | screen_time_budget
    active: bool = True
    required_task_ids: list[str] = field(default_factory=list)
    # screen_time_budget
    daily_minutes: int = 0
    # enforcement window (only enforce between these hours, e.g. "15:00"-"21:00")
    enforce_from: str = ""
    enforce_until: str = ""


CHILD_STATES = ("allowed", "warned", "grace_period", "restricted", "overridden")


class FamilyService:
    """Loads family_state.json and exposes typed people/resources/policies.

    Also acts as façade for the per-child state machine stored in MetricsDB.
    Falls back gracefully if family_state.json does not exist.
    """

    def __init__(self, state_path: Path, db) -> None:
        self._path = state_path
        self._db = db
        self._people: dict[str, Person] = {}
        self._resources: dict[str, Resource] = {}
        self._policies: list[Policy] = []
        self._load()

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            _LOGGER.info("family_service.no_state_file", path=str(self._path))
            return
        try:
            data = json.loads(self._path.read_text())
            for p in data.get("people", []):
                person = Person(
                    id=p["id"],
                    role=p.get("role", "adult"),
                    display_name=p.get("display_name", p["id"].title()),
                    guardian_ids=p.get("guardian_ids", []),
                    bedtime_weekday=p.get("bedtime_weekday", ""),
                    bedtime_weekend=p.get("bedtime_weekend", ""),
                    school_nights=p.get("school_nights",
                                        ["monday", "tuesday", "wednesday", "thursday", "sunday"]),
                )
                self._people[person.id] = person
            for r in data.get("resources", []):
                resource = Resource(
                    id=r["id"],
                    kind=r.get("kind", "mdm_device"),
                    owner_id=r.get("owner_id", ""),
                    device_number=r.get("device_number", ""),
                    entity_id=r.get("entity_id", ""),
                    package_names=r.get("package_names", []),
                )
                self._resources[resource.id] = resource
            for pol in data.get("policies", []):
                policy = Policy(
                    id=pol["id"],
                    subject_id=pol["subject_id"],
                    resource_id=pol.get("resource_id", ""),
                    rule_type=pol["rule_type"],
                    active=pol.get("active", True),
                    required_task_ids=pol.get("required_task_ids", []),
                    daily_minutes=int(pol.get("daily_minutes", 0)),
                    enforce_from=pol.get("enforce_from", ""),
                    enforce_until=pol.get("enforce_until", ""),
                )
                self._policies.append(policy)
            _LOGGER.info("family_service.loaded",
                         people=len(self._people),
                         resources=len(self._resources),
                         policies=len(self._policies))
        except Exception as exc:
            _LOGGER.warning("family_service.load_error", exc=str(exc))

    def reload(self) -> None:
        self._people.clear()
        self._resources.clear()
        self._policies.clear()
        self._load()

    # ── People ────────────────────────────────────────────────────────────────

    def get_children(self) -> list[Person]:
        return [p for p in self._people.values() if p.role == "child"]

    def get_guardians(self) -> list[Person]:
        return [p for p in self._people.values() if p.role == "guardian"]

    def get_person(self, person_id: str) -> Person | None:
        return self._people.get(person_id)

    def all_people(self) -> list[Person]:
        return list(self._people.values())

    # ── Resources ─────────────────────────────────────────────────────────────

    def get_resources_for(self, person_id: str) -> list[Resource]:
        return [r for r in self._resources.values() if r.owner_id == person_id]

    def get_resource(self, resource_id: str) -> Resource | None:
        return self._resources.get(resource_id)

    # ── Policies ──────────────────────────────────────────────────────────────

    def get_policies_for(self, person_id: str) -> list[Policy]:
        return [p for p in self._policies if p.subject_id == person_id and p.active]

    def get_homework_gate_policies(self) -> list[Policy]:
        return [p for p in self._policies
                if p.rule_type == "requires_task_before_entertainment" and p.active]

    # ── State machine (façade over DB) ────────────────────────────────────────

    def get_child_state(self, person_id: str) -> dict:
        return self._db.get_child_state(person_id)

    def set_child_state(self, person_id: str, state: str,
                        reason: str = "", expires_ts: str | None = None) -> dict:
        if state not in CHILD_STATES:
            raise ValueError(f"Invalid state: {state!r}. Must be one of {CHILD_STATES}")
        return self._db.set_child_state(person_id, state, reason, expires_ts)

    def list_child_states(self) -> list[dict]:
        return self._db.list_child_states()

    # ── Summary for LLM tool ──────────────────────────────────────────────────

    def describe_person(self, person_id: str) -> str:
        person = self.get_person(person_id)
        if not person:
            return f"Unknown person: {person_id}"
        state = self.get_child_state(person_id)
        resources = self.get_resources_for(person_id)
        policies = self.get_policies_for(person_id)
        lines = [
            f"{person.display_name} ({person.role})",
            f"  State: {state.get('state', 'allowed')} — {state.get('reason', '')}",
            f"  Devices: {', '.join(r.device_number or r.id for r in resources) or 'none'}",
            f"  Active policies: {', '.join(p.rule_type for p in policies) or 'none'}",
        ]
        return "\n".join(lines)
