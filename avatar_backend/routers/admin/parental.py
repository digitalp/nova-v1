"""
Parental control router — proxies Headwind MDM REST API for the Nova admin portal.
Headwind MDM runs internally at http://localhost:8083 (Docker container hmdm_server).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import time
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .common import _require_session

_LOGGER = structlog.get_logger()

router = APIRouter()

_HMDM_BASE = "http://localhost:8083"
_HMDM_PUBLIC = "https://mdm.nova-home.co.uk"
_HMDM_LOGIN = "admin"
_HMDM_RAW_PW = "linkstar"
# Headwind expects MD5(password).upper() as the API password
_HMDM_API_PW = hashlib.md5(_HMDM_RAW_PW.encode()).hexdigest().upper()

# Cached JWT token
_jwt_token: str = ""
_jwt_expires: float = 0.0
_jwt_lock = asyncio.Lock()


async def _get_jwt() -> str:
    global _jwt_token, _jwt_expires
    async with _jwt_lock:
        if _jwt_token and time.time() < _jwt_expires - 60:
            return _jwt_token
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_HMDM_BASE}/rest/public/jwt/login",
                json={"login": _HMDM_LOGIN, "password": _HMDM_API_PW},
            )
            resp.raise_for_status()
            _jwt_token = resp.json()["id_token"]
            # JWT expires in 24h; refresh after 23h
            _jwt_expires = time.time() + 23 * 3600
        return _jwt_token


async def _hmdm(method: str, path: str, **kwargs) -> Any:
    """Make an authenticated request to Headwind MDM."""
    token = await _get_jwt()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await getattr(client, method.lower())(
            f"{_HMDM_BASE}{path}", headers=headers, **kwargs
        )
        if resp.status_code == 401:
            # Token expired — force refresh and retry once
            global _jwt_expires
            _jwt_expires = 0
            token = await _get_jwt()
            headers = {"Authorization": f"Bearer {token}"}
            resp = await getattr(client, method.lower())(
                f"{_HMDM_BASE}{path}", headers=headers, **kwargs
            )
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}


# ── Devices ───────────────────────────────────────────────────────────────────

@router.get("/parental/devices")
async def list_devices(request: Request):
    _require_session(request, min_role="viewer")
    try:
        data = await _hmdm(
            "post", "/rest/private/devices/search",
            json={"pageSize": 100, "pageNum": 1, "sortValue": "lastUpdate", "sortDir": "DESC"},
        )
        items = data.get("data", {}).get("devices", {}).get("items", [])
        configs = data.get("data", {}).get("configurations", {})
        return {"devices": items, "configurations": configs}
    except Exception as exc:
        _LOGGER.warning("parental.devices_error", exc=str(exc)[:120])
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/parental/devices/{device_number}/info")
async def device_info(device_number: str, request: Request):
    _require_session(request, min_role="viewer")
    try:
        data = await _hmdm("get", f"/rest/plugins/deviceinfo/deviceinfo/private/{device_number}")
        return data.get("data", {})
    except Exception as exc:
        _LOGGER.warning("parental.device_info_error", device=device_number, exc=str(exc)[:120])
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── Configurations ────────────────────────────────────────────────────────────

@router.get("/parental/configurations")
async def list_configurations(request: Request):
    _require_session(request, min_role="viewer")
    try:
        data = await _hmdm("get", "/rest/private/configurations/search/")
        return {"configurations": data.get("data", [])}
    except Exception as exc:
        _LOGGER.warning("parental.configs_error", exc=str(exc)[:120])
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.get("/parental/configurations/{config_id}")
async def get_configuration(config_id: int, request: Request):
    _require_session(request, min_role="viewer")
    try:
        data = await _hmdm("get", f"/rest/private/configurations/{config_id}")
        return data.get("data", {})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── App blocking ──────────────────────────────────────────────────────────────

class AppBlockBody(BaseModel):
    config_id: int
    pkg: str
    # action: 0 = disabled/blocked, 1 = allowed, 2 = install required
    action: int = 0


@router.post("/parental/apps/block")
async def set_app_action(body: AppBlockBody, request: Request):
    """Set app action (0=block, 1=allow) within a configuration."""
    _require_session(request, min_role="admin")
    try:
        # Get current config
        cfg_data = await _hmdm("get", f"/rest/private/configurations/{body.config_id}")
        cfg = cfg_data.get("data", {})
        if not cfg:
            return JSONResponse({"error": "Configuration not found"}, status_code=404)

        # Find the app in configApplications and update action
        apps = cfg.get("applications", [])
        updated = False
        for app in apps:
            if app.get("pkg") == body.pkg:
                app["action"] = body.action
                updated = True
                break

        if not updated:
            # App not in config — add it (need to look up app ID first)
            apps_data = await _hmdm("get", f"/rest/private/applications/search/{body.pkg}")
            found = next((a for a in apps_data.get("data", []) if a.get("pkg") == body.pkg), None)
            if found:
                apps.append({
                    "id": found["id"],
                    "pkg": body.pkg,
                    "name": found.get("name", body.pkg),
                    "action": body.action,
                    "selected": body.action != 0,
                    "skipVersion": False,
                    "version": "0",
                })

        cfg["applications"] = apps
        await _hmdm("put", "/rest/private/configurations", json=cfg)
        # Push updated config to all devices using this config
        await _push_config_to_devices(body.config_id)
        return {"ok": True, "pkg": body.pkg, "action": body.action}
    except Exception as exc:
        _LOGGER.warning("parental.app_block_error", exc=str(exc)[:120])
        return JSONResponse({"error": str(exc)}, status_code=502)


async def _push_config_to_devices(config_id: int):
    """Push updated configuration to all devices using it."""
    try:
        dev_data = await _hmdm(
            "post", "/rest/private/devices/search",
            json={"pageSize": 100, "pageNum": 1},
        )
        items = dev_data.get("data", {}).get("devices", {}).get("items", [])
        for device in items:
            if device.get("configurationId") == config_id:
                try:
                    await _hmdm("get", f"/rest/private/push/{device['id']}")
                except Exception:
                    pass
    except Exception as exc:
        _LOGGER.warning("parental.push_error", exc=str(exc)[:120])


# ── Alerts ────────────────────────────────────────────────────────────────────

class AlertBody(BaseModel):
    device_number: str
    message: str
    title: str = "Nova Alert"


@router.post("/parental/alert")
async def send_alert(body: AlertBody, request: Request):
    _require_session(request, min_role="admin")
    try:
        await _hmdm(
            "post", "/rest/plugins/messaging/private/send",
            json={
                "deviceNumber": body.device_number,
                "messageBody": body.message,
                "messageTitle": body.title,
                "type": "plainText",
            },
        )
        return {"ok": True}
    except Exception as exc:
        _LOGGER.warning("parental.alert_error", exc=str(exc)[:120])
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── Enrollment ────────────────────────────────────────────────────────────────

@router.get("/parental/enroll/{config_id}")
async def enrollment_info(config_id: int, request: Request):
    """Return the QR code URL and enrollment details for a configuration."""
    _require_session(request, min_role="admin")
    try:
        cfg_data = await _hmdm("get", f"/rest/private/configurations/{config_id}")
        cfg = cfg_data.get("data", {})
        qr_key = cfg.get("qrCodeKey", "")
        enroll_url = f"{_HMDM_PUBLIC}/?k={qr_key}"
        # Generate QR code server-side (MDM has no /qr endpoint)
        import qrcode
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(enroll_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        return {
            "config_name": cfg.get("name", ""),
            "qr_key": qr_key,
            "enroll_url": enroll_url,
            "qr_image_url": qr_data_url,
            "hmdm_url": _HMDM_PUBLIC,
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── Proxy raw HMDM UI ─────────────────────────────────────────────────────────

@router.get("/parental/status")
async def parental_status(request: Request):
    """Quick health check — confirms Headwind MDM is reachable."""
    _require_session(request, min_role="viewer")
    try:
        await _get_jwt()
        return {"hmdm_reachable": True, "url": _HMDM_BASE}
    except Exception as exc:
        return {"hmdm_reachable": False, "error": str(exc)[:100]}


# ── APK proxy (bypasses Cloudflare bot challenge on direct MDM URL) ────────────

@router.get("/parental/apk")
async def download_apk():
    """Stream the Headwind MDM launcher APK via the Nova backend."""
    import asyncio
    from fastapi.responses import StreamingResponse
    apk_url = f"{_HMDM_BASE}/files/hmdm-6.14-os.apk"
    async def stream():
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("GET", apk_url) as r:
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    yield chunk
    return StreamingResponse(
        stream(),
        media_type="application/vnd.android.package-archive",
        headers={"Content-Disposition": "attachment; filename=hmdm.apk"},
    )
