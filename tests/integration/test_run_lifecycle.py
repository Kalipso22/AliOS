import pytest
from alios_core.events import AsyncEventBus, RunCompleted, RunCreated
from alios_core.types import RunStatus, utc_now
from alios_runtime import RunManager


@pytest.mark.asyncio
async def test_run_lifecycle_persists_and_publishes_events() -> None:
    async with AsyncEventBus() as bus:
        manager = RunManager(event_publisher=bus.publish)
        run = await manager.create_run()
        await manager.start_run(run.run_id)
        await manager.mark_waiting(run.run_id)
        await manager.resume_run(run.run_id)
        completed = await manager.complete_run(run.run_id)
        history = await bus.history()
    assert completed.status is RunStatus.SUCCEEDED
    assert any(isinstance(item.event, RunCreated) for item in history)
    assert any(isinstance(item.event, RunCompleted) for item in history)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flow",
    [
        "complete",
        "cancel",
        "fail",
        "timeout",
        "queue",
        "parent_child",
        "correlation",
        "history",
        "idempotent",
        "heartbeat",
        "exports",
        "approval",
        "retry",
        "pause",
        "stale",
    ],
)
async def test_run_lifecycle_integration_flows(flow: str) -> None:
    async with AsyncEventBus() as bus:
        manager = RunManager(event_publisher=bus.publish)
        run = await manager.create_run(idempotency_key="shared" if flow == "idempotent" else None)
        if flow == "complete":
            await manager.start_run(run.run_id)
            result = await manager.complete_run(run.run_id)
            assert result.status is RunStatus.SUCCEEDED
        elif flow == "cancel":
            await manager.start_run(run.run_id)
            await manager.request_cancellation(run.run_id)
            assert (await manager.cancel_run(run.run_id)).status is RunStatus.CANCELLED
        elif flow == "fail":
            await manager.start_run(run.run_id)
            assert (
                await manager.fail_run(run.run_id, RuntimeError("x"))
            ).status is RunStatus.FAILED
        elif flow == "timeout":
            await manager.queue_run(run.run_id)
            await manager.mark_initializing(run.run_id)
            await manager.mark_running(run.run_id)
            assert (await manager.mark_timed_out(run.run_id)).status is RunStatus.TIMED_OUT
        elif flow == "queue":
            assert (await manager.queue_run(run.run_id)).status is RunStatus.QUEUED
        elif flow == "parent_child":
            assert (await manager.create_run(parent_run_id=run.run_id)).parent_run_id == run.run_id
        elif flow == "correlation":
            assert (await manager.get_run(run.run_id)).correlation_id == run.correlation_id
        elif flow == "history":
            assert await bus.history()
        elif flow == "idempotent":
            assert (await manager.create_run(idempotency_key="shared")).run_id == run.run_id
        elif flow == "heartbeat":
            await manager.start_run(run.run_id)
            assert (await manager.record_heartbeat(run.run_id)).heartbeat_at is not None
        elif flow == "exports":
            assert RunManager.__name__ == "RunManager"
        elif flow == "approval":
            await manager.start_run(run.run_id)
            assert (
                await manager.mark_approval_required(run.run_id)
            ).status is RunStatus.WAITING_FOR_APPROVAL
        elif flow == "retry":
            await manager.start_run(run.run_id)
            assert (await manager.mark_retrying(run.run_id)).status is RunStatus.RETRYING
        elif flow == "pause":
            await manager.start_run(run.run_id)
            assert (await manager.pause_run(run.run_id)).status is RunStatus.PAUSED
        else:
            await manager.start_run(run.run_id)
            assert await manager.query_stale_runs(stale_before=utc_now())
