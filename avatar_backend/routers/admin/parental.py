"""
Parental control router — proxies Headwind MDM REST API for the Nova admin portal.
Headwind MDM runs internally at http://localhost:8083 (Docker container hmdm_server).
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import subprocess
import time
from collections import defaultdict
from typing import Any

import httpx
from avatar_backend.services._shared_http import _http_client
from avatar_backend.services.mdm_client import (
    hmdm as _hmdm,
    _normalize_location_payload,
    _get_db_device_locations,
)
import structlog
from fastapi import APIRouter, Depends, Request
from avatar_backend.bootstrap.container import AppContainer, get_container
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .common import _require_session

_LOGGER = structlog.get_logger()

router = APIRouter()

from .parental_mdm_helpers import (
    _HMDM_BASE,
    _HMDM_PUBLIC,
    _HMDM_LAUNCHER_APK,
    _KNOWN_SOCIAL_APPS,
    _ENROLLMENT_CORE_ALLOW_PKGS,
    _get_devices_payload,
    _extract_device_location,
    _decorate_device_for_ui,
    _list_hmdm_apps,
    _app_is_installable,
    _resolve_app_definition,
    _get_configuration_names,
    _get_configuration_applications,
    _ensure_config_location_tracking,
    _ensure_config_enrollment_prereqs,
    _get_application_configuration_links,
    _set_application_config_action,
    _resolve_target_configs,
)

# ── Devices ───────────────────────────────────────────────────────────────────

@router.get("/parental/devices")
async def list_devices(request: Request):
    _require_session(request, min_role="viewer")
    try:
        data = await _get_devices_payload()
        db_locations = await _get_db_device_locations()
        items = [
            _decorate_device_for_ui(item, db_locations)
            for item in (data.get("data", {}).get("devices", {}).get("items", []) or [])
        ]
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
        info = data.get("data", {}) or {}
        db_locations = await _get_db_device_locations()
        location = (
            _extract_device_location(None, info)
            or db_locations.get(device_number)
            or db_locations.get(device_number.lower())
        )
        if location:
            info = {
                **info,
                "location": {**(info.get("location") or {}), **location},
                "lat": location["lat"],
                "lon": location["lon"],
            }
        return info
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


@router.get("/parental/apps")
async def list_available_apps(request: Request, query: str = "", limit: int = 250):
    _require_session(request, min_role="viewer")
    try:
        apps = await _list_hmdm_apps(query=query, limit=limit)
        return {"apps": apps}
    except Exception as exc:
        _LOGGER.warning("parental.apps_error", exc=str(exc)[:120], query=query[:80])
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── App blocking ──────────────────────────────────────────────────────────────

class AppActionBody(BaseModel):
    pkg: str
    name: str = ""
    config_id: int | None = None
    device_numbers: list[str] = []
    # action: 0 = not installed/prohibited, 1 = install/permit, 2 = delete
    action: int = 0


async def _apply_app_action(
    *,
    pkg: str,
    action: int,
    preferred_name: str = "",
    config_id: int | None = None,
    device_numbers: list[str] | None = None,
    operation: str = "set",
) -> dict[str, Any]:
    requested_numbers = [str(n or "").strip() for n in (device_numbers or []) if str(n or "").strip()]
    all_devices: list[dict[str, Any]] = []
    configs_to_devices: dict[int, list[dict[str, Any]]] = defaultdict(list)
    target_devices: list[dict[str, Any]] = []

    if requested_numbers:
        target_devices, configs_to_devices, all_devices = await _resolve_target_configs(requested_numbers)
    elif config_id:
        payload = await _get_devices_payload()
        all_devices = payload.get("data", {}).get("devices", {}).get("items", []) or []
        config_devices = [dev for dev in all_devices if int(dev.get("configurationId") or 0) == int(config_id)]
        configs_to_devices[int(config_id)] = config_devices
        target_devices = config_devices
    else:
        raise ValueError("Select at least one device or provide a configuration id")

    app_def = await _resolve_app_definition(pkg, preferred_name)
    effective_action = 1 if operation == "deploy" else action
    updated_config_ids: list[int] = []
    persisted_rows: list[dict[str, Any]] = []
    for cfg_id in sorted(configs_to_devices):
        result = await _set_application_config_action(
            cfg_id,
            pkg=pkg,
            action=effective_action,
            preferred_name=preferred_name,
        )
        updated_config_ids.append(cfg_id)
        persisted = result.get("persisted") or {}
        if persisted:
            persisted_rows.append(
                {
                    "configurationId": int(persisted.get("configurationId") or cfg_id),
                    "configurationName": str(persisted.get("configurationName") or ""),
                    "action": int(persisted.get("action") or effective_action),
                }
            )

    affected_devices = [
        {
            "number": str(dev.get("number") or ""),
            "description": str(dev.get("description") or dev.get("number") or ""),
            "configurationId": int(dev.get("configurationId") or 0),
        }
        for dev in all_devices
        if int(dev.get("configurationId") or 0) in updated_config_ids
    ]
    requested_devices = [
        {
            "number": str(dev.get("number") or ""),
            "description": str(dev.get("description") or dev.get("number") or ""),
            "configurationId": int(dev.get("configurationId") or 0),
        }
        for dev in target_devices
    ]
    result_mode = "install" if app_def.get("installable") else "allow"
    message = (
        f"{app_def.get('name') or pkg} will be installed where Headwind can install apps."
        if operation == "deploy" and app_def.get("installable")
        else (
            f"{app_def.get('name') or pkg} is a system or permit-only app in Headwind. Nova can allow it, but Headwind cannot silently install it."
            if operation == "deploy"
            else ""
        )
    )
    return {
        "ok": True,
        "pkg": pkg,
        "action": effective_action,
        "requested_action": action,
        "operation": operation,
        "result_mode": result_mode,
        "message": message,
        "application": app_def,
        "updated_config_ids": updated_config_ids,
        "persisted_rows": persisted_rows,
        "requested_devices": requested_devices,
        "affected_devices": affected_devices,
    }


@router.post("/parental/apps/block")
async def set_app_action(body: AppActionBody, request: Request):
    """Set app action (0=block, 1=allow) within a configuration."""
    _require_session(request, min_role="admin")
    try:
        return await _apply_app_action(
            pkg=body.pkg,
            action=body.action,
            preferred_name=body.name,
            config_id=body.config_id,
            device_numbers=body.device_numbers,
            operation="set",
        )
    except Exception as exc:
        _LOGGER.warning("parental.app_block_error", exc=str(exc)[:120])
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.post("/parental/apps/deploy")
async def deploy_app(body: AppActionBody, request: Request):
    """Deploy/install an app to one or more devices by marking it install-required."""
    _require_session(request, min_role="admin")
    try:
        return await _apply_app_action(
            pkg=body.pkg,
            action=1,
            preferred_name=body.name,
            config_id=body.config_id,
            device_numbers=body.device_numbers,
            operation="deploy",
        )
    except Exception as exc:
        _LOGGER.warning("parental.app_deploy_error", exc=str(exc)[:120])
        return JSONResponse({"error": str(exc)}, status_code=502)


async def _push_config_to_devices(config_id: int):
    pass  # MDM pushes via MQTT automatically on config save


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
        tracking = await _ensure_config_enrollment_prereqs(config_id)
        cfg_data = await _hmdm("get", f"/rest/private/configurations/{config_id}")
        cfg = cfg_data.get("data", {})
        qr_key = cfg.get("qrCodeKey", "")
        enroll_url = f"{_HMDM_PUBLIC}/?k={qr_key}"
        # Fetch the QR JSON content the launcher app actually expects (public endpoint)
        _r = await _http_client().get(f"{_HMDM_BASE}/rest/public/qr/json/{qr_key}", timeout=10.0)
        qr_content = _r.text  # already JSON string
        # Generate QR code from JSON content
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
            "location_tracking": tracking,
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── Proxy raw HMDM UI ─────────────────────────────────────────────────────────

@router.get("/parental/status")
async def parental_status(request: Request):
    """Quick health check — confirms Headwind MDM is reachable."""
    _require_session(request, min_role="viewer")
    try:
        await _hmdm("get", "/rest/private/configurations/search/")
        return {"hmdm_reachable": True, "url": _HMDM_BASE}
    except Exception as exc:
        return {"hmdm_reachable": False, "error": str(exc)[:100]}


# ── APK proxy (bypasses Cloudflare bot challenge on direct MDM URL) ────────────

@router.get("/parental/provisioning-qr")
async def provisioning_qr(config_id: int = 2):
    """
    Return an Android Device Owner provisioning QR.
    Scanned at the Android setup wizard (6-tap method) to install MDM as Device Owner.
    Different from the basic enrollment QR — this tells Android to download and
    install the MDM app itself before the OS is fully set up.
    """
    import hashlib, json
    tracking = await _ensure_config_enrollment_prereqs(config_id)
    # Fetch the config QR key
    cfg_data = await _hmdm("get", f"/rest/private/configurations/{config_id}")
    cfg = cfg_data.get("data", {})
    qr_key = cfg.get("qrCodeKey", "")

    # Signing certificate SHA-256 (keytool -printcert -jarfile) as base64url, no padding
    cert_hex = "095761E0055FE057672406397F352257CD34D71F279E8BD4F4FD3D8F91099757"
    checksum = base64.urlsafe_b64encode(bytes.fromhex(cert_hex)).rstrip(b"=").decode()
    package_checksum = "7QS-tY3z_oP2LPgn5XCbbHXj8l-lu0LP2Yc2yeqbuDE"

    provisioning = {
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME":
            "com.hmdm.launcher/com.hmdm.launcher.AdminReceiver",
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION":
            f"{_HMDM_PUBLIC}/files/{_HMDM_LAUNCHER_APK}",
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_CHECKSUM": package_checksum,
        "android.app.extra.PROVISIONING_DEVICE_ADMIN_SIGNATURE_CHECKSUM": checksum,
        # Keep core Google/system packages enabled during fully managed setup.
        # Without this, some devices can hang on "Setting up supervision"
        # before Headwind has a chance to apply its post-enrollment config.
        "android.app.extra.PROVISIONING_LEAVE_ALL_SYSTEM_APPS_ENABLED": True,
        "android.app.extra.PROVISIONING_SKIP_ENCRYPTION": True,
        "android.app.extra.PROVISIONING_ADMIN_EXTRAS_BUNDLE": {
            "com.hmdm.BASE_URL": _HMDM_PUBLIC,
            "com.hmdm.SERVER_PROJECT": "",
            "com.hmdm.QR_CODE_KEY": qr_key,
        },
    }
    content = json.dumps(provisioning, separators=(",", ":"))

    import qrcode as _qrcode
    qr = _qrcode.QRCode(
        box_size=6, border=2,
        error_correction=_qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return {
        "qr_image_url": qr_data_url,
        "config_name": cfg.get("name", ""),
        "qr_key": qr_key,
        "location_tracking": tracking,
    }


@router.get("/parental/apk")
async def download_apk():
    """Stream the Headwind MDM launcher APK via the Nova backend."""
    import asyncio
    from fastapi.responses import StreamingResponse
    apk_url = f"{_HMDM_BASE}/files/{_HMDM_LAUNCHER_APK}"
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


@router.get("/parental/apk-qr")
async def apk_qr():
    """Return a QR code PNG pointing to the APK download proxy."""
    import qrcode
    url = f"{_HMDM_PUBLIC}/files/{_HMDM_LAUNCHER_APK}"
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return {"qr_image_url": qr_b64, "download_url": url}
