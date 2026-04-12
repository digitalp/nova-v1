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


def _replace_home_profile_section(template_text: str, rendered_section: str) -> str:
    pattern = re.compile(
        r"={70}\n2\. HOME PROFILE\n={70}\n.*?\n={70}\nENTITY ID RULES — CRITICAL, NEVER VIOLATE\n={70}",
        re.DOTALL,
    )
    replacement = rendered_section + "\n\n" + "=" * 70 + "\nENTITY ID RULES — CRITICAL, NEVER VIOLATE\n" + "=" * 70
    updated = pattern.sub(replacement, template_text, count=1)
    if updated == template_text:
        raise RuntimeError("Could not locate HOME PROFILE section in system prompt template")
    return updated


def _strip_template_comments(text: str) -> str:
    kept_lines: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        kept_lines.append(line.rstrip())
    return "\n".join(kept_lines)


def _remove_placeholder_lines(text: str) -> str:
    kept_lines: list[str] = []
    for line in text.splitlines():
        if re.search(r"<[A-Z0-9_]+>", line):
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines)


def _render_home_profile(
    *,
    address: str,
    timezone_name: str,
    household: list[HouseholdMember],
    vehicles: list[VehicleProfile],
    extra_notes: list[str],
    states: list[dict],
    source_label: str,
) -> str:
    weather_entity = _pick_weather_entity(states)
    inventory = _build_inventory(states, weather_entity)
    personal_devices = _match_personal_devices(states, household)

    lines: list[str] = [
        "=" * 70,
        "2. HOME PROFILE",
        "=" * 70,
        "",
        f"Location: {address}",
        f"Timezone: {timezone_name}",
        f"Bootstrap source: {source_label}",
        "This section was generated during installer setup from the current Home Assistant state.",
        "Treat it as the initial source of truth and refresh it when the home setup changes.",
        "",
        "Household:",
    ]
    for member in household:
        detail_suffix = f" ({member.details})" if member.details else ""
        lines.append(f"  {member.name} — {member.role}{detail_suffix}")

    if vehicles:
        lines.extend(["", "Vehicles:"])
        for vehicle in vehicles:
            lines.append(f"  {vehicle.owner} — {vehicle.description}")

    if extra_notes:
        lines.extend(["", "Stable household notes:"])
        for note in extra_notes:
            lines.append(f"  - {note}")

    if personal_devices:
        lines.extend(["", "Personal devices and presence:"])
        for member_name, device_lines in personal_devices.items():
            lines.append(f"  {member_name}:")
            for entry in device_lines:
                lines.append(f"    {entry}")

    lines.extend(
        [
            "",
            "Home Assistant entity inventory:",
            "  The groups below list the exact entities discovered during install.",
            "  If an exact ID is missing here later, call get_entities(domain) before acting.",
        ]
    )

    if weather_entity:
        lines.extend(
            [
                "",
                "Weather:",
                f"  {weather_entity} — primary weather entity; use this first for weather questions.",
            ]
        )

    for title, entries in inventory:
        if not entries:
            continue
        lines.extend(["", f"{title}:"])
        for entry in entries:
            lines.append(f"  {entry}")

    if not states:
        lines.extend(
            [
                "",
                "Discovery status:",
                "  Home Assistant entities were not fetched during install.",
                "  Use the admin panel prompt sync after HA credentials are confirmed to enrich this section.",
            ]
        )

    return "\n".join(lines)


def _pick_weather_entity(states: list[dict]) -> str | None:
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if entity_id.startswith("weather."):
            return entity_id
    return None


def _pick_camera(states: list[dict], keywords: tuple[str, ...]) -> str | None:
    matches: list[tuple[int, str]] = []
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if not entity_id.startswith("camera."):
            continue
        attrs = state.get("attributes") or {}
        haystack = f"{entity_id} {attrs.get('friendly_name', '')}".lower()
        score = sum(1 for keyword in keywords if keyword in haystack)
        if score:
            matches.append((score, entity_id))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]))
    return matches[0][1]


def _build_inventory(states: list[dict], weather_entity: str | None) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {
        "People and presence": [],
        "Climate and comfort": [],
        "Lights and scenes": [],
        "Media and speakers": [],
        "Security, cameras, and access": [],
        "Power, appliances, and controls": [],
        "Sensors and monitoring": [],
        "Vehicle and transport": [],
    }

    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if not _should_include_entity(state):
            continue
        if entity_id == weather_entity:
            continue
        group = _classify_group(state)
        rendered = _render_entity_line(state)
        if rendered not in groups[group]:
            groups[group].append(rendered)

    ordered: list[tuple[str, list[str]]] = []
    for title, entries in groups.items():
        ordered.append((title, sorted(entries)[:60]))
    return ordered


