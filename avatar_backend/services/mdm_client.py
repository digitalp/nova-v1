"""Headwind MDM — shared auth + high-level helpers used by both parental router and ha_proxy."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import os
import json
import subprocess
import time
from typing import Any

import httpx
from avatar_backend.services._shared_http import _http_client
import structlog

_LOGGER = structlog.get_logger(__name__)

_HMDM_BASE = os.getenv("HEADWIND_URL", "http://localhost:8083")
_HMDM_PUBLIC = os.getenv("HEADWIND_PUBLIC_URL", "")
_HMDM_LOGIN = os.getenv("HEADWIND_LOGIN", "admin")
_HMDM_RAW_PW = os.getenv("HEADWIND_PASSWORD", "")
_HMDM_API_PW = hashlib.md5(_HMDM_RAW_PW.encode()).hexdigest().upper() if _HMDM_RAW_PW else ""
_KNOWN_SOCIAL_APPS = [
    {"name": "Instagram", "pkg": "com.instagram.android"},
    {"name": "TikTok", "pkg": "com.zhiliaoapp.musically"},
    {"name": "WhatsApp", "pkg": "com.whatsapp"},
    {"name": "Snapchat", "pkg": "com.snapchat.android"},
    {"name": "X / Twitter", "pkg": "com.twitter.android"},
    {"name": "Facebook", "pkg": "com.facebook.katana"},
    {"name": "YouTube", "pkg": "com.google.android.youtube"},
]

_jwt_token: str = ""
_jwt_expires: float = 0.0
_jwt_lock = asyncio.Lock()

_db_location_cache: dict[str, dict[str, Any]] = {}
_db_location_cache_expires: float = 0.0
_db_location_cache_lock = asyncio.Lock()

async def _get_jwt() -> str:
    global _jwt_token, _jwt_expires
    async with _jwt_lock:
        if _jwt_token and time.time() < _jwt_expires - 60:
            return _jwt_token
        resp = await _http_client().post(
            f"{_HMDM_BASE}/rest/public/jwt/login",
            json={"login": _HMDM_LOGIN, "password": _HMDM_API_PW},
            timeout=10.0,
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
    _client = _http_client()
    resp = await getattr(_client, method.lower())(
        f"{_HMDM_BASE}{path}", headers=headers, timeout=15.0, **kwargs
    )
    if resp.status_code in (401, 403):
        _jwt_expires = 0.0
        token = await _get_jwt()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await getattr(_client, method.lower())(
            f"{_HMDM_BASE}{path}", headers=headers, timeout=15.0, **kwargs
        )
    resp.raise_for_status()
    if resp.content:
        return resp.json()
    return {}

def _normalize_location_payload(raw: dict[str, Any] | None, fallback_ts: Any = None) -> dict[str, Any] | None:
    raw = raw or {}
    try:
        lat = float(raw.get("lat"))
        lon = float(raw.get("lon"))
    except (TypeError, ValueError):
        return None
    return {
        "lat": lat,
        "lon": lon,
        "ts": raw.get("ts") or fallback_ts,
    }

async def _get_db_device_locations(force: bool = False) -> dict[str, dict[str, Any]]:
    global _db_location_cache, _db_location_cache_expires
    async with _db_location_cache_lock:
        now = time.time()
        if not force and _db_location_cache and now < _db_location_cache_expires:
            return dict(_db_location_cache)

        sql = "select number, replace(encode(convert_to(coalesce(infojson::text,'{}'),'UTF8'),'base64'), E'\\n', '') from devices;"
        cmd = ["docker", "exec", "hmdm_db", "psql", "-U", "hmdm", "-d", "hmdm", "-At", "-F", "\t", "-c", sql]
        locations: dict[str, dict[str, Any]] = {}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
            for line in proc.stdout.splitlines():
                if not line.strip(): continue
                number, _, payload_b64 = line.partition("\t")
                if not number or not payload_b64: continue
                try:
                    infojson = base64.b64decode(payload_b64).decode("utf-8")
                    payload = json.loads(infojson)
                except Exception: continue
                location = _normalize_location_payload(payload.get("location"), payload.get("lastUpdate"))
                if not location: continue
                locations[number] = location
                locations[number.lower()] = location
        except Exception as exc:
            _LOGGER.warning("mdm.db_location_error", exc=str(exc))
        _db_location_cache = locations
        _db_location_cache_expires = now + 15
        return dict(_db_location_cache)

async def get_devices() -> list[dict]:
    data = await hmdm(
        "post",
        "/rest/private/devices/search",
        json={"pageSize": 100, "pageNum": 1, "sortValue": "lastUpdate", "sortDir": "DESC"},
    )
    return data.get("data", {}).get("devices", {}).get("items", [])


async def get_parental_status() -> dict[str, Any]:
    await _get_jwt()
    return {"hmdm_reachable": True, "url": _HMDM_BASE}


async def get_device(device_number: str) -> dict[str, Any]:
    devices = await get_devices()
    device = next((d for d in devices if str(d.get("number") or "").lower() == str(device_number).lower()), None)
    if not device:
        raise ValueError(f"Device {device_number!r} not found")
    return device


async def get_device_info(device_number: str) -> dict[str, Any]:
    data = await hmdm("get", f"/rest/plugins/deviceinfo/deviceinfo/private/{device_number}")
    return data.get("data", {}) or data


def _extract_location(device: dict[str, Any] | None, info: dict[str, Any] | None = None) -> dict[str, Any] | None:
    device = device or {}
    info = info or {}
    dev_info = device.get("info")
    if isinstance(dev_info, str):
        try:
            dev_info = json.loads(dev_info)
        except Exception:
            dev_info = None
    if not isinstance(dev_info, dict):
        dev_info = {}
    explicit = device.get("location") or {}
    latest_dynamic = info.get("latestDynamicData") or dev_info.get("latestDynamicData") or {}
    embedded = info.get("location") or explicit or dev_info.get("location") or {}
    raw_lat = (
        info.get("lat")
        or info.get("latitude")
        or device.get("lat")
        or device.get("latitude")
        or embedded.get("lat")
        or latest_dynamic.get("gpsLat")
    )
    raw_lon = (
        info.get("lon")
        or info.get("longitude")
        or info.get("lng")
        or device.get("lon")
        or device.get("longitude")
        or device.get("lng")
        or embedded.get("lon")
        or latest_dynamic.get("gpsLon")
    )
    try:
        lat = float(raw_lat)
        lon = float(raw_lon)
    except (TypeError, ValueError):
        return None
    return {
        "lat": lat,
        "lon": lon,
        "ts": embedded.get("ts") or info.get("latestUpdateTime") or device.get("lastUpdate"),
    }


async def get_device_location(device_number: str) -> dict[str, Any] | None:
    device = await get_device(device_number)
    try:
        info = await get_device_info(device_number)
    except Exception:
        info = {}
    
    api_loc = _extract_location(device, info)
    if api_loc:
        return api_loc
        
    db_locations = await _get_db_device_locations()
    return db_locations.get(device_number) or db_locations.get(device_number.lower())


async def get_configurations() -> list[dict[str, Any]]:
    data = await hmdm("get", "/rest/private/configurations/search/")
    return data.get("data", []) or []


def _app_is_installable(app: dict[str, Any]) -> bool:
    return (
        not bool(app.get("system"))
        and str(app.get("type") or "") == "app"
        and any(app.get(field) for field in ("url", "urlArmeabi", "urlArm64"))
    )


async def search_apps(query: str = "", limit: int = 25) -> list[dict[str, Any]]:
    query = (query or "").strip()
    candidates: list[dict[str, Any]] = []
    paths = [f"/rest/private/applications/search/{query}"]
    if not query:
        paths.append("/rest/private/applications/search/")
    last_exc: Exception | None = None
    for path in paths:
        try:
            data = await hmdm("get", path)
            raw_items = data.get("data", [])
            if isinstance(raw_items, dict):
                raw_items = raw_items.get("items", []) or raw_items.get("applications", []) or []
            for item in raw_items or []:
                pkg = str(item.get("pkg") or "").strip()
                if not pkg:
                    continue
                candidates.append({
                    "id": item.get("id"),
                    "name": str(item.get("name") or pkg).strip(),
                    "pkg": pkg,
                    "version": str(item.get("version") or "").strip(),
                    "system": bool(item.get("system")),
                    "type": str(item.get("type") or "").strip(),
                    "url": item.get("url"),
                    "urlArmeabi": item.get("urlArmeabi"),
                    "urlArm64": item.get("urlArm64"),
                })
            if candidates:
                break
        except Exception as exc:
            last_exc = exc
    merged: dict[str, dict[str, Any]] = {app["pkg"]: dict(app) for app in _KNOWN_SOCIAL_APPS}
    for item in candidates:
        merged[item["pkg"]] = item
    items = list(merged.values())
    if query:
        q = query.lower()
        items = [item for item in items if q in item["pkg"].lower() or q in item["name"].lower()]
    items.sort(key=lambda item: (item["name"].lower(), item["pkg"].lower()))
    if not items and last_exc is not None:
        raise last_exc
    trimmed = items[: max(1, min(limit, 100))]
    for item in trimmed:
        item["installable"] = _app_is_installable(item)
        item["assignment_mode"] = "install" if item["installable"] else "allow"
    return trimmed


async def _resolve_app_definition(pkg: str, preferred_name: str = "") -> dict[str, Any]:
    apps = await search_apps(pkg, limit=50)
    found = next((app for app in apps if app.get("pkg") == pkg), None)
    return {
        "id": found.get("id") if found else None,
        "pkg": pkg,
        "name": (found or {}).get("name") or preferred_name or pkg,
        "version": (found or {}).get("version") or "0",
        "system": bool((found or {}).get("system")),
        "type": str((found or {}).get("type") or "").strip(),
        "url": (found or {}).get("url"),
        "urlArmeabi": (found or {}).get("urlArmeabi"),
        "urlArm64": (found or {}).get("urlArm64"),
        "installable": _app_is_installable(found or {}),
    }


async def _get_configuration_names() -> dict[int, str]:
    items = await get_configurations()
    return {int(item.get("id") or 0): str(item.get("name") or "") for item in items if int(item.get("id") or 0)}


async def _get_application_configuration_links(application_id: int) -> list[dict[str, Any]]:
    data = await hmdm("get", f"/rest/private/applications/configurations/{application_id}")
    return data.get("data", []) or []


async def _set_application_config_action(
    config_id: int,
    pkg: str,
    action: int,
    preferred_name: str = "",
) -> dict[str, Any]:
    app_def = await _resolve_app_definition(pkg, preferred_name)
    app_id = int(app_def.get("id") or 0)
    if not app_id:
        raise ValueError(
            f"{pkg} is not in Headwind's application catalog yet. Add it to Headwind first, then deploy it from Nova."
        )
    existing_links = await _get_application_configuration_links(app_id)
    config_names = await _get_configuration_names()
    cfg_name = config_names.get(int(config_id))
    if not cfg_name:
        raise ValueError(f"Configuration {config_id} not found")

    updated = False
    links: list[dict[str, Any]] = []
    for link in existing_links:
        row = dict(link)
        if int(row.get("configurationId") or 0) == int(config_id):
            row["action"] = action
            row["remove"] = action == 2
            row["notify"] = True
            row["applicationId"] = app_id
            row["applicationName"] = app_def.get("name") or preferred_name or pkg
            row["configurationName"] = cfg_name
            row["configurationDescription"] = row.get("configurationDescription") or ""
            updated = True
        links.append(row)

    if not updated:
        links.append(
            {
                "id": None,
                "applicationId": app_id,
                "applicationName": app_def.get("name") or preferred_name or pkg,
                "configurationId": int(config_id),
                "configurationName": cfg_name,
                "configurationDescription": "",
                "action": action,
                "remove": action == 2,
                "notify": True,
            }
        )

    await hmdm("post", "/rest/private/applications/configurations", json={"applicationId": app_id, "configurations": links})
    return {
        "ok": True,
        "persisted": {
            "configurationId": int(config_id),
            "configurationName": cfg_name,
            "action": action,
        },
        "application": app_def,
    }


async def set_app_action(device_number: str, pkg: str, action: int) -> dict:
    """Block (action=0) or unblock (action=1) an app on the device's config."""
    device = await get_device(device_number)
    config_id = device.get("configurationId")
    if not config_id:
        raise ValueError(f"Device {device_number!r} has no configuration assigned")
    await _set_application_config_action(int(config_id), pkg=pkg, action=action)
    return {"ok": True, "pkg": pkg, "action": action, "device_number": device_number}


