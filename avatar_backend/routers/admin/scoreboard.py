"""Admin router — scoreboard management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from avatar_backend.bootstrap.container import AppContainer, get_container
from .common import _require_session

router = APIRouter()


def _svc(container: AppContainer):
    svc = getattr(container, "scoreboard_service", None)
    if svc is None:
        raise Exception("Scoreboard service not available")
    return svc


@router.get("/scoreboard")
async def get_scoreboard(request: Request, container: AppContainer = Depends(get_container)):
    """Public-ish: leaderboard + recent activity for the avatar page widget."""
    svc = _svc(container)
    cfg = svc.get_config()
    members = await svc.get_members()
    cfg = dict(cfg)
    cfg["members"] = members  # always reflect live face list
    return {
        "weekly": svc.weekly_scores(),
        "recent": svc.recent_logs(10),
        "config": cfg,
    }


@router.get("/scoreboard/logs")
async def get_logs(request: Request, days: int = 7, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = _svc(container)
    return {"logs": svc.all_logs(days)}


@router.delete("/scoreboard/logs/{log_id}")
async def delete_log(log_id: int, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = _svc(container)
    svc.delete_log(log_id)
    return {"ok": True}


@router.get("/scoreboard/config")
async def get_config(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    return _svc(container).get_config()


@router.post("/scoreboard/config")
async def save_config(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    svc = _svc(container)
    svc.save_config(body)
    return {"ok": True}


@router.patch("/scoreboard/tasks/{task_id}")
async def update_task(task_id: str, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    svc = _svc(container)
    cfg = svc.get_config()
    updated = False
    for t in cfg.get("tasks", []):
        if t["id"] == task_id:
            for key in ("label", "points", "cooldown_hours", "verification", "camera_entity_id", "requires_approval", "assigned_to", "reminders", "keywords"):
                if key in body:
                    t[key] = body[key]
            updated = True
            break
    if not updated:
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)
    svc.save_config(cfg)
    return {"ok": True}


@router.post("/scoreboard/widget-visibility")
async def set_widget_visibility(request: Request, container: AppContainer = Depends(get_container)):
    """Toggle the scoreboard widget on the avatar page."""
    _require_session(request, min_role="admin")
    body = await request.json()
    svc = _svc(container)
    cfg = svc.get_config()
    cfg["show_widget"] = bool(body.get("show_widget", True))
    svc.save_config(cfg)
    return {"ok": True, "show_widget": cfg["show_widget"]}


@router.post("/scoreboard/tasks")
async def add_task(request: Request, container: AppContainer = Depends(get_container)):
    """Add a new task to the scoreboard config."""
    _require_session(request, min_role="admin")
    body = await request.json()
    svc = _svc(container)
    cfg = svc.get_config()
    task_id = str(body.get("id") or "").strip().lower().replace(" ", "_")
    if not task_id or not body.get("label"):
        return JSONResponse({"ok": False, "error": "id and label required"}, status_code=400)
    if any(t["id"] == task_id for t in cfg.get("tasks", [])):
        return JSONResponse({"ok": False, "error": "Task id already exists"}, status_code=409)
    new_task = {
        "id": task_id,
        "label": str(body.get("label", "")).strip(),
        "points": int(body.get("points", 5)),
        "cooldown_hours": int(body.get("cooldown_hours", 16)),
        "verification": str(body.get("verification", "honour")),
        "camera_entity_id": body.get("camera_entity_id") or None,
        "requires_approval": bool(body.get("requires_approval", False)),
        "assigned_to": body.get("assigned_to") or [],
        "reminders": body.get("reminders") or [],
        "keywords": body.get("keywords") or [],
    }
    cfg.setdefault("tasks", []).append(new_task)
    svc.save_config(cfg)
    return {"ok": True, "task": new_task}


@router.delete("/scoreboard/tasks/{task_id}")
async def delete_task(task_id: str, request: Request, container: AppContainer = Depends(get_container)):
    """Remove a task from the scoreboard config."""
    _require_session(request, min_role="admin")
    svc = _svc(container)
    cfg = svc.get_config()
    before = len(cfg.get("tasks", []))
    cfg["tasks"] = [t for t in cfg.get("tasks", []) if t["id"] != task_id]
    if len(cfg["tasks"]) == before:
        return JSONResponse({"ok": False, "error": "Task not found"}, status_code=404)
    svc.save_config(cfg)
    return {"ok": True}


@router.post("/scoreboard/log")
async def manual_log(request: Request, container: AppContainer = Depends(get_container)):
    """Admin manual point award."""
    _require_session(request, min_role="admin")
    body = await request.json()
    svc = _svc(container)
    task_id = str(body.get("task_id") or "").strip()
    person = str(body.get("person") or "").strip().lower()
    task = svc.get_task(task_id)
    if not task or not person:
        return JSONResponse({"ok": False, "error": "task_id and person required"}, status_code=400)
    log_id = svc.record_chore(person, task_id, task["label"], task["points"], verified=True)
    return {"ok": True, "log_id": log_id}


@router.get("/scoreboard/notifications")
async def get_notifications(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    from avatar_backend.services.home_runtime import load_home_runtime_config
    rt = load_home_runtime_config()
    return {
        "blind_reminder_names": rt.blind_reminder_names,
        "blind_check_camera": rt.blind_check_camera,
    }


@router.patch("/scoreboard/notifications")
async def update_notifications(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    import json as _json
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    body = await request.json()
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    if "blind_reminder_names" in body:
        raw["blind_reminder_names"] = str(body["blind_reminder_names"]).strip()
    if "blind_check_camera" in body:
        raw["blind_check_camera"] = str(body["blind_check_camera"]).strip()
    out = _json.dumps(raw, indent=2, sort_keys=True) + chr(10)
    _RUNTIME_FILE.write_text(out)
    return {"ok": True}
