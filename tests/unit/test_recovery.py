import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta

import pytest
from alios_core.errors import (
    RecoveryCompletedPublicationError,
    RecoveryPlanStaleError,
    RecoveryStartedPublicationError,
)
from alios_core.events import BaseEvent, RecoveryCompleted, RecoveryStarted
from alios_core.ids import CheckpointId, CorrelationId, RunId
from alios_core.types import RunStatus
from alios_runtime.recovery import (
    Checkpoint,
    CheckpointFilter,
    CheckpointKind,
    CheckpointService,
    InMemoryCheckpointRepository,
    RecoveredPayload,
    RecoveryCoordinator,
    RecoveryMode,
    RecoveryPlan,
    RecoveryResult,
)
from alios_runtime.run_manager import RunManager


async def _recovery_fixture(
    mode: RecoveryMode = RecoveryMode.RESUME,
    publisher: Callable[[BaseEvent], Awaitable[object]] | None = None,
) -> tuple[RunManager, InMemoryCheckpointRepository, RecoveryCoordinator, Checkpoint, RecoveryPlan]:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    checkpoint = await repository.append(
        run_id=run.run_id,
        correlation_id=run.correlation_id,
        kind=CheckpointKind.PROGRESS,
        run_status=run.status,
        state={"value": 1},
    )
    coordinator = RecoveryCoordinator(repository, manager, publisher)
    plan = await coordinator.create_plan(run.run_id, mode=mode)
    return manager, repository, coordinator, checkpoint, plan


async def _stale(coordinator: RecoveryCoordinator, plan: RecoveryPlan) -> RecoveryPlanStaleError:
    with pytest.raises(RecoveryPlanStaleError) as raised:
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    return raised.value


def test_checkpoint_round_trip_is_immutable_and_deterministic() -> None:
    checkpoint = Checkpoint(state={"nested": {"value": 1}}, metadata={"safe": True})
    restored = Checkpoint.from_dict(checkpoint.to_dict())
    assert restored.checksum == checkpoint.checksum and restored.state["nested"]["value"] == 1


@pytest.mark.parametrize("kind", list(CheckpointKind))
def test_checkpoint_kinds_have_deterministic_checksum(kind: CheckpointKind) -> None:
    first = Checkpoint(kind=kind, state={"kind": kind.value})
    second = Checkpoint.from_dict(first.to_dict())
    assert first.checksum == second.checksum


@pytest.mark.asyncio
@pytest.mark.parametrize("count", range(1, 31))
async def test_atomic_checkpoint_versions_are_monotonic(count: int) -> None:
    repository = InMemoryCheckpointRepository()
    run_id = RunId()
    correlation_id = CorrelationId()
    checkpoints = await asyncio.gather(
        *[
            repository.append(
                run_id=run_id,
                correlation_id=correlation_id,
                kind=CheckpointKind.PROGRESS,
                run_status=RunStatus.RUNNING,
                state={"index": index},
            )
            for index in range(count)
        ]
    )
    assert sorted(item.version for item in checkpoints) == list(range(1, count + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(CheckpointKind))
async def test_repository_persists_every_checkpoint_kind(kind: CheckpointKind) -> None:
    repository = InMemoryCheckpointRepository()
    run_id = RunId()
    correlation_id = CorrelationId()
    checkpoint = await repository.append(
        run_id=run_id,
        correlation_id=correlation_id,
        kind=kind,
        run_status=RunStatus.RUNNING,
        state={},
    )
    assert await repository.get(checkpoint.checkpoint_id) == checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filter_name", ["run", "correlation", "kind", "status", "minimum", "maximum", "limit", "offset"]
)
async def test_checkpoint_filters(filter_name: str) -> None:
    repository = InMemoryCheckpointRepository()
    run_id = RunId()
    correlation_id = CorrelationId()
    for index in range(3):
        await repository.append(
            run_id=run_id,
            correlation_id=correlation_id,
            kind=CheckpointKind.PROGRESS,
            run_status=RunStatus.RUNNING,
            state={"index": index},
        )
    if filter_name == "run":
        filters = CheckpointFilter(run_id=run_id)
    elif filter_name == "correlation":
        filters = CheckpointFilter(correlation_id=correlation_id)
    elif filter_name == "kind":
        filters = CheckpointFilter(kinds=frozenset({CheckpointKind.PROGRESS}))
    elif filter_name == "status":
        filters = CheckpointFilter(statuses=frozenset({RunStatus.RUNNING}))
    elif filter_name == "minimum":
        filters = CheckpointFilter(minimum_version=2)
    elif filter_name == "maximum":
        filters = CheckpointFilter(maximum_version=2)
    elif filter_name == "limit":
        filters = CheckpointFilter(limit=1)
    else:
        filters = CheckpointFilter(offset=1)
    assert await repository.list(filters)


