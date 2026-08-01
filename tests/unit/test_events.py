import asyncio
from dataclasses import replace

import pytest
from alios_core.events import AsyncEventBus, BaseEvent, DispatchMode, Event, EventBus
from alios_core.types import EventPriority


def test_legacy_event_bus_delivers_subscribed_events() -> None:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe("runtime.started", received.append)
    event = bus.publish("runtime.started")
    assert received == [event]


@pytest.mark.asyncio
async def test_base_event_and_direct_dispatch() -> None:
    event = BaseEvent(metadata={"key": "value"})
    bus = AsyncEventBus()
    received: list[BaseEvent] = []
    await bus.subscribe(BaseEvent, received.append)
    result = await bus.dispatch(event)
    assert received == [event] and result.successful_handler_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", list(EventPriority))
async def test_event_priorities_dispatch(priority: EventPriority) -> None:
    async with AsyncEventBus() as bus:
        result = await bus.publish(BaseEvent(priority=priority))
    assert result.event.priority is priority


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(DispatchMode))
async def test_dispatch_modes(mode: DispatchMode) -> None:
    seen: list[int] = []
    bus = AsyncEventBus(default_dispatch_mode=mode)
    await bus.subscribe(BaseEvent, lambda _: seen.append(1))
    await bus.subscribe(BaseEvent, lambda _: seen.append(2))
    await bus.dispatch(BaseEvent())
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_async_handler_timeout_and_failure_isolation() -> None:
    async def slow(_: BaseEvent) -> None:
        await asyncio.sleep(0.02)

    bus = AsyncEventBus(default_handler_timeout=0.001)
    await bus.subscribe(BaseEvent, slow)
    await bus.subscribe(BaseEvent, lambda _: None)
    result = await bus.dispatch(BaseEvent())
    assert result.failed_handler_count == 1 and result.successful_handler_count == 1


@pytest.mark.asyncio
async def test_middleware_transforms() -> None:
    bus = AsyncEventBus()
    bus.add_middleware(lambda event: replace(event, source="middleware"))
    assert (await bus.dispatch(BaseEvent())).event.source == "middleware"


def test_recovery_completed_success_payload() -> None:
    from alios_core.events import RecoveryCompleted

    event = RecoveryCompleted(
        run_id="run",
        recovery_id="recovery",
        mode="resume",
        success=True,
        checkpoint_id="checkpoint",
    )
    assert event.run_id == "run" and event.recovery_id == "recovery" and event.failure_code is None


def test_recovery_completed_failure_payload() -> None:
    from alios_core.events import RecoveryCompleted

    event = RecoveryCompleted(
        run_id="run",
        recovery_id="recovery",
        mode="retry",
        success=False,
        failure_code="recovery_failure",
    )
    assert not event.success and event.failure_code == "recovery_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(24))
async def test_event_history_filters(index: int) -> None:
    bus = AsyncEventBus(history_capacity=2)
    event = BaseEvent(source=f"source-{index}")
    await bus.dispatch(event)
    assert await bus.history(source=f"source-{index}")
