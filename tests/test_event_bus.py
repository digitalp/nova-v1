from __future__ import annotations

from pydantic import ValidationError
import pytest

from avatar_backend.models.events import EventEnvelope
from avatar_backend.services.event_bus import EventBusService


@pytest.mark.asyncio
async def test_event_bus_publishes_validated_typed_events():
    bus = EventBusService()
    seen: list[EventEnvelope] = []

    async def handler(event: EventEnvelope) -> None:
        seen.append(event)

    bus.subscribe("doorbell", handler)
    event = await bus.publish(
        {
            "event_id": "evt-1",
            "event_type": "doorbell",
            "source": "doorbell",
            "summary": "Front door live view",
            "confidence": 0.92,
        }
    )

    assert event.event_id == "evt-1"
    assert seen[0].event_type == "doorbell"
    assert seen[0].confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_event_bus_supports_wildcard_subscribers():
    bus = EventBusService()
    seen: list[str] = []

    def wildcard(event: EventEnvelope) -> None:
        seen.append(event.event_type)

    bus.subscribe("*", wildcard)
    await bus.publish({"event_id": "evt-2", "event_type": "package_delivery"})
    await bus.publish({"event_id": "evt-3", "event_type": "driveway_vehicle"})

    assert seen == ["package_delivery", "driveway_vehicle"]


@pytest.mark.asyncio
async def test_event_bus_unsubscribe_stops_delivery():
    bus = EventBusService()
    seen: list[str] = []

    def handler(event: EventEnvelope) -> None:
        seen.append(event.event_id)

    bus.subscribe("doorbell", handler)
    bus.unsubscribe("doorbell", handler)
    await bus.publish({"event_id": "evt-4", "event_type": "doorbell"})

    assert seen == []


def test_event_bus_rejects_invalid_payload_shape():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {
                "event_id": "evt-invalid",
                "event_type": "doorbell",
                "confidence": 1.5,
            }
        )