async def deploy_app(device_number: str, pkg: str, preferred_name: str = "") -> dict[str, Any]:
    device = await get_device(device_number)
    config_id = int(device.get("configurationId") or 0)
    if not config_id:
        raise ValueError(f"Device {device_number!r} has no configuration assigned")
    result = await _set_application_config_action(config_id, pkg=pkg, action=1, preferred_name=preferred_name)
    app_def = result.get("application") or {}
    mode = "install" if app_def.get("installable") else "allow"
    return {
        "ok": True,
        "device_number": device_number,
        "pkg": pkg,
        "result_mode": mode,
        "application": app_def,
    }


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


async def get_enrollment_info(config_id: int) -> dict[str, Any]:
    cfg_data = await hmdm("get", f"/rest/private/configurations/{config_id}")
    cfg = cfg_data.get("data", {})
    if not cfg:
        raise ValueError(f"Configuration {config_id} not found")
    qr_key = cfg.get("qrCodeKey", "")
    if not qr_key:
        raise ValueError(f"Configuration {config_id} has no QR key")
    return {
        "config_id": int(cfg.get("id") or config_id),
        "config_name": str(cfg.get("name") or ""),
        "qr_key": qr_key,
        "enroll_url": f"{_HMDM_PUBLIC}/?k={qr_key}",
    }
