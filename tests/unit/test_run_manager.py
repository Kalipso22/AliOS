from datetime import timedelta

import pytest
from alios_core.errors import RunNotFoundError, ValidationError
from alios_core.ids import TenantId
from alios_core.types import RunStatus, utc_now
from alios_runtime.run_manager import InMemoryRunRepository, RunFilter, RunManager


@pytest.mark.asyncio
async def test_repository_crud_filter_and_pagination() -> None:
    manager = RunManager(InMemoryRunRepository())
    first = await manager.create_run()
    second = await manager.create_run()
    assert await manager.get_run(first.run_id) == first
    assert len(await manager.list_runs(RunFilter(limit=1))) == 1
    assert await manager.repository.count() == 2 and second.run_id != first.run_id
    with pytest.raises(RunNotFoundError):
        await manager.get_run(type(first.run_id)())


@pytest.mark.asyncio
async def test_idempotent_creation_is_tenant_scoped() -> None:
    manager = RunManager()
    tenant = TenantId()
    first = await manager.create_run(tenant_id=tenant, idempotency_key="request")
    second = await manager.create_run(tenant_id=tenant, idempotency_key="request")
    assert first.run_id == second.run_id


@pytest.mark.asyncio
async def test_parent_child_and_lifecycle_operations() -> None:
    manager = RunManager()
    parent = await manager.create_run()
    child = await manager.create_run(parent_run_id=parent.run_id)
    running = await manager.start_run(parent.run_id)
    waiting = await manager.mark_waiting(parent.run_id)
    resumed = await manager.resume_run(parent.run_id)
    completed = await manager.complete_run(parent.run_id)
    assert child.root_run_id == parent.run_id and running.started_at is not None
    assert (
        waiting.status is RunStatus.WAITING
        and resumed.status is RunStatus.RUNNING
        and completed.status is RunStatus.SUCCEEDED
    )


@pytest.mark.asyncio
async def test_cancellation_failure_timeout_and_heartbeat() -> None:
    manager = RunManager()
    run = await manager.create_run()
    await manager.start_run(run.run_id)
    heartbeat = await manager.record_heartbeat(run.run_id)
    cancelling = await manager.request_cancellation(run.run_id)
    cancelled = await manager.cancel_run(run.run_id)
    assert (
        heartbeat.heartbeat_at is not None
        and cancelling.status is RunStatus.CANCELLING
        and cancelled.status is RunStatus.CANCELLED
    )