@pytest.mark.asyncio
async def test_checkpoint_service_and_recovery_resume() -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    service = CheckpointService(repository, manager)
    checkpoint = await service.create_checkpoint(run.run_id, state={"progress": 1})
    coordinator = RecoveryCoordinator(repository, manager)
    plan = await coordinator.create_plan(run.run_id, mode=RecoveryMode.RESUME)
    result = await coordinator.recover(
        plan, lambda item, _: RecoveredPayload(item.state if item else {})
    )
    assert checkpoint.checkpoint_id == plan.checkpoint_id and result.success


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(RecoveryMode))
@pytest.mark.parametrize("index", range(15))
async def test_recovery_modes_are_isolated(mode: RecoveryMode, index: int) -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    if mode is RecoveryMode.RESTART:
        coordinator = RecoveryCoordinator(repository, manager)
        plan = await coordinator.create_plan(run.run_id, mode=mode)
    else:
        await repository.append(
            run_id=run.run_id,
            correlation_id=run.correlation_id,
            kind=CheckpointKind.PROGRESS,
            run_status=RunStatus.CREATED,
            state={"index": index},
        )
        coordinator = RecoveryCoordinator(repository, manager)
        plan = await coordinator.create_plan(run.run_id, mode=mode)
    result = await coordinator.recover(
        plan,
        lambda checkpoint, _: RecoveredPayload(
            checkpoint.state if checkpoint else {"fresh": index}
        ),
    )
    assert result.success


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["failure", "bad input", "interrupted", "invalid", "retryable"])
async def test_recovery_normalizes_restorer_failures(message: str) -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    await repository.append(
        run_id=run.run_id,
        correlation_id=run.correlation_id,
        kind=CheckpointKind.PROGRESS,
        run_status=RunStatus.CREATED,
        state={},
    )
    coordinator = RecoveryCoordinator(repository, manager)
    plan = await coordinator.create_plan(run.run_id)

    def broken(*_: object) -> RecoveredPayload:
        raise RuntimeError(message)

    result = await coordinator.recover(plan, broken)
    assert not result.success and result.failure is not None and result.recovered_state is None


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_ms", range(1, 21))
async def test_recovery_timeout_is_safe(timeout_ms: int) -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    await repository.append(
        run_id=run.run_id,
        correlation_id=run.correlation_id,
        kind=CheckpointKind.PROGRESS,
        run_status=RunStatus.CREATED,
        state={},
    )
    coordinator = RecoveryCoordinator(
        repository, manager, default_timeout=timedelta(milliseconds=timeout_ms)
    )

    async def slow(*_: object) -> RecoveredPayload:
        await asyncio.Event().wait()
        return RecoveredPayload({})

    result = await coordinator.recover(await coordinator.create_plan(run.run_id), slow)
    assert (
        not result.success
        and result.failure is not None
        and result.failure.error_code == "recovery_timeout"
    )


