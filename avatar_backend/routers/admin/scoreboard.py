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
    return {
        "weekly": svc.weekly_scores(),
        "recent": svc.recent_logs(10),
        "config": svc.get_config(),
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
            for key in ("label", "points", "cooldown_hours", "verification", "camera_entity_id", "requires_approval"):
                if key in body:
                    t[key] = body[key]
            updated = True
            break
    if not updated:
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
