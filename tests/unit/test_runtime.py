import asyncio

import pytest
from alios_core.ids import RunId
from alios_core.policy import (
    InMemoryPolicyEvaluator,
    PolicyAction,
    PolicyContext,
    PolicyResource,
    PolicyRule,
    PolicySubject,
)
from alios_core.types import PolicyDecision, RunOutcome
from alios_runtime import ExecutionContext, Runtime, RuntimeExecutionResult, RuntimePolicyRequest


@pytest.mark.asyncio
async def test_runtime_executes_async_task() -> None:
    runtime = Runtime()
    await runtime.start()
    result = await runtime.execute(lambda _: "done")
    await runtime.stop()
    await runtime.close()
    assert result.outcome is RunOutcome.SUCCEEDED and result.value == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effect,called", [(PolicyDecision.ALLOW, True), (PolicyDecision.DENY, False)]
)
async def test_runtime_policy_controls_execution(effect: PolicyDecision, called: bool) -> None:
    evaluator = InMemoryPolicyEvaluator((PolicyRule("policy", "policy", effect),))
    runtime = Runtime(policy_evaluator=evaluator)
    await runtime.start()
    invoked = False

    def task(_: ExecutionContext) -> str:
        nonlocal invoked
        invoked = True
        return "ok"

    request = RuntimePolicyRequest(
        PolicySubject("user", "u"),
        PolicyResource("run", "r"),
        PolicyAction("execute"),
        PolicyContext(),
    )
    result = await runtime.execute(task, policy_request=request)
    await runtime.stop()
    await runtime.close()
    assert invoked is called
    assert result.outcome is (
        RunOutcome.SUCCEEDED if effect is PolicyDecision.ALLOW else RunOutcome.FAILED
    )


@pytest.mark.asyncio
async def test_runtime_external_cancellation_returns_cancelled_result() -> None:
    gate = asyncio.Event()
    runtime = Runtime()
    await runtime.start()

    async def task(_: ExecutionContext) -> str:
        await gate.wait()
        return "late"

    pending: asyncio.Task[RuntimeExecutionResult[str]] = asyncio.create_task(runtime.execute(task))
    while await runtime.active_count() == 0:
        await asyncio.sleep(0)
    run_id = next(iter(runtime._active))
    assert isinstance(run_id, RunId)
    cancelled = await runtime.cancel_run(run_id)
    result = await pending
    await runtime.stop()
    await runtime.close()
    assert cancelled.outcome is RunOutcome.CANCELLED and result.outcome is RunOutcome.CANCELLED


@pytest.mark.asyncio
async def test_execute_existing_does_not_duplicate_run() -> None:
    runtime = Runtime()
    await runtime.start()
    run = await runtime.run_manager.create_run()
    result = await runtime.execute_existing(run.run_id, lambda _: "existing")
    await runtime.stop()
    await runtime.close()
    assert result.run.run_id == run.run_id and await runtime.run_manager.repository.count() == 1
