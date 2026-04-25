"""
MDM helper functions and constants for the parental control router.
All module-level constants and pure/async helper functions live here.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from collections import defaultdict
from typing import Any

import httpx
import structlog

from avatar_backend.services._shared_http import _http_client
from avatar_backend.services.mdm_client import (
    hmdm as _hmdm,
    _normalize_location_payload,
    _get_db_device_locations,
)

_LOGGER = structlog.get_logger()

_HMDM_BASE = "http://localhost:8083"
_HMDM_PUBLIC = "https://mdm.nova-home.co.uk"
_HMDM_LAUNCHER_APK = "hmdm-6.34-os.apk"
_KNOWN_SOCIAL_APPS = [
    {"name": "Instagram", "pkg": "com.instagram.android"},
    {"name": "TikTok", "pkg": "com.zhiliaoapp.musically"},
    {"name": "WhatsApp", "pkg": "com.whatsapp"},
    {"name": "Snapchat", "pkg": "com.snapchat.android"},
    {"name": "X / Twitter", "pkg": "com.twitter.android"},
    {"name": "Facebook", "pkg": "com.facebook.katana"},
    {"name": "YouTube", "pkg": "com.google.android.youtube"},
]
_ENROLLMENT_CORE_ALLOW_PKGS = [
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.android.vending",
    "com.google.android.packageinstaller",
    "com.android.packageinstaller",
    "com.google.android.permissioncontroller",
    "com.google.android.gms.setup",
]


async def _get_devices_payload() -> dict[str, Any]:
    return await _hmdm(
        "post", "/rest/private/devices/search",
        json={"pageSize": 100, "pageNum": 1, "sortValue": "lastUpdate", "sortDir": "DESC"},
    )


def _extract_device_location(device: dict[str, Any] | None, info: dict[str, Any] | None = None) -> dict[str, Any] | None:
    dev_info = (device or {}).get("info")
    if isinstance(dev_info, str):
        try:
            dev_info = json.loads(dev_info)
        except Exception:
            dev_info = None
    if not isinstance(dev_info, dict):
        dev_info = {}

    info = info or {}
    explicit_device_location = (device or {}).get("location") or {}
    latest_dynamic = info.get("latestDynamicData") or dev_info.get("latestDynamicData") or {}
    embedded = info.get("location") or explicit_device_location or dev_info.get("location") or {}
    raw_lat = (
        info.get("lat")
        or info.get("latitude")
        or (device or {}).get("lat")
        or (device or {}).get("latitude")
        or embedded.get("lat")
        or latest_dynamic.get("gpsLat")
    )
    raw_lon = (
        info.get("lon")
        or info.get("longitude")
        or info.get("lng")
        or (device or {}).get("lon")
        or (device or {}).get("longitude")
        or (device or {}).get("lng")
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
        "ts": embedded.get("ts") or info.get("latestUpdateTime") or (device or {}).get("lastUpdate"),
    }


def _decorate_device_for_ui(device: dict[str, Any], db_locations: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    enriched = dict(device)
    number = str(enriched.get("number") or "")
    location = (
        _extract_device_location(enriched)
        or (db_locations or {}).get(number)
        or (db_locations or {}).get(number.lower())
    )
    if location:
        enriched["location"] = location
        enriched["lat"] = location["lat"]
        enriched["lon"] = location["lon"]
    return enriched


async def _list_hmdm_apps(query: str = "", limit: int = 250) -> list[dict[str, Any]]:
    query = (query or "").strip()
    candidates: list[dict[str, Any]] = []
    paths = [f"/rest/private/applications/search/{query}"]
    if not query:
        paths.append("/rest/private/applications/search/")

    last_exc: Exception | None = None
    for path in paths:
        try:
            data = await _hmdm("get", path)
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
    trimmed = items[: max(1, min(limit, 500))]
    for item in trimmed:
        item["installable"] = _app_is_installable(item)
        item["assignment_mode"] = "install" if item["installable"] else "allow"
    return trimmed


def _app_is_installable(app: dict[str, Any]) -> bool:
    return (
        not bool(app.get("system"))
        and str(app.get("type") or "") == "app"
        and any(app.get(field) for field in ("url", "urlArmeabi", "urlArm64"))
    )


async def _resolve_app_definition(pkg: str, preferred_name: str = "") -> dict[str, Any]:
    apps = await _list_hmdm_apps(pkg, limit=50)
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
    data = await _hmdm("get", "/rest/private/configurations/search/")
    items = data.get("data", []) or []
    return {int(item.get("id") or 0): str(item.get("name") or "") for item in items if int(item.get("id") or 0)}


async def _get_configuration_applications(config_id: int) -> list[dict[str, Any]]:
    data = await _hmdm("get", f"/rest/private/configurations/applications/{config_id}")
    return data.get("data", []) or []


async def _ensure_config_location_tracking(config_id: int) -> dict[str, Any]:
    cfg_data = await _hmdm("get", f"/rest/private/configurations/{config_id}")
    cfg = cfg_data.get("data", {})
    if not cfg:
        raise ValueError(f"Configuration {config_id} not found")

    changed = False
    if cfg.get("requestUpdates") != "GPS":
        cfg["requestUpdates"] = "GPS"
        changed = True
    if cfg.get("gps") is not True:
        cfg["gps"] = True
        changed = True
    if cfg.get("disableLocation") is not False:
        cfg["disableLocation"] = False
        changed = True
    if cfg.get("appPermissions") == "DENYLOCATION":
        cfg["appPermissions"] = "GRANTALL"
        changed = True

    if changed:
        # Headwind clears app assignments if configurations are PUT back without
        # the effective applications list attached. Preserve those assignments.
        cfg["applications"] = await _get_configuration_applications(config_id)
        await _hmdm("put", "/rest/private/configurations", json=cfg)
        cfg_data = await _hmdm("get", f"/rest/private/configurations/{config_id}")
        cfg = cfg_data.get("data", {}) or cfg

    return {
        "id": int(cfg.get("id") or config_id),
        "name": str(cfg.get("name") or ""),
        "requestUpdates": cfg.get("requestUpdates"),
        "gps": cfg.get("gps"),
        "disableLocation": cfg.get("disableLocation"),
        "appPermissions": cfg.get("appPermissions"),
        "changed": changed,
    }


async def _ensure_config_enrollment_prereqs(config_id: int) -> dict[str, Any]:
    tracking = await _ensure_config_location_tracking(config_id)
    changed_packages: list[str] = []

    current_apps = await _get_configuration_applications(config_id)
    actions_by_pkg = {
        str(item.get("pkg") or "").strip(): int(item.get("action") or 0)
        for item in current_apps
        if str(item.get("pkg") or "").strip()
    }
    for pkg in _ENROLLMENT_CORE_ALLOW_PKGS:
        if actions_by_pkg.get(pkg) == 1:
            continue
        await _set_application_config_action(config_id, pkg, 1)
        changed_packages.append(pkg)

    return {
        **tracking,
        "enrollment_core_packages": list(_ENROLLMENT_CORE_ALLOW_PKGS),
        "enrollment_packages_changed": changed_packages,
    }


async def _get_application_configuration_links(application_id: int) -> list[dict[str, Any]]:
    data = await _hmdm("get", f"/rest/private/applications/configurations/{application_id}")
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
            updated = True
        links.append(row)

    if not updated:
        links.append(
            {
                "id": None,
                "customerId": 1,
                "configurationId": int(config_id),
                "configurationName": cfg_name,
                "applicationId": app_id,
                "applicationName": app_def.get("name") or preferred_name or pkg,
                "action": action,
                "showIcon": True,
                "remove": action == 2,
                "outdated": False,
                "latestVersionText": app_def.get("version") or "0",
                "currentVersionText": None,
                "notify": True,
                "common": False,
            }
        )

    await _hmdm(
        "post",
        "/rest/private/applications/configurations",
        json={"applicationId": app_id, "configurations": links},
    )

    persisted_links = await _get_application_configuration_links(app_id)
    persisted = next(
        (row for row in persisted_links if int(row.get("configurationId") or 0) == int(config_id)),
        None,
    )
    return {
        "application": app_def,
        "configuration": {"id": int(config_id), "name": cfg_name},
        "persisted": persisted or {},
    }


async def _resolve_target_configs(device_numbers: list[str]) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    payload = await _get_devices_payload()
    all_devices = payload.get("data", {}).get("devices", {}).get("items", []) or []
    devices_by_number = {str(dev.get("number") or ""): dev for dev in all_devices}
    requested_devices: list[dict[str, Any]] = []
    configs_to_devices: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for number in dict.fromkeys([str(n or "").strip() for n in device_numbers if str(n or "").strip()]):
        dev = devices_by_number.get(number)
        if dev is None:
            raise ValueError(f"Device {number!r} not found")
        config_id = int(dev.get("configurationId") or 0)
        if not config_id:
            raise ValueError(f"Device {number!r} has no configuration assigned")
        requested_devices.append(dev)
        configs_to_devices[config_id].append(dev)
    return requested_devices, configs_to_devices, all_devices