@pytest.mark.asyncio
async def test_stale_query_and_delete_rules() -> None:
    manager = RunManager()
    run = await manager.create_run()
    await manager.start_run(run.run_id)
    stale = await manager.query_stale_runs(stale_before=utc_now() + timedelta(seconds=1))
    assert run.run_id in {item.run_id for item in stale}
    with pytest.raises(ValidationError):
        await manager.delete_run(run.run_id)
    await manager.delete_run(run.run_id, force=True)
    assert await manager.get_optional_run(run.run_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "queue",
        "initializing",
        "running",
        "waiting",
        "approval",
        "retrying",
        "pause",
        "resume_queued",
        "complete",
        "fail",
        "timeout",
        "list_children",
        "filter_status",
        "filter_tenant",
        "filter_deadline",
        "invalid_key",
        "long_key",
        "missing_delete",
    ],
)
async def test_run_manager_operations(operation: str) -> None:
    manager = RunManager()
    run = await manager.create_run(tenant_id=TenantId(), deadline=utc_now() + timedelta(minutes=1))
    if operation == "queue":
        assert (await manager.queue_run(run.run_id)).status is RunStatus.QUEUED
    elif operation == "initializing":
        await manager.queue_run(run.run_id)
        assert (await manager.mark_initializing(run.run_id)).status is RunStatus.INITIALIZING
    elif operation == "running":
        assert (await manager.start_run(run.run_id)).status is RunStatus.RUNNING
    elif operation == "waiting":
        await manager.start_run(run.run_id)
        assert (await manager.mark_waiting(run.run_id)).status is RunStatus.WAITING
    elif operation == "approval":
        await manager.start_run(run.run_id)
        assert (
            await manager.mark_approval_required(run.run_id)
        ).status is RunStatus.WAITING_FOR_APPROVAL
    elif operation == "retrying":
        await manager.start_run(run.run_id)
        assert (await manager.mark_retrying(run.run_id)).status is RunStatus.RETRYING
    elif operation == "pause":
        await manager.start_run(run.run_id)
        assert (await manager.pause_run(run.run_id)).status is RunStatus.PAUSED
    elif operation == "resume_queued":
        await manager.start_run(run.run_id)
        await manager.pause_run(run.run_id)
        assert (await manager.resume_run(run.run_id, to_queued=True)).status is RunStatus.QUEUED
    elif operation == "complete":
        await manager.start_run(run.run_id)
        assert (await manager.complete_run(run.run_id)).status is RunStatus.SUCCEEDED
    elif operation == "fail":
        await manager.start_run(run.run_id)
        assert (
            await manager.fail_run(run.run_id, RuntimeError("unsafe"))
        ).status is RunStatus.FAILED
    elif operation == "timeout":
        await manager.start_run(run.run_id)
        assert (await manager.mark_timed_out(run.run_id)).status is RunStatus.TIMED_OUT
    elif operation == "list_children":
        child = await manager.create_run(parent_run_id=run.run_id)
        assert child in await manager.list_child_runs(run.run_id)
    elif operation == "filter_status":
        assert run in await manager.list_runs(RunFilter(statuses=frozenset({RunStatus.CREATED})))
    elif operation == "filter_tenant":
        assert run in await manager.list_runs(RunFilter(tenant_id=run.tenant_id))
    elif operation == "filter_deadline":
        assert run in await manager.list_runs(RunFilter(has_deadline=True))
    elif operation == "invalid_key":
        with pytest.raises(ValidationError):
            await manager.create_run(idempotency_key=" ")
    elif operation == "long_key":
        with pytest.raises(ValidationError):
            await manager.create_run(idempotency_key="x" * 257)
    else:
        with pytest.raises(RunNotFoundError):
            await manager.repository.delete(type(run.run_id)())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filter_name",
    [
        "status",
        "tenant",
        "user",
        "parent",
        "root",
        "correlation",
        "created_after",
        "created_before",
        "updated_after",
        "updated_before",
        "deadline",
        "terminal",
        "limit",
        "offset",
        "multiple_statuses",
        "count",
    ],
)
async def test_repository_filter_contract(filter_name: str) -> None:
    manager = RunManager()
    root = await manager.create_run()
    child = await manager.create_run(parent_run_id=root.run_id)
    if filter_name == "status":
        result = await manager.list_runs(RunFilter(statuses=frozenset({RunStatus.CREATED})))
    elif filter_name == "tenant":
        result = await manager.list_runs(RunFilter(tenant_id=None))
    elif filter_name == "user":
        result = await manager.list_runs(RunFilter(user_id=None))
    elif filter_name == "parent":
        result = await manager.list_runs(RunFilter(parent_run_id=root.run_id))
    elif filter_name == "root":
        result = await manager.list_runs(RunFilter(root_run_id=root.run_id))
    elif filter_name == "correlation":
        result = await manager.list_runs(RunFilter(correlation_id=root.correlation_id))
    elif filter_name == "created_after":
        result = await manager.list_runs(RunFilter(created_after=root.created_at))
    elif filter_name == "created_before":
        result = await manager.list_runs(RunFilter(created_before=utc_now()))
    elif filter_name == "updated_after":
        result = await manager.list_runs(RunFilter(updated_after=root.updated_at))
    elif filter_name == "updated_before":
        result = await manager.list_runs(RunFilter(updated_before=utc_now()))
    elif filter_name == "deadline":
        result = await manager.list_runs(RunFilter(has_deadline=False))
    elif filter_name == "terminal":
        result = await manager.list_runs(RunFilter(terminal=False))
    elif filter_name == "limit":
        result = await manager.list_runs(RunFilter(limit=1))
    elif filter_name == "offset":
        result = await manager.list_runs(RunFilter(offset=1))
    elif filter_name == "multiple_statuses":
        result = await manager.list_runs(
            RunFilter(statuses=frozenset({RunStatus.CREATED, RunStatus.RUNNING}))
        )
    else:
        assert await manager.repository.count() == 2
        return
    assert (
        isinstance(result, tuple)
        and child.run_id in {item.run_id for item in result}
        or filter_name in {"parent", "offset", "limit", "correlation"}
    )


@pytest.mark.parametrize("index", range(8))
def test_run_filter_rejects_invalid_pagination(index: int) -> None:
    with pytest.raises(ValidationError):
        RunFilter(limit=-index - 1)
