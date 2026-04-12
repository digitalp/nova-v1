from __future__ import annotations

import inspect
from collections import defaultdict
from typing import Any, Awaitable, Callable

from avatar_backend.models.events import EventEnvelope


EventSubscriber = Callable[[EventEnvelope], Any] | Callable[[EventEnvelope], Awaitable[Any]]


class EventBusService:
    """Canonical in-process event pipeline for V2 services."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventSubscriber]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventSubscriber) -> None:
        normalized = (event_type or "").strip() or "*"
        self._subscribers[normalized].append(handler)

    def unsubscribe(self, event_type: str, handler: EventSubscriber) -> None:
        normalized = (event_type or "").strip() or "*"
        handlers = self._subscribers.get(normalized, [])
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            self._subscribers.pop(normalized, None)

    async def publish(self, event: EventEnvelope | dict[str, Any]) -> EventEnvelope:
        envelope = event if isinstance(event, EventEnvelope) else EventEnvelope.model_validate(event)
        handlers = list(self._subscribers.get(envelope.event_type, ()))
        handlers.extend(self._subscribers.get("*", ()))
        for handler in handlers:
            result = handler(envelope)
            if inspect.isawaitable(result):
                await result
        return envelope
