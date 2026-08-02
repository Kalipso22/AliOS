import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast

import pytest
from alios_core.errors import (
    RecoveryCompletedPublicationError,
    RecoveryPlanStaleError,
    RecoveryStartedPublicationError,
)
from alios_core.events import (
    AsyncEventBus,
    BaseEvent,
    CheckpointCreated,
    RecoveryCompleted,
    RecoveryStarted,
    RunCompleted,
    RunCreated,
    RunStateChanged,
)
from alios_core.types import RunOutcome, RunStatus
from alios_runtime import (
    Checkpoint,
    CheckpointService,
    ExecutionContext,
    InMemoryCheckpointRepository,
    RecoveredPayload,
    RecoveryCoordinator,
    RecoveryMode,
    RecoveryPlan,
    RecoveryResult,
    RunManager,
    RunRecord,
    Runtime,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", range(50))
async def test_runtime_orchestrates_independent_executions(value: int) -> None:
    runtime = Runtime()
    await runtime.initialize()
    await runtime.start()
    result = await runtime.execute(lambda _: value)
    await runtime.stop()
    await runtime.close()
    assert result.outcome is RunOutcome.SUCCEEDED and result.value == value


@asynccontextmanager
async def _runtime_stack() -> AsyncIterator[
    tuple[Runtime, AsyncEventBus, RunManager, CheckpointService]
]:
    async with AsyncEventBus(history_capacity=200) as bus:
        manager = RunManager(event_publisher=bus.publish)
        checkpoints = CheckpointService(InMemoryCheckpointRepository(), manager, bus.publish)
        runtime = Runtime(bus, manager, checkpoints)
        await runtime.initialize()
        await runtime.start()
        try:
            yield runtime, bus, manager, checkpoints
        finally:
            await runtime.stop()
            await runtime.close()


async def _planned_recovery(
    runtime: Runtime,
    manager: RunManager,
    checkpoints: CheckpointService,
    *,
    mode: RecoveryMode = RecoveryMode.RESUME,
    status: RunStatus = RunStatus.CREATED,
) -> tuple[RunRecord, Checkpoint, RecoveryPlan]:
    run = await manager.create_run()
    if status is RunStatus.FAILED:
        await manager.fail_run(run.run_id, RuntimeError("source failure"))
    elif status is RunStatus.QUEUED:
        await manager.queue_run(run.run_id)
    checkpoint = await checkpoints.create_checkpoint(run.run_id, state={"recovered": True})
    plan = await runtime.create_recovery_plan(run.run_id, mode=mode)
    return run, checkpoint, plan


def _payload(plan: RecoveryPlan) -> RecoveredPayload:
    return RecoveredPayload(
        {"recovered": True},
        ExecutionContext.create(run_id=plan.run_id, correlation_id=plan.correlation_id),
    )


async def _recovery_events(bus: AsyncEventBus) -> list[BaseEvent]:
    return [
        item.event
        for item in await bus.history()
        if isinstance(item.event, (RecoveryStarted, RecoveryCompleted))
    ]


@pytest.mark.asyncio
async def test_stale_recovery_plan_emits_no_recovery_events() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        run, checkpoint, plan = await _planned_recovery(runtime, manager, checkpoints)
        await checkpoints.repository.delete(checkpoint.checkpoint_id)
        invoked = False

        def restore(*_: object) -> RecoveredPayload:
            nonlocal invoked
            invoked = True
            return _payload(plan)

        with pytest.raises(RecoveryPlanStaleError):
            await runtime.recover(plan, restore)
        assert not invoked
        assert not await _recovery_events(bus)
        assert plan.run_id not in runtime.recovery._active
        assert run.run_id == plan.run_id


@pytest.mark.asyncio
async def test_recovery_can_retry_after_stale_plan_rejection() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, checkpoint, plan = await _planned_recovery(runtime, manager, checkpoints)
        await checkpoints.repository.delete(checkpoint.checkpoint_id)
        with pytest.raises(RecoveryPlanStaleError):
            await runtime.recover(plan, lambda *_: _payload(plan))
        replacement = await checkpoints.create_checkpoint(plan.run_id, state={"recovered": True})
        valid = await runtime.create_recovery_plan(plan.run_id)
        invoked = 0

        def restore(*_: object) -> RecoveredPayload:
            nonlocal invoked
            invoked += 1
            return _payload(valid)

        assert (await runtime.recover(valid, restore)).success
        events = await _recovery_events(bus)
        assert sum(isinstance(event, RecoveryStarted) for event in events) == 1
        completed = [event for event in events if isinstance(event, RecoveryCompleted)]
        assert len(completed) == 1 and completed[0].success
        assert invoked == 1 and valid.checkpoint_id == replacement.checkpoint_id


@pytest.mark.asyncio
async def test_recovery_success_event_precedes_execution_events() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)
        calls = 0

        def task(context: ExecutionContext) -> str:
            nonlocal calls
            calls += 1
            assert context.correlation_id == plan.correlation_id
            return "done"

        result = await runtime.recover_and_execute(plan, lambda *_: _payload(plan), task)
        history = [item.event for item in await bus.history()]
        started = next(
            index for index, event in enumerate(history) if isinstance(event, RecoveryStarted)
        )
        completed = next(
            index for index, event in enumerate(history) if isinstance(event, RecoveryCompleted)
        )
        execution = next(
            index
            for index, event in enumerate(history)
            if isinstance(event, RunStateChanged) and index > completed
        )
        terminal = next(
            index
            for index, event in enumerate(history)
            if isinstance(event, RunCompleted) and index > completed
        )
        assert started < completed < execution < terminal
        assert isinstance(result.recovery_result, RecoveryResult)
        assert result.recovery_result.success
        assert result.outcome is RunOutcome.SUCCEEDED and result.value == "done" and calls == 1


