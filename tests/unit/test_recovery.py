import asyncio
from datetime import timedelta

import pytest
from alios_core.errors import RecoveryPlanStaleError
from alios_core.ids import CorrelationId, RunId
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
)
from alios_runtime.run_manager import RunManager


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