@pytest.mark.asyncio
async def test_stale_plan_missing_checkpoint_releases_reservation() -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    checkpoint = await repository.append(
        run_id=run.run_id,
        correlation_id=run.correlation_id,
        kind=CheckpointKind.PROGRESS,
        run_status=RunStatus.CREATED,
        state={},
    )
    coordinator = RecoveryCoordinator(repository, manager)
    plan = await coordinator.create_plan(run.run_id, checkpoint_id=checkpoint.checkpoint_id)
    await repository.delete(checkpoint.checkpoint_id)
    with pytest.raises(RecoveryPlanStaleError):
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    await repository.append(
        run_id=run.run_id,
        correlation_id=run.correlation_id,
        kind=CheckpointKind.PROGRESS,
        run_status=RunStatus.CREATED,
        state={},
    )

    def restore(checkpoint: Checkpoint | None, _: object) -> RecoveredPayload:
        assert checkpoint is not None
        return RecoveredPayload(checkpoint.state)

    assert (await coordinator.recover(await coordinator.create_plan(run.run_id), restore)).success


@pytest.mark.asyncio
async def test_stale_plan_status_change_releases_reservation() -> None:
    manager, _, coordinator, _, plan = await _recovery_fixture()
    await manager.queue_run(plan.run_id)
    assert (await _stale(coordinator, plan)).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_stale_plan_missing_run_releases_reservation() -> None:
    manager, _, coordinator, _, plan = await _recovery_fixture()
    await manager.repository.delete(plan.run_id)
    assert (await _stale(coordinator, plan)).details["reason"] == "run_missing"


@pytest.mark.asyncio
async def test_stale_plan_checkpoint_version_change_releases_reservation() -> None:
    _, repository, coordinator, checkpoint, plan = await _recovery_fixture()
    object.__setattr__(checkpoint, "version", checkpoint.version + 1)
    assert (await _stale(coordinator, plan)).details["reason"] == "checkpoint_changed"


@pytest.mark.asyncio
async def test_stale_plan_correlation_change_releases_reservation() -> None:
    manager, _, coordinator, _, plan = await _recovery_fixture()
    run = await manager.get_run(plan.run_id)
    await manager.repository.update(replace(run, correlation_id=CorrelationId()))
    assert (await _stale(coordinator, plan)).details["reason"] == "run_changed"


@pytest.mark.asyncio
async def test_stale_plan_corrupt_checksum_releases_reservation() -> None:
    _, _, coordinator, checkpoint, plan = await _recovery_fixture()
    object.__setattr__(checkpoint, "checksum", "corrupt")
    assert (await _stale(coordinator, plan)).details["reason"] == "checkpoint_changed"


@pytest.mark.asyncio
async def test_valid_recovery_after_stale_plan_rejection() -> None:
    _, repository, coordinator, checkpoint, plan = await _recovery_fixture()
    await repository.delete(checkpoint.checkpoint_id)
    await _stale(coordinator, plan)
    replacement = await repository.append(
        run_id=plan.run_id,
        correlation_id=plan.correlation_id,
        kind=CheckpointKind.PROGRESS,
        run_status=plan.source_status,
        state={},
    )
    valid = await coordinator.create_plan(plan.run_id, checkpoint_id=replacement.checkpoint_id)
    assert (await coordinator.recover(valid, lambda item, _: RecoveredPayload(item.state))).success


@pytest.mark.asyncio
async def test_stale_plan_does_not_invoke_restorer() -> None:
    manager, _, coordinator, _, plan = await _recovery_fixture()
    await manager.queue_run(plan.run_id)
    invoked = False

    def restore(*_: object) -> RecoveredPayload:
        nonlocal invoked
        invoked = True
        return RecoveredPayload({})

    await _stale(coordinator, plan)
    assert not invoked


@pytest.mark.asyncio
async def test_stale_plan_does_not_publish_started() -> None:
    events: list[BaseEvent] = []

    async def publish(event: BaseEvent) -> object:
        events.append(event)
        return object()

    manager, _, coordinator, _, plan = await _recovery_fixture(publisher=publish)
    await manager.queue_run(plan.run_id)
    await _stale(coordinator, plan)
    assert not events


