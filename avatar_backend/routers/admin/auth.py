"""Auth sub-router: login, logout, setup, /me, /api-key, admin page, user CRUD."""
from __future__ import annotations

import structlog
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from avatar_backend.bootstrap.container import AppContainer, get_container

from .common import (
    _COOKIE_NAME,
    _STATIC_DIR,
    _get_session,
    _require_session,
    _set_session_cookie,
    LoginBody,
    CreateUserBody,
    ChangePasswordBody,
    ChangeRoleBody,
)

_LOGGER = structlog.get_logger()
router = APIRouter()


# ── Login / logout / setup ────────────────────────────────────────────────────

@router.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(str(_STATIC_DIR / "login.html"))


@router.get("/setup-required", include_in_schema=False)
async def setup_required(request: Request, container: AppContainer = Depends(get_container)):
    return {"required": not container.user_service.has_users()}


@router.post("/setup", include_in_schema=False)
async def first_run_setup(request: Request, container: AppContainer = Depends(get_container)):
    """Create the very first admin account. Only works when no users exist."""
    from avatar_backend.middleware.ratelimit import is_rate_limited, record_failure
    client_ip = request.client.host if request.client else "unknown"
    if is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    users = container.user_service
    if users.has_users():
        raise HTTPException(status_code=409, detail="Setup already complete")
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username:
        record_failure(client_ip)
        raise HTTPException(status_code=400, detail="Username is required")
    try:
        users.create_user(username, password, "admin")
    except ValueError as exc:
        record_failure(client_ip)
        raise HTTPException(status_code=400, detail=str(exc))
    _LOGGER.info("admin.setup_complete", username=username)
    return {"created": True}


@router.post("/login")
async def do_login(body: LoginBody, request: Request, container: AppContainer = Depends(get_container)):
    from avatar_backend.middleware.ratelimit import is_rate_limited, record_failure, clear_failures
    client_ip = request.client.host if request.client else "unknown"
    if is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")

    users = container.user_service
    user  = users.authenticate(body.username, body.password)
    if not user:
        record_failure(client_ip)
        _LOGGER.warning("admin.login_failed", username=body.username, client=client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    clear_failures(client_ip)
    token = users.create_session(user["username"], user["role"])
    _LOGGER.info("admin.login_ok", username=user["username"], role=user["role"])
    resp  = JSONResponse({"ok": True, "role": user["role"]})
    _set_session_cookie(resp, token, request=request)
    return resp


@router.post("/logout")
async def do_logout(request: Request, container: AppContainer = Depends(get_container)):
    token = request.cookies.get(_COOKIE_NAME)
    if token:
        container.user_service.invalidate_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE_NAME, path="/admin")
    return resp


@router.get("/me")
async def get_me(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": sess["username"], "role": sess["role"]}


@router.get("/api-key")
async def get_api_key(request: Request):
    """Return the API key if the caller has a valid admin session cookie."""
    _require_session(request, min_role="viewer")
    from avatar_backend.config import get_settings
    return {"api_key": get_settings().api_key}


# ── Admin page ────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def admin_page(request: Request):
    if not _get_session(request):
        return RedirectResponse("/admin/login")
    return FileResponse(str(_STATIC_DIR / "admin.html"), headers={"Cache-Control": "no-cache, must-revalidate"})


# ── User management (admin only) ──────────────────────────────────────────────

@router.get("/users")
async def list_users(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    return {"users": container.user_service.list_users()}


@router.post("/users", status_code=201)
async def create_user(body: CreateUserBody, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    try:
        container.user_service.create_user(body.username, body.password, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"created": body.username}


@router.delete("/users/{username}")
async def delete_user(username: str, request: Request, container: AppContainer = Depends(get_container)):
    sess = _require_session(request, min_role="admin")
    if username == sess["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    try:
        container.user_service.delete_user(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": username}


@router.post("/users/{username}/password")
async def change_user_password(username: str, body: ChangePasswordBody, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    try:
        container.user_service.change_password(username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"updated": username}


@router.post("/users/{username}/role")
async def change_user_role(username: str, body: ChangeRoleBody, request: Request, container: AppContainer = Depends(get_container)):
    sess = _require_session(request, min_role="admin")
    if username == sess["username"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    try:
        container.user_service.change_role(username, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"updated": username, "role": body.role}