@pytest.mark.asyncio
async def test_recovery_failure_event_contains_failure_code() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)

        def broken(*_: object) -> RecoveredPayload:
            raise RuntimeError("secret failure")

        result = await runtime.recover(plan, broken)
        event = [
            item for item in await _recovery_events(bus) if isinstance(item, RecoveryCompleted)
        ][0]
        assert (
            not result.success
            and result.failure is not None
            and result.failure.error_code == "recovery_failure"
        )
        assert not event.success and event.failure_code == "recovery_failure"
        assert event.run_id == str(plan.run_id) and event.recovery_id == plan.recovery_id
        assert "secret failure" not in event.to_dict().values()


@pytest.mark.asyncio
async def test_recovery_timeout_event_contains_timeout_code() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)
        entered = asyncio.Event()

        async def blocked(*_: object) -> RecoveredPayload:
            entered.set()
            await asyncio.Event().wait()
            return _payload(plan)

        result = await asyncio.wait_for(
            runtime.recover(plan, blocked, timeout=timedelta(milliseconds=5)), 1
        )
        event = [
            item for item in await _recovery_events(bus) if isinstance(item, RecoveryCompleted)
        ][0]
        assert entered.is_set() and not result.success and result.failure is not None
        assert (
            event.failure_code == "recovery_timeout" and event.correlation_id == plan.correlation_id
        )
        assert plan.run_id not in runtime.recovery._active


