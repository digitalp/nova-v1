from __future__ import annotations

import asyncio
import json
import re
import ssl
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest


_DISCOVERY_DOMAINS = {
    "binary_sensor",
    "camera",
    "climate",
    "cover",
    "device_tracker",
    "fan",
    "humidifier",
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "light",
    "lock",
    "media_player",
    "number",
    "person",
    "sensor",
    "switch",
    "water_heater",
    "weather",
}

_SKIP_PREFIXES = (
    "automation.",
    "button.",
    "counter.",
    "event.",
    "image.",
    "scene.",
    "script.",
    "sun.",
    "system_log.",
    "timer.",
    "update.",
    "zone.",
)

_SKIP_NAME_FRAGMENTS = (
    "firmware",
    "linkquality",
    "lqi",
    "ping",
    "reboot",
    "restart",
    "rssi",
    "signal",
    "strength",
    "uptime",
    "version",
)

_SENSOR_DEVICE_CLASSES = {
    "aqi",
    "battery",
    "carbon_dioxide",
    "carbon_monoxide",
    "current",
    "door",
    "energy",
    "gas",
    "humidity",
    "illuminance",
    "moisture",
    "occupancy",
    "opening",
    "pm1",
    "pm10",
    "pm25",
    "power",
    "precipitation",
    "pressure",
    "signal_strength",
    "smoke",
    "temperature",
    "voltage",
    "water",
    "weight",
    "wind_speed",
}

_PRESENCE_BINARY_CLASSES = {"connectivity", "motion", "occupancy", "opening", "presence"}
_ACCESS_BINARY_CLASSES = {"door", "garage_door", "lock", "opening", "window"}
_SAFETY_BINARY_CLASSES = {"battery", "gas", "moisture", "plug", "problem", "safety", "smoke", "tamper"}
_VEHICLE_KEYWORDS = ("car", "ev", "fuel", "ignition", "mileage", "odometer", "tire", "tyre", "vehicle")


@dataclass(frozen=True)
class HouseholdMember:
    name: str
    role: str
    details: str = ""


@dataclass(frozen=True)
class VehicleProfile:
    owner: str
    description: str

from avatar_backend.services.prompt_helpers import (
    _replace_home_profile_section,
    _strip_template_comments,
    _remove_placeholder_lines,
    _render_home_profile,
    _pick_weather_entity,
    _pick_camera,
    _infer_motion_camera_map,
    _build_camera_vision_prompts,
    _infer_excluded_entities,
    _infer_sensor_threshold_rules,
    _infer_sensor_exclusions,
    _should_include_entity,
    _classify_group,
)

def fetch_ha_states(ha_url: str, ha_token: str, timeout_s: float = 20.0) -> list[dict]:
    base_url = ha_url.rstrip("/")
    req = urlrequest.Request(
        f"{base_url}/api/states",
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout_s) as response:
            payload = response.read().decode("utf-8")
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Home Assistant returned HTTP {exc.code}: {body or exc.reason}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"Could not reach Home Assistant: {exc.reason}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Home Assistant returned invalid JSON for /api/states") from exc
    if not isinstance(data, list):
        raise RuntimeError("Home Assistant /api/states response was not a list")
    return [item for item in data if isinstance(item, dict) and "entity_id" in item]


def parse_primary_users(raw_value: str, default_name: str) -> list[HouseholdMember]:
    names = [part.strip() for part in raw_value.split(",") if part.strip()]
    if not names:
        names = [default_name]
    return [HouseholdMember(name=name, role="primary user") for name in names]


def parse_other_members(raw_value: str) -> list[HouseholdMember]:
    members: list[HouseholdMember] = []
    for chunk in raw_value.split(";"):
        item = chunk.strip()
        if not item:
            continue
        name, sep, remainder = item.partition(":")
        role_text = remainder.strip() if sep else "household member"
        role, _, details = role_text.partition(",")
        members.append(
            HouseholdMember(
                name=name.strip(),
                role=role.strip() or "household member",
                details=details.strip(),
            )
        )
    return members


