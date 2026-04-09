from unittest.mock import AsyncMock, MagicMock

import pytest

from avatar_backend.services.camera_event_service import CameraEventService
from avatar_backend.services.event_service import EventService


@pytest.mark.asyncio
async def test_camera_event_service_suppresses_no_person_doorbell():
    ha = MagicMock()
    ha.resolve_camera_entity.return_value = "camera.front_door"
    ha.fetch_camera_image = AsyncMock(return_value=b"image-bytes")

    llm = MagicMock()
    llm.describe_image = AsyncMock(return_value="NO_PERSON")

    service = CameraEventService(ha_proxy=ha, llm_service=llm, event_service=EventService())
    result = await service.describe_doorbell("camera.doorbell")

    assert result["camera_entity_id"] == "camera.front_door"
    assert result["suppressed"] is True
    assert result["message"] == "no_person_visible"


@pytest.mark.asyncio
async def test_camera_event_service_parses_delivery_and_builds_canonical_motion_event():
    ha = MagicMock()
    ha.resolve_camera_entity.return_value = "camera.driveway"
    ha.fetch_camera_image = AsyncMock(return_value=b"image-bytes")

    llm = MagicMock()
    llm.describe_image_with_gemini = AsyncMock(
        return_value="A courier is walking to the porch.\nDELIVERY: DHL"
    )

    service = CameraEventService(ha_proxy=ha, llm_service=llm, event_service=EventService())
    result = await service.analyze_motion(
        camera_entity_id="camera.outdoor_2",
        location="driveway",
        trigger_entity_id="binary_sensor.driveway_motion",
        source="proactive_motion",
    )

    assert result["camera_entity_id"] == "camera.driveway"
    assert result["is_delivery"] is True
    assert result["delivery_company"] == "DHL"
    assert result["message"].startswith("Delivery alert!")
    assert result["canonical_event"]["event_type"] == "delivery_detected"
    assert result["canonical_event"]["camera_entity_id"] == "camera.driveway"
    assert result["canonical_event"]["event_context"]["source"] == "proactive_motion"


def test_camera_event_service_builds_package_event():
    ha = MagicMock()
    ha.resolve_camera_entity.return_value = "camera.front_door"
    llm = MagicMock()

    service = CameraEventService(ha_proxy=ha, llm_service=llm, event_service=EventService())
    result = service.build_package_event(
        camera_entity_id="camera.doorbell",
        source="package_announce",
        trigger_entity_id="binary_sensor.reolink_video_doorbell_poe_package",
    )

    assert result["camera_entity_id"] == "camera.front_door"
    assert result["canonical_event"]["event_type"] == "package_delivery"
    assert result["canonical_event"]["camera_entity_id"] == "camera.front_door"
    assert result["canonical_event"]["event_context"]["source"] == "package_announce"