def _should_include_entity(state: dict) -> bool:
    entity_id = str(state.get("entity_id", ""))
    if "." not in entity_id:
        return False
    domain = entity_id.split(".", 1)[0]
    if domain not in _DISCOVERY_DOMAINS:
        return False
    if any(entity_id.startswith(prefix) for prefix in _SKIP_PREFIXES):
        return False

    attrs = state.get("attributes") or {}
    friendly_name = str(attrs.get("friendly_name", ""))
    lowered_name = friendly_name.lower()
    lowered_entity = entity_id.lower()
    if any(fragment in lowered_name or fragment in lowered_entity for fragment in _SKIP_NAME_FRAGMENTS):
        return False

    current_state = str(state.get("state", "")).strip().lower()
    if current_state in {"", "unknown", "unavailable"}:
        return False

    if domain == "sensor":
        device_class = str(attrs.get("device_class", "")).lower()
        if device_class:
            return device_class in _SENSOR_DEVICE_CLASSES
        return any(keyword in lowered_entity or keyword in lowered_name for keyword in _VEHICLE_KEYWORDS + ("battery", "cost", "energy", "humidity", "power", "temp", "temperature"))

    if domain == "binary_sensor":
        device_class = str(attrs.get("device_class", "")).lower()
        return device_class in _PRESENCE_BINARY_CLASSES | _ACCESS_BINARY_CLASSES | _SAFETY_BINARY_CLASSES or any(
            keyword in lowered_entity or keyword in lowered_name for keyword in _VEHICLE_KEYWORDS
        )

    return True


def _classify_group(state: dict) -> str:
    entity_id = str(state.get("entity_id", ""))
    domain = entity_id.split(".", 1)[0]
    attrs = state.get("attributes") or {}
    device_class = str(attrs.get("device_class", "")).lower()
    lowered = f"{entity_id.lower()} {str(attrs.get('friendly_name', '')).lower()}"

    if domain in {"person", "device_tracker"} or device_class in _PRESENCE_BINARY_CLASSES:
        return "People and presence"
    if domain in {"climate", "fan", "humidifier", "water_heater", "weather"}:
        return "Climate and comfort"
    if domain == "light":
        return "Lights and scenes"
    if domain == "media_player":
        return "Media and speakers"
    if domain in {"camera", "lock", "cover"} or device_class in _ACCESS_BINARY_CLASSES | _SAFETY_BINARY_CLASSES:
        return "Security, cameras, and access"
    if any(keyword in lowered for keyword in _VEHICLE_KEYWORDS):
        return "Vehicle and transport"
    if domain in {"input_boolean", "input_number", "input_select", "input_text", "number", "switch"}:
        return "Power, appliances, and controls"
    return "Sensors and monitoring"


def _render_entity_line(state: dict) -> str:
    entity_id = str(state.get("entity_id", ""))
    attrs = state.get("attributes") or {}
    label_parts: list[str] = []

    friendly_name = str(attrs.get("friendly_name", "")).strip()
    if friendly_name and friendly_name != entity_id:
        label_parts.append(friendly_name)

    device_class = str(attrs.get("device_class", "")).strip()
    if device_class:
        label_parts.append(device_class.replace("_", " "))

    unit = str(attrs.get("unit_of_measurement", "")).strip()
    if unit:
        label_parts.append(unit)

    if not label_parts:
        return entity_id
    return f"{entity_id} — {'; '.join(label_parts)}"


def _match_personal_devices(states: list[dict], household: Iterable[HouseholdMember]) -> dict[str, list[str]]:
    matched: dict[str, list[str]] = {}
    for member in household:
        slug = _slugify(member.name)
        entries: list[str] = []
        for state in states:
            entity_id = str(state.get("entity_id", ""))
            if "." not in entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            if domain not in {"person", "device_tracker", "sensor", "binary_sensor"}:
                continue
            attrs = state.get("attributes") or {}
            friendly_name = str(attrs.get("friendly_name", ""))
            haystack = f"{entity_id} {friendly_name}".lower()
            if slug not in _slugify(haystack):
                continue
            if domain == "sensor" and not any(keyword in haystack for keyword in ("battery", "phone", "tablet", "watch", "laptop")):
                continue
            if domain == "binary_sensor" and not any(keyword in haystack for keyword in ("charging", "phone", "watch", "tablet", "laptop")):
                continue
            rendered = _render_entity_line(state)
            if rendered not in entries:
                entries.append(rendered)
        if entries:
            matched[member.name] = sorted(entries)[:12]
    return matched


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _infer_motion_camera_map(states: list[dict]) -> dict[str, str]:
    cameras = [state for state in states if str(state.get("entity_id", "")).startswith("camera.")]
    mappings: dict[str, str] = {}
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if not entity_id.startswith("binary_sensor."):
            continue
        attrs = state.get("attributes") or {}
        device_class = str(attrs.get("device_class", "")).lower()
        haystack = f"{entity_id} {attrs.get('friendly_name', '')}".lower()
        if device_class not in {"motion", "occupancy", "presence", "moving"} and not any(
            keyword in haystack for keyword in ("motion", "person", "visitor", "package", "vehicle")
        ):
            continue
        best_camera = _find_best_matching_camera(haystack, cameras)
        if best_camera:
            mappings[entity_id] = best_camera
    return mappings