@pytest.mark.asyncio
async def test_recovery_cancellation_event_contains_cancelled_code() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)
        entered = asyncio.Event()

        async def blocked(*_: object) -> RecoveredPayload:
            entered.set()
            await asyncio.Event().wait()
            return _payload(plan)

        operation = asyncio.create_task(runtime.recover(plan, blocked))
        await asyncio.wait_for(entered.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        event = [
            item for item in await _recovery_events(bus) if isinstance(item, RecoveryCompleted)
        ][0]
        assert (
            event.failure_code == "recovery_cancelled"
            and event.correlation_id == plan.correlation_id
        )
        assert event.recovery_id == plan.recovery_id and event.checkpoint_id == str(
            plan.checkpoint_id
        )
        assert plan.run_id not in runtime.recovery._active
        assert (
            await runtime.recover(
                await runtime.create_recovery_plan(plan.run_id), lambda *_: _payload(plan)
            )
        ).success


@pytest.mark.asyncio
async def test_restart_recovery_event_has_no_checkpoint_id() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        run, _, plan = await _planned_recovery(
            runtime, manager, checkpoints, mode=RecoveryMode.RESTART
        )
        received: list[Checkpoint | None] = []

        def restore(checkpoint: object, _: RecoveryPlan) -> RecoveredPayload:
            received.append(cast(Checkpoint | None, checkpoint))
            return _payload(plan)

        result = await runtime.recover_and_execute(
            plan, restore, lambda context: context.attempt_number
        )
        event = [
            item for item in await _recovery_events(bus) if isinstance(item, RecoveryCompleted)
        ][0]
        assert plan.checkpoint_id is None and received == [None]
        assert (
            event.mode == "restart"
            and event.success
            and event.failure_code is None
            and event.checkpoint_id is None
        )
        assert result.run.run_id != run.run_id and result.run.parent_run_id is None
        assert result.run.correlation_id == plan.correlation_id and result.value == 1
        assert (await manager.get_run(run.run_id)).status is RunStatus.CREATED


@pytest.mark.asyncio
async def test_retry_recovery_creates_fresh_child_run() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        run, _, plan = await _planned_recovery(
            runtime, manager, checkpoints, mode=RecoveryMode.RETRY
        )
        result = await runtime.recover_and_execute(
            plan, lambda *_: _payload(plan), lambda context: context.attempt_number
        )
        created = [
            item.event
            for item in await bus.history(event_type=RunCreated)
            if isinstance(item.event, RunCreated) and item.event.run_id == str(result.run.run_id)
        ]
        terminal = [
            item.event
            for item in await bus.history(event_type=RunCompleted)
            if isinstance(item.event, RunCompleted) and item.event.run_id == str(result.run.run_id)
        ]
        assert result.run.run_id != run.run_id and result.run.parent_run_id == run.run_id
        assert result.run.correlation_id == run.correlation_id and result.value == 2
        assert (await manager.get_run(run.run_id)).status is RunStatus.CREATED
        assert isinstance(result.recovery_result, RecoveryResult)
        assert result.recovery_result.success
        assert len(created) == 1 and len(terminal) == 1


@pytest.mark.asyncio
async def test_resume_recovery_uses_existing_executable_run() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        run, _, plan = await _planned_recovery(runtime, manager, checkpoints)
        initial_created = len(await bus.history(event_type=RunCreated))
        result = await runtime.recover_and_execute(plan, lambda *_: _payload(plan), lambda _: "ok")
        assert result.run.run_id == run.run_id and result.run.correlation_id == run.correlation_id
        assert result.outcome is RunOutcome.SUCCEEDED
        assert len(await bus.history(event_type=RunCreated)) == initial_created
        assert (await manager.get_run(run.run_id)).status is RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_resume_recovery_creates_continuation_for_non_executable_source() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        run, _, plan = await _planned_recovery(
            runtime, manager, checkpoints, status=RunStatus.FAILED
        )
        result = await runtime.recover_and_execute(
            plan, lambda *_: _payload(plan), lambda _: "continued"
        )
        assert result.run.run_id != run.run_id and result.run.parent_run_id == run.run_id
        assert result.run.correlation_id == run.correlation_id and result.value == "continued"
        assert (await manager.get_run(run.run_id)).status is RunStatus.FAILED
        assert len(await bus.history(event_type=RunCreated)) == 2


@pytest.mark.asyncio
async def test_started_publication_failure_blocks_restoration() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)

        async def publish(event: BaseEvent) -> object:
            if isinstance(event, RecoveryStarted):
                raise ValueError("publisher failure")
            return await bus.publish(event)

        runtime.recovery = RecoveryCoordinator(checkpoints.repository, manager, publish)
        invoked = False

        def restore(*_: object) -> RecoveredPayload:
            nonlocal invoked
            invoked = True
            return _payload(plan)

        with pytest.raises(RecoveryStartedPublicationError) as raised:
            await runtime.recover(plan, restore)
        assert raised.value.code == "recovery_started_publication_failed" and isinstance(
            raised.value.__cause__, ValueError
        )
        assert (
            not invoked
            and not await _recovery_events(bus)
            and plan.run_id not in runtime.recovery._active
        )


