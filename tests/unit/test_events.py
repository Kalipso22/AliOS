import asyncio
from dataclasses import FrozenInstanceError, replace

import pytest
from alios_core.events import (
    AsyncEventBus,
    BaseEvent,
    DispatchMode,
    Event,
    EventBus,
    RecoveryCompleted,
)
from alios_core.ids import CorrelationId
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
    event = RecoveryCompleted(
        correlation_id=CorrelationId(),
        run_id="run",
        recovery_id="recovery",
        mode="resume",
        success=True,
        checkpoint_id="checkpoint",
    )
    assert event.run_id == "run"
    assert event.recovery_id == "recovery"
    assert event.mode == "resume"
    assert event.success is True
    assert event.failure_code is None
    assert event.checkpoint_id == "checkpoint"
    assert event.correlation_id


def test_recovery_completed_failure_payload() -> None:
    event = RecoveryCompleted(
        correlation_id=CorrelationId(),
        run_id="run",
        recovery_id="recovery",
        mode="retry",
        success=False,
        failure_code="recovery_failure",
    )
    assert event.run_id == "run"
    assert event.recovery_id == "recovery"
    assert event.mode == "retry"
    assert event.success is False
    assert event.failure_code == "recovery_failure"
    assert event.checkpoint_id is None
    assert event.correlation_id


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(24))
async def test_event_history_filters(index: int) -> None:
    bus = AsyncEventBus(history_capacity=2)
    event = BaseEvent(source=f"source-{index}")
    await bus.dispatch(event)
    assert await bus.history(source=f"source-{index}")


def _completed_event(**overrides: object) -> RecoveryCompleted:
    values: dict[str, object] = {
        "correlation_id": CorrelationId(),
        "run_id": "run-1",
        "recovery_id": "recovery-1",
        "mode": "resume",
        "success": True,
        "failure_code": None,
        "checkpoint_id": "checkpoint-1",
        "metadata": {"safe": "value"},
    }
    values.update(overrides)
    return RecoveryCompleted(**values)  # type: ignore[arg-type]


def test_recovery_completed_timeout_payload() -> None:
    event = _completed_event(success=False, failure_code="recovery_timeout")
    assert event.run_id == "run-1"
    assert event.recovery_id == "recovery-1"
    assert event.mode == "resume"
    assert event.success is False
    assert event.failure_code == "recovery_timeout"
    assert event.checkpoint_id == "checkpoint-1"
    assert event.correlation_id


def test_recovery_completed_cancellation_payload() -> None:
    event = _completed_event(success=False, failure_code="recovery_cancelled")
    assert event.run_id == "run-1"
    assert event.recovery_id == "recovery-1"
    assert event.mode == "resume"
    assert event.success is False
    assert event.failure_code == "recovery_cancelled"
    assert event.checkpoint_id == "checkpoint-1"
    assert event.correlation_id


def test_recovery_completed_restart_has_no_checkpoint() -> None:
    event = _completed_event(mode="restart", checkpoint_id=None)
    assert event.run_id == "run-1"
    assert event.recovery_id == "recovery-1"
    assert event.mode == "restart"
    assert event.success is True
    assert event.failure_code is None
    assert event.checkpoint_id is None
    assert event.correlation_id


def test_recovery_completed_serialization_contains_base_fields() -> None:
    serialized = _completed_event().to_dict()
    for field in (
        "event_id",
        "correlation_id",
        "occurred_at",
        "source",
        "tenant_id",
        "user_id",
        "metadata",
        "priority",
    ):
        assert field in serialized


def test_recovery_completed_serialization_contains_required_fields() -> None:
    serialized = _completed_event().to_dict()
    assert serialized["run_id"] == "run-1"
    assert serialized["recovery_id"] == "recovery-1"
    assert serialized["mode"] == "resume"
    assert serialized["success"] is True
    assert serialized["failure_code"] is None
    assert serialized["checkpoint_id"] == "checkpoint-1"


def test_recovery_completed_serialization_preserves_correlation() -> None:
    event = _completed_event()
    assert event.to_dict()["correlation_id"] == str(event.correlation_id)


def test_recovery_completed_serialization_omits_runtime_payload() -> None:
    serialized = _completed_event().to_dict()
    for forbidden in (
        "recovered_state",
        "execution_context",
        "exception",
        "traceback",
        "restore",
        "restorer",
    ):
        assert forbidden not in serialized