def _find_best_matching_camera(sensor_haystack: str, cameras: list[dict]) -> str | None:
    ranked: list[tuple[int, str]] = []
    sensor_tokens = {token for token in re.split(r"[^a-z0-9]+", sensor_haystack) if len(token) > 2}
    for camera in cameras:
        entity_id = str(camera.get("entity_id", ""))
        attrs = camera.get("attributes") or {}
        camera_haystack = f"{entity_id} {attrs.get('friendly_name', '')}".lower()
        camera_tokens = {token for token in re.split(r"[^a-z0-9]+", camera_haystack) if len(token) > 2}
        overlap = sensor_tokens & camera_tokens
        score = len(overlap)
        if score:
            ranked.append((score, entity_id))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1]


def _build_camera_vision_prompts(outdoor_camera: str | None, vehicles: list[VehicleProfile]) -> dict[str, str]:
    if not outdoor_camera or not vehicles:
        return {}
    primary_vehicle = vehicles[0]
    vehicle_desc = primary_vehicle.description.strip()
    prompt = (
        "This is a residential outdoor security camera snapshot. "
        f"The household often has {vehicle_desc} parked in view. "
        "Treat that vehicle as normal background context unless it appears to be moving, damaged, open, or involved in something unusual. "
        "Only alert if you see a person, an unfamiliar vehicle, an unexpected object, a delivery, or unusual activity. "
        "If motion was caused solely by the known parked household vehicle or has no obvious cause, reply with exactly: NO_MOTION\n"
        "Otherwise describe what you see in 1-2 sentences. "
        "Do NOT mention age, race, gender or personal attributes. "
        "If you can see someone making a delivery, append a new line with EXACTLY:\n"
        "DELIVERY: <company>\n"
        "where <company> is one of: DHL, Royal Mail, Amazon, or Unknown."
    )
    return {outdoor_camera: prompt}


def _infer_excluded_entities(states: list[dict]) -> set[str]:
    excluded: set[str] = set()
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        if not entity_id.startswith("binary_sensor."):
            continue
        attrs = state.get("attributes") or {}
        haystack = f"{entity_id} {attrs.get('friendly_name', '')}".lower()
        if "doorbell" in haystack and any(keyword in haystack for keyword in ("person", "vehicle", "visitor", "face", "package")):
            excluded.add(entity_id)
    return excluded


def _infer_sensor_threshold_rules(states: list[dict]) -> dict[str, dict]:
    rules: dict[str, dict] = {}
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        attrs = state.get("attributes") or {}
        haystack = f"{entity_id} {attrs.get('friendly_name', '')}".lower()
        if entity_id.startswith("sensor.") and "fuel" in haystack and "level" in haystack:
            rules[entity_id] = {
                "min": 15.0,
                "max": None,
                "label": attrs.get("friendly_name") or "Vehicle fuel level",
                "unit": attrs.get("unit_of_measurement", "%") or "%",
                "min_msg": "Heads up — the vehicle fuel level is low at {value}%. Consider filling up soon.",
            }
        if entity_id.startswith("sensor.") and "days_until_collection" in entity_id:
            friendly_name = str(attrs.get("friendly_name", "Bin collection")).strip() or "Bin collection"
            rules[entity_id] = {
                "min": None,
                "max": None,
                "equals": 1,
                "label": friendly_name,
                "unit": attrs.get("unit_of_measurement", "days") or "days",
                "equals_msg": f"Reminder: {friendly_name.lower()} is due tomorrow.",
            }
    return rules


def _infer_sensor_exclusions(states: list[dict], extra_notes: list[str]) -> tuple[set[str], set[str]]:
    snapshot_excludes: set[str] = {"sensor.ble_", "sensor.cpu_", "sensor.monthly_"}
    temp_excludes: set[str] = set()
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        attrs = state.get("attributes") or {}
        haystack = f"{entity_id} {attrs.get('friendly_name', '')}".lower()
        if not entity_id.startswith("sensor."):
            continue
        if any(keyword in haystack for keyword in ("awtrix", "uptime", "signal", "linkquality", "firmware", "version")):
            snapshot_excludes.add(_entity_prefix(entity_id))
        if any(keyword in haystack for keyword in ("device temperature", "thermo valve", "thermo idle", "thermo closing")):
            snapshot_excludes.add(_entity_prefix(entity_id))
            temp_excludes.add(_entity_prefix(entity_id))
    for note in extra_notes:
        if "awtrix" in note.lower():
            snapshot_excludes.add("sensor.awtrix_")
    return snapshot_excludes, temp_excludes


def _entity_prefix(entity_id: str) -> str:
    if "." not in entity_id:
        return entity_id
    domain, name = entity_id.split(".", 1)
    parts = name.split("_")
    if len(parts) <= 2:
        return entity_id
    return f"{domain}.{ '_'.join(parts[:2]) }_"
