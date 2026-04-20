from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
from avatar_backend.runtime_paths import config_dir, install_dir


_INSTALL_DIR = install_dir()
_CONFIG_DIR = config_dir()
_RUNTIME_FILE = _CONFIG_DIR / "home_runtime.json"


@dataclass
class HomeRuntimeConfig:
    default_doorbell_camera: str | None = None
    weather_entity: str | None = None
    camera_aliases: dict[str, str] = field(default_factory=dict)
    motion_camera_map: dict[str, str] = field(default_factory=dict)
    bypass_global_motion_cameras: set[str] = field(default_factory=set)
    camera_vision_prompts: dict[str, str] = field(default_factory=dict)
    exclude_entities: set[str] = field(default_factory=set)
    sensor_snapshot_exclude_prefixes: tuple[str, ...] = ()
    sensor_temp_exclude_prefixes: tuple[str, ...] = ()
    sensor_threshold_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    phone_notify_services: list[str] = field(default_factory=list)
    energy_summary_entities: dict[str, str] = field(default_factory=dict)
    energy_device_entities: dict[str, str] = field(default_factory=dict)
    camera_labels: dict[str, str] = field(default_factory=dict)
    blueiris_camera_map: dict[str, str] = field(default_factory=dict)
    polling_only_cameras: list[str] = field(default_factory=list)
    vision_enabled_cameras: list[str] = field(default_factory=list)
    camera_room_map: dict[str, str] = field(default_factory=dict)  # camera_id → room_id slug
    sensor_shortcuts: dict[str, str] = field(default_factory=dict)
    kitchen_watch_camera: str = "camera.tangu_home_kitchen"
    kitchen_watch_tasks: dict[str, int] = field(default_factory=lambda: {"empty_kitchen_bin": 7200})
    living_room_camera: str = "camera.reolink_living_room_profile000_mainstream"
    blind_check_camera: str = "camera.reolink_living_room_profile000_mainstream"
    blind_reminder_names: str = "Jason, Miya, Joel or Tse"
    greeting_camera: str = "camera.tangu_home_hallway"
    greeting_cooldown_minutes: int = 30
    greeting_active_start: int = 6   # hour — don't greet before this
    greeting_active_end: int = 23    # hour — don't greet after this


def load_home_runtime_config() -> HomeRuntimeConfig:
    if not _RUNTIME_FILE.exists():
        return HomeRuntimeConfig()

    try:
        raw = json.loads(_RUNTIME_FILE.read_text(encoding="utf-8"))
    except Exception:
        return HomeRuntimeConfig()

    if not isinstance(raw, dict):
        return HomeRuntimeConfig()

    return HomeRuntimeConfig(
        default_doorbell_camera=_as_optional_str(raw.get("default_doorbell_camera")),
        weather_entity=_as_optional_str(raw.get("weather_entity")),
        camera_aliases=_as_str_dict(raw.get("camera_aliases")),
        motion_camera_map=_as_str_dict(raw.get("motion_camera_map")),
        bypass_global_motion_cameras=set(_as_str_list(raw.get("bypass_global_motion_cameras"))),
        camera_vision_prompts=_as_str_dict(raw.get("camera_vision_prompts")),
        exclude_entities=set(_as_str_list(raw.get("exclude_entities"))),
        sensor_snapshot_exclude_prefixes=tuple(_as_str_list(raw.get("sensor_snapshot_exclude_prefixes"))),
        sensor_temp_exclude_prefixes=tuple(_as_str_list(raw.get("sensor_temp_exclude_prefixes"))),
        sensor_threshold_rules=_as_dict_of_dicts(raw.get("sensor_threshold_rules")),
        phone_notify_services=_as_str_list(raw.get("phone_notify_services")),
        energy_summary_entities=_as_str_dict(raw.get("energy_summary_entities")),
        energy_device_entities=_as_str_dict(raw.get("energy_device_entities")),
        camera_labels=_as_str_dict(raw.get("camera_labels")),
        blueiris_camera_map=_as_str_dict(raw.get("blueiris_camera_map")),
        polling_only_cameras=_as_str_list(raw.get("polling_only_cameras")),
        vision_enabled_cameras=_as_str_list(raw.get("vision_enabled_cameras")),
        camera_room_map=_as_str_dict(raw.get("camera_room_map")),
        sensor_shortcuts=_as_str_dict(raw.get("sensor_shortcuts")),
        kitchen_watch_camera=str(raw.get("kitchen_watch_camera") or "camera.tangu_home_kitchen"),
        kitchen_watch_tasks={str(k): int(v) for k, v in (raw.get("kitchen_watch_tasks") or {"empty_kitchen_bin": 7200}).items()},
        living_room_camera=str(raw.get("living_room_camera") or "camera.reolink_living_room_profile000_mainstream"),
        blind_check_camera=str(raw.get("blind_check_camera") or "camera.reolink_living_room_profile000_mainstream"),
        blind_reminder_names=str(raw.get("blind_reminder_names") or "Jason, Miya, Joel or Tse"),
        greeting_camera=str(raw.get("greeting_camera") or "camera.tangu_home_hallway"),
        greeting_cooldown_minutes=int(raw.get("greeting_cooldown_minutes") or 30),
        greeting_active_start=int(raw.get("greeting_active_start") or 6),
        greeting_active_end=int(raw.get("greeting_active_end") or 23),
    )


def write_home_runtime_config(config: dict[str, Any], path: Path | None = None) -> None:
    target = path or _RUNTIME_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _as_optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, str) and key.strip() and item.strip():
            result[key.strip()] = item.strip()
    return result


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return result


def _as_dict_of_dicts(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, dict):
            result[key] = item
    return result
