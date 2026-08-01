"""Typed event contracts and in-process event bus."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable runtime event."""

    name: str
    occurred_at: datetime


EventHandler = Callable[[Event], None]


class EventBus:
    """Small synchronous event bus for the first runtime vertical slice."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, name: str, handler: EventHandler) -> None:
        self._handlers[name].append(handler)

    def publish(self, name: str) -> Event:
        event = Event(name=name, occurred_at=datetime.now(UTC))
        for handler in self._handlers[name]:
            handler(event)
        return event