def parse_vehicle_profiles(raw_value: str) -> list[VehicleProfile]:
    vehicles: list[VehicleProfile] = []
    for chunk in raw_value.split(";"):
        item = chunk.strip()
        if not item:
            continue
        owner, sep, description = item.partition(":")
        if sep:
            vehicles.append(VehicleProfile(owner=owner.strip() or "Household", description=description.strip()))
        else:
            vehicles.append(VehicleProfile(owner="Household", description=item))
    return vehicles


def parse_notes(raw_value: str) -> list[str]:
    return [chunk.strip() for chunk in raw_value.split(";") if chunk.strip()]


def generate_prompt(
    template_text: str,
    address: str,
    timezone_name: str,
    household: list[HouseholdMember],
    vehicles: list[VehicleProfile],
    extra_notes: list[str],
    states: list[dict] | None,
    source_label: str,
    area_by_entity: dict[str, str] | None = None,
) -> str:
    prompt = template_text
    prompt = prompt.replace("<YOUR_ADDRESS>", address)

    generated_section = _render_home_profile(
        address=address,
        timezone_name=timezone_name,
        household=household,
        vehicles=vehicles,
        extra_notes=extra_notes,
        states=states or [],
        source_label=source_label,
        area_by_entity=area_by_entity or {},
    )
    prompt = _replace_home_profile_section(prompt, generated_section)
    prompt = _strip_template_comments(prompt)
    prompt = _remove_placeholder_lines(prompt)
    prompt = re.sub(r"\n{3,}", "\n\n", prompt).strip() + "\n"
    return prompt


def build_home_runtime_config(
    states: list[dict],
    vehicles: list[VehicleProfile],
    extra_notes: list[str],
) -> dict:
    default_doorbell_camera = _pick_camera(states, ("doorbell", "front door"))
    outdoor_camera = _pick_camera(states, ("driveway", "outdoor", "outside", "floodlight"))
    living_room_camera = _pick_camera(states, ("living room", "sitting room", "lounge"))
    weather_entity = _pick_weather_entity(states)

    camera_aliases: dict[str, str] = {}
    if default_doorbell_camera:
        for alias in (
            "camera.doorbell",
            "camera.front_door",
            "camera.front_door_camera",
            "camera.reolink_doorbell",
            "camera.doorbell_camera",
        ):
            camera_aliases[alias] = default_doorbell_camera
    if outdoor_camera:
        for alias in ("camera.outdoor", "camera.outdoor_1", "camera.outdoor1", "camera.outdoor_camera", "camera.outside"):
            camera_aliases[alias] = outdoor_camera
    if living_room_camera:
        for alias in ("camera.living_room", "camera.living_room_camera"):
            camera_aliases[alias] = living_room_camera

    motion_camera_map = _infer_motion_camera_map(states)
    bypass_global_motion_cameras = sorted(set(motion_camera_map.values()))
    camera_vision_prompts = _build_camera_vision_prompts(outdoor_camera, vehicles)
    exclude_entities = _infer_excluded_entities(states)
    sensor_threshold_rules = _infer_sensor_threshold_rules(states)
    sensor_snapshot_excludes, sensor_temp_excludes = _infer_sensor_exclusions(states, extra_notes)

    return {
        "default_doorbell_camera": default_doorbell_camera,
        "weather_entity": weather_entity,
        "camera_aliases": camera_aliases,
        "motion_camera_map": motion_camera_map,
        "bypass_global_motion_cameras": bypass_global_motion_cameras,
        "camera_vision_prompts": camera_vision_prompts,
        "exclude_entities": sorted(exclude_entities),
        "sensor_snapshot_exclude_prefixes": sorted(sensor_snapshot_excludes),
        "sensor_temp_exclude_prefixes": sorted(sensor_temp_excludes),
        "sensor_threshold_rules": sensor_threshold_rules,
    }


def extract_known_entity_ids(prompt_text: str) -> set[str]:
    return set(re.findall(r"\b\w+\.\w[\w_]*", prompt_text))


@dataclass
class NewEntityInfo:
    entity_id: str
    friendly_name: str
    domain: str
    state: str
    device_class: str
    unit: str
    area: str          # area name, empty string if unknown
    group: str         # logical group label (e.g. "Climate and comfort")