def test_recovery_completed_serialization_returns_independent_dict() -> None:
    event = _completed_event()
    first = event.to_dict()
    second = event.to_dict()
    first["run_id"] = "changed"
    assert first is not second
    assert second["run_id"] == "run-1"
    assert event.run_id == "run-1"


def test_recovery_completed_serialized_metadata_is_independent() -> None:
    event = _completed_event(metadata={"safe": "value"})
    serialized = event.to_dict()
    metadata = serialized["metadata"]
    assert isinstance(metadata, dict)
    metadata["safe"] = "changed"
    assert event.metadata["safe"] == "value"


def test_recovery_completed_is_immutable() -> None:
    event = _completed_event()
    with pytest.raises(FrozenInstanceError):
        event.run_id = "changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_recovery_completed_direct_dispatch_preserves_fields() -> None:
    bus = AsyncEventBus()
    received: list[RecoveryCompleted] = []
    event = _completed_event()
    await bus.subscribe(RecoveryCompleted, received.append)
    result = await bus.dispatch(event)
    assert received == [event]
    assert result.event.run_id == event.run_id
    assert result.event.recovery_id == event.recovery_id
    assert result.event.mode == event.mode
    assert result.event.success == event.success
    assert result.event.failure_code == event.failure_code
    assert result.event.checkpoint_id == event.checkpoint_id
    assert result.event.correlation_id == event.correlation_id
    assert result.successful_handler_count == 1


@pytest.mark.asyncio
async def test_recovery_completed_queued_publish_preserves_fields() -> None:
    event = _completed_event()
    received: list[RecoveryCompleted] = []
    async with AsyncEventBus() as bus:
        await bus.subscribe(RecoveryCompleted, received.append)
        result = await bus.publish(event)
    assert received == [event]
    assert result.event.run_id == event.run_id
    assert result.event.recovery_id == event.recovery_id
    assert result.event.mode == event.mode
    assert result.event.success == event.success
    assert result.event.failure_code == event.failure_code
    assert result.event.checkpoint_id == event.checkpoint_id
    assert result.event.correlation_id == event.correlation_id


@pytest.mark.asyncio
async def test_recovery_completed_history_preserves_fields() -> None:
    bus = AsyncEventBus()
    event = _completed_event()
    await bus.dispatch(event)
    history = await bus.history(event_type=RecoveryCompleted)
    assert len(history) == 1
    stored = history[0].event
    assert isinstance(stored, RecoveryCompleted)
    assert stored.run_id == event.run_id
    assert stored.recovery_id == event.recovery_id
    assert stored.mode == event.mode
    assert stored.success == event.success
    assert stored.failure_code == event.failure_code
    assert stored.checkpoint_id == event.checkpoint_id
    assert stored.correlation_id == event.correlation_id


@pytest.mark.asyncio
async def test_recovery_completed_correlation_history_query() -> None:
    bus = AsyncEventBus()
    event = _completed_event()
    await bus.dispatch(event)
    assert len(await bus.history(correlation_id=event.correlation_id)) == 1
    assert not await bus.history(correlation_id=CorrelationId())


@pytest.mark.asyncio
async def test_recovery_completed_base_subscription_receives_event() -> None:
    bus = AsyncEventBus()
    received: list[BaseEvent] = []
    await bus.subscribe(BaseEvent, received.append)
    event = _completed_event()
    await bus.dispatch(event)
    assert received == [event]


@pytest.mark.asyncio
async def test_recovery_completed_exact_subscription_receives_event() -> None:
    bus = AsyncEventBus()
    received: list[RecoveryCompleted] = []
    await bus.subscribe(RecoveryCompleted, received.append)
    event = _completed_event()
    await bus.dispatch(event)
    assert received == [event]


@pytest.mark.asyncio
async def test_recovery_completed_multiple_subscribers_receive_same_event() -> None:
    bus = AsyncEventBus()
    order: list[str] = []
    event = _completed_event()
    await bus.subscribe(RecoveryCompleted, lambda _: order.append("exact"))
    await bus.subscribe(BaseEvent, lambda _: order.append("base"))
    await bus.dispatch(event)
    assert order == ["exact", "base"]


@pytest.mark.asyncio
async def test_recovery_completed_unsubscribed_handler_receives_no_future_event() -> None:
    bus = AsyncEventBus()
    received: list[RecoveryCompleted] = []
    subscription = await bus.subscribe(RecoveryCompleted, received.append)
    await bus.dispatch(_completed_event())
    await subscription.unsubscribe()
    await bus.dispatch(_completed_event())
    assert len(received) == 1
