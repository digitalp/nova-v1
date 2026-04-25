"""
Parental family model endpoints: override queue, family status, timeline,
resources, policies, and audit log.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from avatar_backend.bootstrap.container import AppContainer, get_container

from .common import _require_session

router = APIRouter()

@router.get("/parental/overrides")
async def list_overrides(request: Request, status: str = "", container: AppContainer = Depends(get_container)):
    """List parental override requests."""
    _require_session(request, min_role="viewer")
    db = getattr(container, "metrics_db", None)
    if not db:
        return {"overrides": []}
    return {"overrides": db.list_overrides(status=status or None, limit=100)}


@router.post("/parental/overrides/{override_id}/approve")
async def approve_override(override_id: int, request: Request, container: AppContainer = Depends(get_container)):
    """Approve a pending override request."""
    _require_session(request, min_role="admin")
    db = getattr(container, "metrics_db", None)
    if not db:
        return {"ok": False, "error": "DB not available"}
    result = db.resolve_override(override_id, status="approved", resolved_by="admin")
    if not result:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Override not found"}, status_code=404)
    return {"ok": True, "override": result}


@router.post("/parental/overrides/{override_id}/deny")
async def deny_override(override_id: int, request: Request, container: AppContainer = Depends(get_container)):
    """Deny a pending override request."""
    _require_session(request, min_role="admin")
    db = getattr(container, "metrics_db", None)
    if not db:
        return {"ok": False, "error": "DB not available"}
    result = db.resolve_override(override_id, status="denied", resolved_by="admin")
    if not result:
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "error": "Override not found"}, status_code=404)
    return {"ok": True, "override": result}


@router.get("/parental/family")
async def get_family_status(request: Request, container: AppContainer = Depends(get_container)):
    """Return typed family model: people, their states, and active policies."""
    _require_session(request, min_role="viewer")
    fs = getattr(container, "family_service", None)
    if fs is None:
        return {"configured": False, "people": [], "states": []}
    people_out = []
    for person in fs.all_people():
        state = fs.get_child_state(person.id) if person.role == "child" else {}
        policies = [{"id": p.id, "rule_type": p.rule_type, "active": p.active}
                    for p in fs.get_policies_for(person.id)]
        resources = [{"id": r.id, "kind": r.kind, "device_number": r.device_number}
                     for r in fs.get_resources_for(person.id)]
        from datetime import datetime as _dt
        _today = _dt.now().strftime("%A").lower()
        _school_nights = [s.lower() for s in (person.school_nights or [])]
        _is_school = _today in _school_nights
        _bedtime = (person.bedtime_weekday if _is_school else person.bedtime_weekend) if person.role == "child" else ""
        people_out.append({
            "id": person.id,
            "display_name": person.display_name,
            "role": person.role,
            "state": state,
            "policies": policies,
            "resources": resources,
            "bedtime_tonight": _bedtime,
            "school_night": _is_school if person.role == "child" else None,
        })
    return {"configured": True, "people": people_out}


@router.get("/parental/timeline")
async def get_timeline(request: Request, container: AppContainer = Depends(get_container),
                       days: int = 3):
    """Merged timeline of state changes and tool calls, newest first."""
    _require_session(request, min_role="viewer")
    db = getattr(container, "metrics_db", None)
    if db is None:
        return {"events": []}
    events = []
    # State change history
    for row in db.list_child_state_history(limit=200):
        events.append({
            "ts": row["ts"],
            "kind": "state_change",
            "person_id": row["person_id"],
            "state": row["state"],
            "reason": row["reason"],
        })
    # Parental tool audit
    for row in db.list_parental_audit(limit=200):
        import json as _json
        try:
            args = _json.loads(row.get("args") or "{}")
        except Exception:
            args = {}
        person_id = args.get("person_id") or args.get("device_number") or ""
        events.append({
            "ts": row["ts"],
            "kind": "tool_call",
            "person_id": person_id,
            "tool": row["tool"],
            "success": bool(row["success"]),
            "message": row["message"],
        })
    events.sort(key=lambda e: e["ts"], reverse=True)
    return {"events": events[:300]}



class ResourceCreate(BaseModel):
    person_id: str
    device_number: str          # MDM device "number" field


class PolicyCreate(BaseModel):
    person_id: str
    resource_id: str
    required_task_ids: list[str] = []
    enforce_from: str = "15:00"
    enforce_until: str = "21:00"


@router.post("/parental/resources")
async def create_parental_resource(
    body: ResourceCreate,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    """Add an MDM device resource for a person and return the new resource id."""
    import json as _json
    _require_session(request)
    fs = getattr(container, "family_service", None)
    if not fs:
        return JSONResponse({"ok": False, "error": "family service not available"}, status_code=503)
    state_path = fs._path
    data = _json.loads(state_path.read_text())

    person_id = body.person_id.strip().lower()
    # Prevent duplicate resources for same person + device
    for r in data.get("resources", []):
        if r.get("owner_id") == person_id and r.get("device_number", "").lower() == body.device_number.lower():
            return JSONResponse({"ok": True, "resource_id": r["id"], "existed": True})

    resource_id = f"{person_id}_device"
    # Ensure unique id
    existing_ids = {r["id"] for r in data.get("resources", [])}
    base = resource_id
    counter = 2
    while resource_id in existing_ids:
        resource_id = f"{base}_{counter}"
        counter += 1

    data.setdefault("resources", []).append({
        "id": resource_id,
        "kind": "mdm_device",
        "device_number": body.device_number,
        "owner_id": person_id,
    })
    state_path.write_text(_json.dumps(data, indent=2))
    fs.reload()
    return JSONResponse({"ok": True, "resource_id": resource_id, "existed": False})


@router.post("/parental/policies")
async def create_parental_policy(
    body: PolicyCreate,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    """Add a homework gate policy for a person + resource."""
    import json as _json
    _require_session(request)
    fs = getattr(container, "family_service", None)
    if not fs:
        return JSONResponse({"ok": False, "error": "family service not available"}, status_code=503)
    state_path = fs._path
    data = _json.loads(state_path.read_text())

    person_id = body.person_id.strip().lower()
    # Prevent duplicate policy for same person + rule_type
    for p in data.get("policies", []):
        if (p.get("subject_id") == person_id
                and p.get("rule_type") == "requires_task_before_entertainment"):
            return JSONResponse({"ok": False, "error": "policy already exists for this person"}, status_code=409)

    policy_id = f"{person_id}_homework_gate"
    existing_ids = {p["id"] for p in data.get("policies", [])}
    base = policy_id
    counter = 2
    while policy_id in existing_ids:
        policy_id = f"{base}_{counter}"
        counter += 1

    data.setdefault("policies", []).append({
        "id": policy_id,
        "subject_id": person_id,
        "resource_id": body.resource_id,
        "rule_type": "requires_task_before_entertainment",
        "active": True,
        "required_task_ids": body.required_task_ids,
        "enforce_from": body.enforce_from,
        "enforce_until": body.enforce_until,
    })
    state_path.write_text(_json.dumps(data, indent=2))
    fs.reload()
    return JSONResponse({"ok": True, "policy_id": policy_id})


class PolicyPatch(BaseModel):
    required_task_ids: list[str] | None = None
    enforce_from: str | None = None
    enforce_until: str | None = None
    active: bool | None = None


@router.get("/parental/policies")
async def get_parental_policies(request: Request, container: AppContainer = Depends(get_container)):
    """Return all homework-gate policies with full editable fields."""
    _require_session(request)
    fs = getattr(container, "family_service", None)
    sb = getattr(container, "scoreboard_service", None)
    if not fs:
        return JSONResponse({"policies": [], "tasks": []})
    policies = []
    for pol in fs.get_homework_gate_policies():
        person = fs.get_person(pol.subject_id)
        resource = fs.get_resource(pol.resource_id)
        policies.append({
            "id": pol.id,
            "subject_id": pol.subject_id,
            "subject_name": person.display_name if person else pol.subject_id,
            "resource_id": pol.resource_id,
            "device_number": resource.device_number if resource else "",
            "active": pol.active,
            "required_task_ids": pol.required_task_ids,
            "enforce_from": pol.enforce_from,
            "enforce_until": pol.enforce_until,
        })
    tasks = []
    if sb:
        tasks = [{"id": t["id"], "label": t.get("label", t["id"])}
                 for t in sb.get_config().get("tasks", [])]
    return JSONResponse({"policies": policies, "tasks": tasks})


@router.patch("/parental/policies/{policy_id}")
async def patch_parental_policy(
    policy_id: str,
    body: PolicyPatch,
    request: Request,
    container: AppContainer = Depends(get_container),
):
    """Update required_task_ids, enforce_from, enforce_until, or active on a policy."""
    import json as _json
    from pathlib import Path as _Path
    _require_session(request)
    fs = getattr(container, "family_service", None)
    if not fs:
        return JSONResponse({"ok": False, "error": "family service not available"}, status_code=503)
    state_path = fs._path
    if not state_path.exists():
        return JSONResponse({"ok": False, "error": "family_state.json not found"}, status_code=404)
    data = _json.loads(state_path.read_text())
    updated = False
    for pol in data.get("policies", []):
        if pol.get("id") == policy_id:
            if body.required_task_ids is not None:
                pol["required_task_ids"] = body.required_task_ids
            if body.enforce_from is not None:
                pol["enforce_from"] = body.enforce_from
            if body.enforce_until is not None:
                pol["enforce_until"] = body.enforce_until
            if body.active is not None:
                pol["active"] = body.active
            updated = True
            break
    if not updated:
        return JSONResponse({"ok": False, "error": "policy not found"}, status_code=404)
    state_path.write_text(_json.dumps(data, indent=2))
    fs.reload()
    return JSONResponse({"ok": True})


@router.get("/parental/audit")
async def list_parental_audit(request: Request, container: AppContainer = Depends(get_container)):
    """Return recent parental LLM tool call audit log."""
    _require_session(request, min_role="viewer")
    db = getattr(container, "metrics_db", None)
    if db is None:
        return {"audit": []}
    return {"audit": db.list_parental_audit(limit=100)}