def discover_new_entities(
    states: list[dict],
    known: set[str],
    area_by_entity: dict[str, str] | None = None,
) -> list[NewEntityInfo]:
    """
    Return structured info for each HA entity not yet in the system prompt.
    Skips unavailable/unknown states and infrastructure noise.
    Enriches each entry with its HA area name when available.
    """
    result: list[NewEntityInfo] = []
    area_map = area_by_entity or {}
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if entity_id in known:
            continue
        if not _should_include_entity(state):
            continue
        attrs       = state.get("attributes") or {}
        domain      = entity_id.split(".", 1)[0]
        friendly    = str(attrs.get("friendly_name", "")).strip()
        cur_state   = str(state.get("state", "")).strip()
        unit        = str(attrs.get("unit_of_measurement", "")).strip()
        device_class = str(attrs.get("device_class", "")).strip()
        area        = area_map.get(entity_id, "")
        group       = _classify_group(state)
        result.append(NewEntityInfo(
            entity_id=entity_id,
            friendly_name=friendly,
            domain=domain,
            state=cur_state,
            device_class=device_class,
            unit=unit,
            area=area,
            group=group,
        ))
    return result


def summarise_new_entities(
    states: list[dict],
    known: set[str],
    limit_per_group: int = 40,
    area_by_entity: dict[str, str] | None = None,
) -> str:
    """Build a text block listing new entities for LLM integration."""
    entities = discover_new_entities(states, known, area_by_entity)
    groups: dict[str, list[str]] = defaultdict(list)
    for e in entities:
        line = f"  {e.entity_id}"
        if e.friendly_name and e.friendly_name != e.entity_id:
            line += f" | {e.friendly_name}"
        if e.state:
            line += f" | {e.state}"
            if e.unit:
                line += f" {e.unit}"
        if e.device_class:
            line += f" [{e.device_class}]"
        if e.area:
            line += f" — {e.area}"
        groups[e.domain].append(line)

    if not groups:
        return ""

    parts: list[str] = []
    for domain in sorted(groups):
        entries = groups[domain]
        parts.append(f"{domain} ({len(entries)}):")
        parts.extend(entries[:limit_per_group])
    return "\n".join(parts)


async def fetch_area_mapping(ha_url: str, ha_token: str) -> dict[str, str]:
    """
    Returns a dict of entity_id → area_name by querying HA WebSocket registries.
    Area is resolved via: entity.area_id > entity.device_id → device.area_id → area.name
    Returns empty dict on any failure (non-fatal).
    """
    ws_url = ha_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    try:
        import websockets  # type: ignore
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        async def _ws_list(msg_id: int, msg_type: str) -> list[dict]:
            async with websockets.connect(ws_url, ssl=ssl_ctx, max_size=20 * 1024 * 1024) as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
                await ws.recv()
                await ws.send(json.dumps({"id": msg_id, "type": msg_type}))
                resp = json.loads(await ws.recv())
                return resp.get("result") or []

        areas_raw, entities_raw, devices_raw = await asyncio.gather(
            _ws_list(1, "config/area_registry/list"),
            _ws_list(2, "config/entity_registry/list"),
            _ws_list(3, "config/device_registry/list"),
        )

        area_name: dict[str, str] = {a["area_id"]: a["name"] for a in areas_raw if a.get("area_id")}
        device_area: dict[str, str] = {
            d["id"]: area_name[d["area_id"]]
            for d in devices_raw
            if d.get("id") and d.get("area_id") and d["area_id"] in area_name
        }
        result: dict[str, str] = {}
        for e in entities_raw:
            eid = e.get("entity_id", "")
            if not eid:
                continue
            # entity-level area takes precedence over device-level area
            if e.get("area_id") and e["area_id"] in area_name:
                result[eid] = area_name[e["area_id"]]
            elif e.get("device_id") and e["device_id"] in device_area:
                result[eid] = device_area[e["device_id"]]
        return result
    except Exception as exc:
        import structlog
        structlog.get_logger().warning("prompt_bootstrap.area_fetch_failed", exc=str(exc))
        return {}