@pytest.mark.asyncio
async def test_stale_plan_does_not_publish_completed() -> None:
    events: list[BaseEvent] = []

    async def publish(event: BaseEvent) -> object:
        events.append(event)
        return object()

    manager, _, coordinator, _, plan = await _recovery_fixture(publisher=publish)
    await manager.queue_run(plan.run_id)
    await _stale(coordinator, plan)
    assert not any(isinstance(event, RecoveryCompleted) for event in events)


@pytest.mark.asyncio
async def test_stale_plan_creates_no_restoration_task() -> None:
    manager, _, coordinator, _, plan = await _recovery_fixture()
    await manager.queue_run(plan.run_id)
    current = set(asyncio.all_tasks())
    await _stale(coordinator, plan)
    assert set(asyncio.all_tasks()) == current


@pytest.mark.asyncio
async def test_resume_requires_checkpoint_id_at_revalidation() -> None:
    _, _, coordinator, _, plan = await _recovery_fixture()
    assert (
        await _stale(coordinator, replace(plan, checkpoint_id=None))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_resume_requires_checkpoint_version_at_revalidation() -> None:
    _, _, coordinator, _, plan = await _recovery_fixture()
    assert (
        await _stale(coordinator, replace(plan, checkpoint_version=None))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_resume_rejects_invalid_target_status() -> None:
    _, _, coordinator, _, plan = await _recovery_fixture()
    assert (
        await _stale(coordinator, replace(plan, target_status=RunStatus.QUEUED))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_retry_requires_checkpoint_id_at_revalidation() -> None:
    _, _, coordinator, _, plan = await _recovery_fixture(RecoveryMode.RETRY)
    assert (
        await _stale(coordinator, replace(plan, checkpoint_id=None))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_retry_requires_checkpoint_version_at_revalidation() -> None:
    _, _, coordinator, _, plan = await _recovery_fixture(RecoveryMode.RETRY)
    assert (
        await _stale(coordinator, replace(plan, checkpoint_version=None))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_retry_rejects_succeeded_source() -> None:
    manager, _, coordinator, _, plan = await _recovery_fixture(RecoveryMode.RETRY)
    run = await manager.get_run(plan.run_id)
    succeeded = replace(run, status=RunStatus.SUCCEEDED)
    await manager.repository.update(succeeded)
    stale = replace(plan, source_status=RunStatus.SUCCEEDED)
    assert (await _stale(coordinator, stale)).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_retry_rejects_invalid_target_status() -> None:
    _, _, coordinator, _, plan = await _recovery_fixture(RecoveryMode.RETRY)
    assert (
        await _stale(coordinator, replace(plan, target_status=RunStatus.RUNNING))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_restart_rejects_checkpoint_id() -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    coordinator = RecoveryCoordinator(repository, manager)
    plan = await coordinator.create_plan(run.run_id, mode=RecoveryMode.RESTART)
    assert (
        await _stale(coordinator, replace(plan, checkpoint_id=CheckpointId()))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_restart_rejects_checkpoint_version() -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    coordinator = RecoveryCoordinator(repository, manager)
    plan = await coordinator.create_plan(run.run_id, mode=RecoveryMode.RESTART)
    assert (
        await _stale(coordinator, replace(plan, checkpoint_version=1))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_restart_rejects_invalid_target_status() -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    coordinator = RecoveryCoordinator(repository, manager)
    plan = await coordinator.create_plan(run.run_id, mode=RecoveryMode.RESTART)
    assert (
        await _stale(coordinator, replace(plan, target_status=RunStatus.QUEUED))
    ).code == "recovery_plan_stale"


@pytest.mark.asyncio
async def test_restart_revalidation_does_not_load_checkpoint() -> None:
    manager = RunManager()
    run = await manager.create_run()
    repository = InMemoryCheckpointRepository()
    coordinator = RecoveryCoordinator(repository, manager)
    plan = await coordinator.create_plan(run.run_id, mode=RecoveryMode.RESTART)

    async def forbidden(_: object) -> Checkpoint:
        raise AssertionError("restart must not load a checkpoint")

    object.__setattr__(repository, "get", forbidden)
    assert (await coordinator.recover(plan, lambda item, _: RecoveredPayload({}))).success


def test_checkpoint_public_checksum_verification() -> None:
    assert Checkpoint(state={"value": 1}).verify_checksum()


def test_corrupted_checkpoint_fails_public_verification() -> None:
    checkpoint = Checkpoint(state={"value": 1})
    object.__setattr__(checkpoint, "checksum", "corrupt")
    assert not checkpoint.verify_checksum()


async def _started_failure() -> tuple[RecoveryCoordinator, RecoveryPlan, list[BaseEvent]]:
    events: list[BaseEvent] = []

    async def publish(event: BaseEvent) -> object:
        events.append(event)
        if isinstance(event, RecoveryStarted):
            raise ValueError("unsafe publisher failure")
        return object()

    _, _, coordinator, _, plan = await _recovery_fixture(publisher=publish)
    return coordinator, plan, events


@pytest.mark.asyncio
async def test_started_publication_failure_has_specific_error_code() -> None:
    coordinator, plan, _ = await _started_failure()
    with pytest.raises(RecoveryStartedPublicationError) as raised:
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert raised.value.code == "recovery_started_publication_failed"


@pytest.mark.asyncio
async def test_started_publication_failure_preserves_cause() -> None:
    coordinator, plan, _ = await _started_failure()
    with pytest.raises(RecoveryStartedPublicationError) as raised:
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_started_publication_failure_does_not_invoke_restorer() -> None:
    coordinator, plan, _ = await _started_failure()
    invoked = False

    def restore(*_: object) -> RecoveredPayload:
        nonlocal invoked
        invoked = True
        return RecoveredPayload({})

    with pytest.raises(RecoveryStartedPublicationError):
        await coordinator.recover(plan, restore)
    assert not invoked


@pytest.mark.asyncio
async def test_started_publication_failure_creates_no_restoration_task() -> None:
    coordinator, plan, _ = await _started_failure()
    before = set(asyncio.all_tasks())
    with pytest.raises(RecoveryStartedPublicationError):
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert set(asyncio.all_tasks()) == before


@pytest.mark.asyncio
async def test_started_publication_failure_emits_no_completed_event() -> None:
    coordinator, plan, events = await _started_failure()
    with pytest.raises(RecoveryStartedPublicationError):
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert not any(isinstance(event, RecoveryCompleted) for event in events)


@pytest.mark.asyncio
async def test_started_publication_failure_releases_reservation() -> None:
    coordinator, plan, _ = await _started_failure()
    with pytest.raises(RecoveryStartedPublicationError):
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert plan.run_id not in coordinator._active


async def _completed_failure(*, broken: bool = False) -> tuple[RecoveryCoordinator, RecoveryPlan]:
    async def publish(event: BaseEvent) -> object:
        if isinstance(event, RecoveryCompleted):
            raise ValueError("unsafe publisher failure")
        return object()

    _, _, coordinator, _, plan = await _recovery_fixture(publisher=publish)
    return coordinator, plan


@pytest.mark.asyncio
async def test_completed_publication_failure_has_specific_error_code() -> None:
    coordinator, plan = await _completed_failure()
    with pytest.raises(RecoveryCompletedPublicationError) as raised:
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert raised.value.code == "recovery_completed_publication_failed"


@pytest.mark.asyncio
async def test_completed_publication_failure_preserves_cause() -> None:
    coordinator, plan = await _completed_failure()
    with pytest.raises(RecoveryCompletedPublicationError) as raised:
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_completed_publication_failure_after_success() -> None:
    coordinator, plan = await _completed_failure()
    with pytest.raises(RecoveryCompletedPublicationError) as raised:
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert raised.value.details == {"success": True}


@pytest.mark.asyncio
async def test_completed_publication_failure_after_restorer_failure() -> None:
    coordinator, plan = await _completed_failure()

    def broken(*_: object) -> RecoveredPayload:
        raise RuntimeError("failure")

    with pytest.raises(RecoveryCompletedPublicationError) as raised:
        await coordinator.recover(plan, broken)
    assert raised.value.details == {"success": False}


@pytest.mark.asyncio
async def test_completed_publication_failure_releases_reservation() -> None:
    coordinator, plan = await _completed_failure()
    with pytest.raises(RecoveryCompletedPublicationError):
        await coordinator.recover(plan, lambda *_: RecoveredPayload({}))
    assert plan.run_id not in coordinator._active


async def _cancelled_recovery(
    *, fail_completed: bool = False, restart: bool = False
) -> tuple[
    asyncio.Task[RecoveryResult], list[BaseEvent], RecoveryCoordinator, RecoveryPlan, asyncio.Event
]:
    events: list[BaseEvent] = []
    entered = asyncio.Event()

    async def publish(event: BaseEvent) -> object:
        events.append(event)
        if fail_completed and isinstance(event, RecoveryCompleted):
            raise ValueError("unsafe publisher failure")
        return object()

    if restart:
        manager = RunManager()
        run = await manager.create_run()
        repository = InMemoryCheckpointRepository()
        coordinator = RecoveryCoordinator(repository, manager, publish)
        plan = await coordinator.create_plan(run.run_id, mode=RecoveryMode.RESTART)
    else:
        _, _, coordinator, _, plan = await _recovery_fixture(publisher=publish)

    async def restore(*_: object) -> RecoveredPayload:
        entered.set()
        await asyncio.Event().wait()
        return RecoveredPayload({})

    task = asyncio.create_task(coordinator.recover(plan, restore))
    await asyncio.wait_for(entered.wait(), 1)
    return task, events, coordinator, plan, entered


@pytest.mark.asyncio
async def test_cancelled_recovery_publishes_completed_event() -> None:
    task, events, _, _, _ = await _cancelled_recovery()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert isinstance(events[-1], RecoveryCompleted)


@pytest.mark.asyncio
async def test_cancelled_recovery_completed_event_has_failure_code() -> None:
    task, events, _, _, _ = await _cancelled_recovery()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        isinstance(events[-1], RecoveryCompleted)
        and events[-1].failure_code == "recovery_cancelled"
    )


@pytest.mark.asyncio
async def test_cancelled_recovery_completed_event_preserves_correlation() -> None:
    task, events, _, plan, _ = await _cancelled_recovery()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events[-1].correlation_id == plan.correlation_id


@pytest.mark.asyncio
async def test_cancelled_recovery_re_raises_cancelled_error() -> None:
    task, _, _, _, _ = await _cancelled_recovery()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancelled_recovery_cleans_restoration_task() -> None:
    task, _, _, _, _ = await _cancelled_recovery()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done() and task.cancelled()


@pytest.mark.asyncio
async def test_cancelled_recovery_releases_reservation() -> None:
    task, _, coordinator, plan, _ = await _cancelled_recovery()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plan.run_id not in coordinator._active


@pytest.mark.asyncio
async def test_cancelled_recovery_publication_failure_is_not_silently_swallowed() -> None:
    task, _, _, _, _ = await _cancelled_recovery(fail_completed=True)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert "recovery_completed_publication_failed" in getattr(raised.value, "__notes__", [])


@pytest.mark.asyncio
async def test_cancelled_restart_event_has_no_checkpoint_id() -> None:
    task, events, _, _, _ = await _cancelled_recovery(restart=True)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert isinstance(events[-1], RecoveryCompleted) and events[-1].checkpoint_id is None
