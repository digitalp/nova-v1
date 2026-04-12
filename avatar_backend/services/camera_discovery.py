"""
CameraDiscoveryService — auto-discovers cameras, motion sensors, and their
area assignments from Home Assistant's entity/area/device registries.

Replaces hardcoded _LEGACY_MOTION_CAMERA_MAP with dynamic discovery:
  1. Queries HA WebSocket API for area, device, and entity registries
  2. Identifies outdoor/entrance areas and their cameras + motion sensors
  3. Builds motion_camera_map, bypass cameras, and vision prompt hints
  4. Falls back to legacy hardcoded maps if discovery fails

Usage:
    discovery = CameraDiscoveryService(ha_url, ha_token)
    result = await discovery.discover()
    # result.motion_camera_map, result.bypass_cameras, etc.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import structlog
import websockets

_LOGGER = structlog.get_logger()

# Area name patterns that indicate outdoor/entrance zones
_OUTDOOR_AREA_PATTERNS = re.compile(
    r"outdoor|outside|garden|driveway|entrance|front.*(house|door)|"
    r"rear|side.*outdoor|patio|porch|garage|yard|carport",
    re.IGNORECASE,
)

# Device classes for motion-type binary sensors
_MOTION_DEVICE_CLASSES = {"motion", "occupancy", "presence", "moving"}

# Default vision prompts by area type
_VISION_PROMPT_OUTDOOR = (
    "This is a security camera snapshot of an outdoor area. "
    "Only alert if you see a person, an unfamiliar vehicle, an animal, or unusual activity. "
    "If motion was caused solely by plants moving in the wind, lighting changes, or has no obvious cause, "
    "reply with exactly: NO_MOTION\n"
    "Otherwise describe what you see in 1-2 sentences. "
    "Do NOT mention age, race, gender or personal attributes."
)

_VISION_PROMPT_ENTRANCE = (
    "This is a security camera snapshot of a door or entrance. "
    "Describe who or what triggered the motion sensor or doorbell. "
    "If nothing meaningful is visible or motion has no obvious cause, "
    "reply with exactly: NO_MOTION\n"
    "Otherwise describe what you see in 1-2 sentences. "
    "Do NOT mention age, race, gender or personal attributes. "
    "If you can see someone making a delivery (carrying a parcel, delivery uniform, or liveried van), "
    "append a new line with EXACTLY:\n"
    "DELIVERY: <company>\n"
    "where <company> is one of: DHL, Royal Mail, Amazon, or Unknown. "
    "Only include the DELIVERY line if you are confident a delivery is taking place."
)

_VISION_PROMPT_DRIVEWAY = (
    "This is a security camera snapshot of a residential driveway. "
    "Only alert if you see: a person, an unfamiliar vehicle, an unexpected object, or unusual activity. "
    "If motion was caused solely by a parked car (e.g. lighting change) or has no obvious cause, "
    "reply with exactly: NO_MOTION\n"
    "Otherwise describe what you see in 1-2 sentences. "
    "Do NOT mention age, race, gender or personal attributes. "
    "If you can see someone making a delivery (carrying a parcel, delivery uniform, or liveried van), "
    "append a new line with EXACTLY:\n"
    "DELIVERY: <company>\n"
    "where <company> is one of: DHL, Royal Mail, Amazon, or Unknown. "
    "Only include the DELIVERY line if you are confident a delivery is taking place."
)


@dataclass
class DiscoveryResult:
    """Result of camera/motion sensor auto-discovery."""
    motion_camera_map: dict[str, str] = field(default_factory=dict)
    bypass_global_motion_cameras: set[str] = field(default_factory=set)
    camera_vision_prompts: dict[str, str] = field(default_factory=dict)
    exclude_entities: set[str] = field(default_factory=set)
    camera_areas: dict[str, str] = field(default_factory=dict)  # camera_entity → area_name
    outdoor_cameras: list[str] = field(default_factory=list)
    discovered: bool = False



class CameraDiscoveryService:
    """Discovers cameras and motion sensors from HA registries."""

    def __init__(self, ha_url: str, ha_token: str) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token

    async def discover(self, timeout_s: float = 15.0) -> DiscoveryResult:
        """Query HA registries and build camera/motion maps.

        Returns a DiscoveryResult with auto-discovered mappings.
        Falls back to empty result if HA is unreachable.
        """
        try:
            return await asyncio.wait_for(self._do_discover(), timeout=timeout_s)
        except asyncio.TimeoutError:
            _LOGGER.warning("camera_discovery.timeout", timeout_s=timeout_s)
            return DiscoveryResult()
        except Exception as exc:
            _LOGGER.warning("camera_discovery.failed", exc=str(exc))
            return DiscoveryResult()

    async def _do_discover(self) -> DiscoveryResult:
        ws_url = (
            self._ha_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + "/api/websocket"
        )

        async with websockets.connect(
            ws_url, ping_interval=30, ping_timeout=10, open_timeout=10,
            max_size=16 * 1024 * 1024,  # 16MB — entity registry can be large
        ) as ws:
            # Auth handshake
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Expected auth_required, got {msg.get('type')}")
            await ws.send(json.dumps({"type": "auth", "access_token": self._ha_token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"HA auth failed: {msg}")

            # Fetch registries in parallel
            areas = await self._ws_command(ws, 2, {"type": "config/area_registry/list"})
            devices = await self._ws_command(ws, 3, {"type": "config/device_registry/list"})
            entities = await self._ws_command(ws, 4, {"type": "config/entity_registry/list"})

        return self._build_result(areas, devices, entities)

    async def _ws_command(self, ws, cmd_id: int, payload: dict) -> list[dict]:
        """Send a WS command and return the result list."""
        payload["id"] = cmd_id
        await ws.send(json.dumps(payload))
        # Read messages until we get the response for our command ID
        for _ in range(10):
            msg = json.loads(await ws.recv())
            if msg.get("id") == cmd_id:
                if msg.get("type") != "result" or not msg.get("success"):
                    _LOGGER.warning("camera_discovery.ws_command_failed", cmd=payload.get("type"), msg=msg)
                    return []
                return msg.get("result", [])
        _LOGGER.warning("camera_discovery.ws_command_no_response", cmd=payload.get("type"))
        return []

    def _build_result(
        self,
        areas: list[dict],
        devices: list[dict],
        entities: list[dict],
    ) -> DiscoveryResult:
        result = DiscoveryResult(discovered=True)

        # Build area lookup: area_id → area_name
        area_map: dict[str, str] = {}
        outdoor_area_ids: set[str] = set()
        entrance_area_ids: set[str] = set()
        driveway_area_ids: set[str] = set()

        for area in areas:
            aid = area.get("area_id", "")
            name = area.get("name", "")
            if not aid or not name:
                continue
            area_map[aid] = name
            if _OUTDOOR_AREA_PATTERNS.search(name):
                outdoor_area_ids.add(aid)
            name_lower = name.lower()
            if any(kw in name_lower for kw in ("entrance", "front door", "front of house", "doorbell")):
                entrance_area_ids.add(aid)
            if "driveway" in name_lower:
                driveway_area_ids.add(aid)

        # Build device → area lookup
        device_area: dict[str, str] = {}  # device_id → area_id
        for dev in devices:
            did = dev.get("id", "")
            aid = dev.get("area_id", "")
            if did and aid:
                device_area[did] = aid

        # Classify entities
        cameras_by_area: dict[str, list[str]] = {}  # area_id → [camera entities]
        motion_sensors_by_area: dict[str, list[str]] = {}  # area_id → [motion sensor entities]
        entity_area_map: dict[str, str] = {}  # entity_id → area_id

        for ent in entities:
            eid = ent.get("entity_id", "")
            if not eid:
                continue

            # Resolve area: entity-level area_id overrides device-level
            ent_area = ent.get("area_id", "")
            if not ent_area:
                dev_id = ent.get("device_id", "")
                ent_area = device_area.get(dev_id, "")

            if not ent_area:
                continue

            entity_area_map[eid] = ent_area
            domain = eid.split(".")[0] if "." in eid else ""

            # Cameras — prefer fluent streams for vision snapshots
            if domain == "camera":
                cameras_by_area.setdefault(ent_area, []).append(eid)
                if ent_area in outdoor_area_ids:
                    result.outdoor_cameras.append(eid)
                    result.camera_areas[eid] = area_map.get(ent_area, "")

            # Motion binary sensors
            if domain == "binary_sensor":
                device_class = ent.get("original_device_class", "") or ""
                # Also check entity name patterns for motion sensors without device_class
                is_motion = (
                    device_class in _MOTION_DEVICE_CLASSES
                    or "_motion" in eid
                    or "_person" in eid
                    or "_visitor" in eid
                )
                if is_motion and ent_area in outdoor_area_ids:
                    motion_sensors_by_area.setdefault(ent_area, []).append(eid)

        # Build motion_camera_map: motion_sensor → best camera in same area
        for area_id in outdoor_area_ids:
            cameras = cameras_by_area.get(area_id, [])
            sensors = motion_sensors_by_area.get(area_id, [])
            if not cameras or not sensors:
                continue

            # Prefer fluent stream cameras for vision (MJPEG compatible)
            best_camera = self._pick_best_camera(cameras)
            if not best_camera:
                continue

            for sensor in sensors:
                result.motion_camera_map[sensor] = best_camera

            # Driveway cameras bypass global cooldown (high priority)
            if area_id in driveway_area_ids:
                result.bypass_global_motion_cameras.add(best_camera)

            # Assign vision prompts based on area type
            area_name = area_map.get(area_id, "")
            if area_id in driveway_area_ids:
                result.camera_vision_prompts[best_camera] = _VISION_PROMPT_DRIVEWAY
            elif area_id in entrance_area_ids:
                result.camera_vision_prompts[best_camera] = _VISION_PROMPT_ENTRANCE
            else:
                result.camera_vision_prompts[best_camera] = _VISION_PROMPT_OUTDOOR

        _LOGGER.info(
            "camera_discovery.complete",
            areas_found=len(outdoor_area_ids),
            cameras=len(result.outdoor_cameras),
            motion_mappings=len(result.motion_camera_map),
            bypass_cameras=len(result.bypass_global_motion_cameras),
        )
        return result

    @staticmethod
    def _pick_best_camera(cameras: list[str]) -> str | None:
        """Pick the best camera entity for vision snapshots.

        Preference order:
        1. Fluent stream (MJPEG compatible, good for snapshots)
        2. Any non-mainstream camera
        3. Mainstream as last resort
        """
        fluent = [c for c in cameras if "fluent" in c]
        if fluent:
            return fluent[0]
        non_mainstream = [c for c in cameras if "mainstream" not in c and "profile000" not in c]
        if non_mainstream:
            return non_mainstream[0]
        return cameras[0] if cameras else None