@pytest.mark.asyncio
async def test_completed_publication_failure_surfaces_infrastructure_error() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)
        calls = 0

        async def publish(event: BaseEvent) -> object:
            if isinstance(event, RecoveryCompleted):
                raise ValueError("publisher failure")
            return await bus.publish(event)

        runtime.recovery = RecoveryCoordinator(checkpoints.repository, manager, publish)

        def restore(*_: object) -> RecoveredPayload:
            nonlocal calls
            calls += 1
            return _payload(plan)

        with pytest.raises(RecoveryCompletedPublicationError) as raised:
            await runtime.recover(plan, restore)
        assert raised.value.code == "recovery_completed_publication_failed" and isinstance(
            raised.value.__cause__, ValueError
        )
        assert calls == 1 and plan.run_id not in runtime.recovery._active


@pytest.mark.asyncio
async def test_completed_publication_failure_after_restorer_failure() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)

        async def publish(event: BaseEvent) -> object:
            if isinstance(event, RecoveryCompleted):
                raise ValueError("publisher failure")
            return await bus.publish(event)

        runtime.recovery = RecoveryCoordinator(checkpoints.repository, manager, publish)

        def broken(*_: object) -> RecoveredPayload:
            raise RuntimeError("restore failure")

        with pytest.raises(RecoveryCompletedPublicationError) as raised:
            await runtime.recover(plan, broken)
        assert (
            raised.value.code == "recovery_completed_publication_failed"
            and raised.value.details == {"success": False}
        )
        assert (
            isinstance(raised.value.__cause__, ValueError)
            and plan.run_id not in runtime.recovery._active
        )


@pytest.mark.asyncio
async def test_correlation_spans_recovery_checkpoint_run_and_execution_events() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints, mode=RecoveryMode.RETRY)
        await runtime.recover_and_execute(plan, lambda *_: _payload(plan), lambda _: "ok")
        types = (
            CheckpointCreated,
            RecoveryStarted,
            RecoveryCompleted,
            RunCreated,
            RunStateChanged,
            RunCompleted,
        )
        events = [item.event for item in await bus.history() if isinstance(item.event, types)]
        assert events and all(event.correlation_id == plan.correlation_id for event in events)


@pytest.mark.asyncio
async def test_recovery_completed_history_serialization_is_safe() -> None:
    async with _runtime_stack() as (runtime, bus, manager, checkpoints):
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)
        await runtime.recover(plan, lambda *_: _payload(plan))
        event = (await bus.history(event_type=RecoveryCompleted))[0].event
        serialized = event.to_dict()
        for field in (
            "run_id",
            "recovery_id",
            "mode",
            "success",
            "failure_code",
            "checkpoint_id",
            "correlation_id",
        ):
            assert field in serialized
        for field in (
            "recovered_state",
            "execution_context",
            "exception",
            "traceback",
            "restore",
            "restorer",
        ):
            assert field not in serialized


@pytest.mark.asyncio
async def test_runtime_shutdown_after_recovery_leaves_no_pending_tasks() -> None:
    bus = AsyncEventBus()
    await bus.start()
    manager = RunManager(event_publisher=bus.publish)
    checkpoints = CheckpointService(InMemoryCheckpointRepository(), manager, bus.publish)
    runtime = Runtime(bus, manager, checkpoints)
    await runtime.initialize()
    await runtime.start()
    try:
        _, _, plan = await _planned_recovery(runtime, manager, checkpoints)
        await runtime.recover_and_execute(plan, lambda *_: _payload(plan), lambda _: "done")
        await runtime.stop()
        await runtime.close()
        await runtime.stop()
        await runtime.close()
        assert await runtime.active_count() == 0 and not runtime.recovery._active
        assert bus._started and not bus._stopped
    finally:
        await bus.stop()
