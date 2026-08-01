import asyncio
from datetime import timedelta

import pytest
from alios_core.errors import ValidationError
from alios_core.ids import TaskId
from alios_core.types import ExecutionMode, utc_now
from alios_runtime.execution_context import (
    CancellationError,
    CancellationToken,
    ExecutionContext,
    bind_execution_context,
    current_execution_context,
    require_execution_context,
)


def test_context_defaults_are_typed_and_immutable() -> None:
    source = {"nested": {"number": 1}}
    context = ExecutionContext.create(metadata=source)
    source["nested"]["number"] = 2
    assert (
        context.run_id
        and context.correlation_id
        and context.execution_mode is ExecutionMode.INTERACTIVE
    )
    assert context.attempt_number == 1 and context.metadata["nested"]["number"] == 1


@pytest.mark.parametrize(
    "method", ["with_metadata", "merge_metadata", "with_policy_context", "with_trace_context"]
)
def test_context_mapping_updates_are_immutable(method: str) -> None:
    context = ExecutionContext.create(metadata={"a": {"x": 1}})
    if method == "with_metadata":
        updated = context.with_metadata({"b": 2})
    elif method == "merge_metadata":
        updated = context.merge_metadata({"a": {"y": 2}})
    elif method == "with_policy_context":
        updated = context.with_policy_context({"role": "admin"})
    else:
        updated = context.with_trace_context({"trace": "value"})
    assert updated is not context and context.metadata["a"]["x"] == 1


def test_context_child_and_retry_rules() -> None:
    context = ExecutionContext.create(metadata={"v": 1})
    child = context.create_child(current_task_id=TaskId())
    retry = context.create_retry(metadata={"r": 1})
    assert child.parent_run_id == context.run_id and child.correlation_id == context.correlation_id
    assert (
        child.run_id != context.run_id
        and retry.run_id == context.run_id
        and retry.attempt_number == 2
    )


@pytest.mark.asyncio
async def test_cancellation_token_is_idempotent_and_wakes_waiters() -> None:
    token = CancellationToken()
    waiters = [asyncio.create_task(token.wait()) for _ in range(2)]
    assert token.cancel("user_requested")
    assert not token.cancel("ignored")
    await asyncio.gather(*waiters)
    assert token.reason == "user_requested"
    with pytest.raises(CancellationError):
        token.raise_if_cancelled()


@pytest.mark.parametrize("delta,expired", [(None, False), (1, False), (-1, True)])
def test_deadline_calculations(delta: int | None, expired: bool) -> None:
    deadline = None if delta is None else utc_now() + timedelta(seconds=delta)
    context = ExecutionContext.create(deadline=deadline)
    assert context.is_expired() is expired
    remaining = context.remaining_time()
    if deadline is None:
        assert remaining is None
    else:
        assert remaining is not None and remaining >= timedelta()


def test_context_serialization_round_trip() -> None:
    context = ExecutionContext.create(
        metadata={"safe": "value"}, execution_mode=ExecutionMode.BACKGROUND
    )
    restored = ExecutionContext.from_dict(context.to_dict())
    assert restored.run_id == context.run_id and restored.execution_mode is ExecutionMode.BACKGROUND


@pytest.mark.asyncio
async def test_context_binding_is_nested_and_task_local() -> None:
    outer = ExecutionContext.create()
    inner = ExecutionContext.create()
    async with bind_execution_context(outer):
        assert require_execution_context() == outer
        async with bind_execution_context(inner):
            assert current_execution_context() == inner
        assert current_execution_context() == outer
        assert (
            await asyncio.create_task(asyncio.sleep(0, result=current_execution_context())) == outer
        )
    assert current_execution_context() is None


@pytest.mark.parametrize(
    "operation",
    [
        "no_deadline",
        "expired",
        "remaining",
        "negative_timeout",
        "naive_deadline",
        "missing_binding",
        "metadata_replace",
        "metadata_merge",
        "child_inherits_deadline",
        "child_inherits_identity",
        "retry_id",
        "retry_attempt",
    ],
)
def test_context_contract_details(operation: str) -> None:
    context = ExecutionContext.create(
        deadline=utc_now() + timedelta(seconds=1), metadata={"nested": {"a": 1}}
    )
    if operation == "no_deadline":
        assert ExecutionContext.create().remaining_time() is None
    elif operation == "expired":
        assert ExecutionContext.create(deadline=utc_now() - timedelta(seconds=1)).is_expired()
    elif operation == "remaining":
        remaining = context.remaining_time()
        assert remaining is not None and remaining >= timedelta()
    elif operation == "negative_timeout":
        with pytest.raises(ValidationError):
            context.effective_timeout(timedelta(seconds=-1))
    elif operation == "naive_deadline":
        with pytest.raises(ValidationError):
            ExecutionContext.create(deadline=utc_now().replace(tzinfo=None))
    elif operation == "missing_binding":
        with pytest.raises(ValidationError):
            require_execution_context()
    elif operation == "metadata_replace":
        assert "nested" not in context.with_metadata({"new": 1}).metadata
    elif operation == "metadata_merge":
        assert context.merge_metadata({"nested": {"b": 2}}).metadata["nested"]["b"] == 2
    elif operation == "child_inherits_deadline":
        assert context.create_child().deadline == context.deadline
    elif operation == "child_inherits_identity":
        assert context.create_child().tenant_id == context.tenant_id
    elif operation == "retry_id":
        assert context.create_retry().run_id == context.run_id
    else:
        assert context.create_retry().attempt == context.attempt + 1


@pytest.mark.parametrize("mode", list(ExecutionMode))
def test_context_execution_modes_round_trip(mode: ExecutionMode) -> None:
    context = ExecutionContext.create(execution_mode=mode)
    assert ExecutionContext.from_dict(context.to_dict()).execution_mode is mode


@pytest.mark.parametrize(
    "operation",
    [
        "deadline",
        "policy",
        "trace",
        "task",
        "mode",
        "tenant",
        "user",
        "child_shared",
        "child_fresh",
        "retry_fresh",
        "effective_timeout",
        "serialization_cancelled",
    ],
)
def test_execution_context_operations(operation: str) -> None:
    context = ExecutionContext.create(deadline=utc_now() + timedelta(seconds=10))
    if operation == "deadline":
        result = context.with_deadline(None)
        assert result.deadline is None
    elif operation == "policy":
        assert context.with_policy_context({"x": 1}).policy_context["x"] == 1
    elif operation == "trace":
        assert context.with_trace_context({"x": 1}).trace_context["x"] == 1
    elif operation == "task":
        assert context.with_current_task(TaskId()).current_task_id is not None
    elif operation == "mode":
        assert (
            context.with_execution_mode(ExecutionMode.BACKGROUND).mode is ExecutionMode.BACKGROUND
        )
    elif operation == "tenant":
        assert context.with_tenant(None).tenant_id is None
    elif operation == "user":
        assert context.with_user(None).user_id is None
    elif operation == "child_shared":
        assert (
            context.create_child(inherit_cancellation=True).cancellation_token
            is context.cancellation_token
        )
    elif operation == "child_fresh":
        assert context.create_child().cancellation_token is not context.cancellation_token
    elif operation == "retry_fresh":
        assert (
            context.create_retry(inherit_cancellation=False).cancellation_token
            is not context.cancellation_token
        )
    elif operation == "effective_timeout":
        timeout = context.effective_timeout(timedelta(seconds=20))
        assert timeout is not None and timeout <= timedelta(seconds=10)
    else:
        context.cancellation_token.cancel("safe")
        assert ExecutionContext.from_dict(context.to_dict()).cancellation_token.is_cancelled
