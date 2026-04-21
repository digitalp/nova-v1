"""Headwind MDM — shared auth + high-level helpers used by both parental router and ha_proxy."""
from __future__ import annotations
import asyncio
import hashlib
import time
from typing import Any

import httpx

_HMDM_BASE = "http://localhost:8083"
_HMDM_LOGIN = "admin"
_HMDM_RAW_PW = "linkstar"
_HMDM_API_PW = hashlib.md5(_HMDM_RAW_PW.encode()).hexdigest().upper()

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
            _jwt_expires = time.time() + 23 * 3600
    return _jwt_token


async def hmdm(method: str, path: str, **kwargs: Any) -> Any:
    """Make an authenticated request to the Headwind MDM API."""
    global _jwt_expires
    token = await _get_jwt()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await getattr(client, method.lower())(
            f"{_HMDM_BASE}{path}", headers=headers, **kwargs
        )
        if resp.status_code in (401, 403):
            _jwt_expires = 0.0
            token = await _get_jwt()
            headers = {"Authorization": f"Bearer {token}"}
            resp = await getattr(client, method.lower())(
                f"{_HMDM_BASE}{path}", headers=headers, **kwargs
            )
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}


async def get_devices() -> list[dict]:
    data = await hmdm(
        "post",
        "/rest/private/devices/search",
        json={"pageSize": 100, "pageNum": 1, "sortValue": "lastUpdate", "sortDir": "DESC"},
    )
    return data.get("data", {}).get("devices", {}).get("items", [])


async def set_app_action(device_number: str, pkg: str, action: int) -> dict:
    """Block (action=0) or unblock (action=1) an app on the device's config."""
    devices = await get_devices()
    device = next((d for d in devices if d.get("number") == device_number), None)
    if not device:
        raise ValueError(f"Device {device_number!r} not found")
    config_id = device.get("configurationId")
    if not config_id:
        raise ValueError(f"Device {device_number!r} has no configuration assigned")

    cfg_data = await hmdm("get", f"/rest/private/configurations/{config_id}")
    cfg = cfg_data.get("data", {})
    apps: list[dict] = cfg.get("applications", [])

    found = False
    for app in apps:
        if app.get("pkg") == pkg:
            app["action"] = action
            app["selected"] = action != 0
            found = True
            break

    if not found:
        apps.append({
            "pkg": pkg,
            "name": pkg,
            "action": action,
            "selected": action != 0,
            "skipVersion": False,
            "version": "0",
        })

    cfg["applications"] = apps
    await hmdm("put", "/rest/private/configurations", json=cfg)

    # Push updated config to all devices on this configuration
    for d in devices:
        if d.get("configurationId") == config_id:
            try:
                await hmdm("get", f"/rest/private/push/{d['id']}")
            except Exception:
                pass

    return {"ok": True, "pkg": pkg, "action": action, "device_number": device_number}


async def send_message(device_number: str, message: str, title: str = "Nova") -> dict:
    """Send a push notification to a device."""
    await hmdm(
        "post",
        "/rest/plugins/messaging/private/send",
        json={
            "deviceNumber": device_number,
            "messageBody": message,
            "messageTitle": title,
            "type": "plainText",
        },
    )
    return {"ok": True, "device_number": device_number}
