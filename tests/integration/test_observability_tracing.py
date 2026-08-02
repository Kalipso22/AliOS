import asyncio
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from alios_core.errors import ValidationError
from alios_observability import (
    LogException,
    RedactionPolicy,
    RedactionRule,
    SpanEvent,
    SpanKind,
    SpanLink,
    SpanRecord,
    SpanStatus,
    TraceContext,
    TraceSource,
    bind_trace_context,
    current_trace_context,
    require_trace_context,
)


def _error_record(secret: str = "TRACE-C1A3-SECRET-74c912") -> SpanRecord:
    now = datetime.now(UTC)
    context = TraceContext.create_root(baggage={"api_key": secret})
    linked_context = TraceContext.create_root(baggage={"api_key": secret})
    exception = LogException.from_exception(ValidationError("failure", {"api_key": secret}))
    return SpanRecord(
        context,
        f"api_key={secret}",
        TraceSource("integration"),
        SpanKind.INTERNAL,
        SpanStatus.ERROR,
        now,
        now,
        f"api_key={secret}",
        {"api_key": secret},
        (SpanEvent(f"api_key={secret}", now, {"api_key": secret}),),
        (SpanLink(linked_context, {"api_key": secret}),),
        exception,
    )


@pytest.mark.asyncio
async def test_nested_trace_context_lineage() -> None:
    root = TraceContext.create_root()
    with bind_trace_context(root):
        child = require_trace_context().create_child()
        async with bind_trace_context(child):
            assert require_trace_context().parent_span_id == root.span_id
    assert current_trace_context() is None


@pytest.mark.asyncio
async def test_concurrent_trace_contexts_are_isolated() -> None:
    first, second = TraceContext.create_root(), TraceContext.create_root()

    async def read(context: TraceContext) -> TraceContext:
        async with bind_trace_context(context):
            await asyncio.sleep(0)
            return require_trace_context()

    assert tuple(await asyncio.gather(read(first), read(second))) == (first, second)


@pytest.mark.asyncio
async def test_child_async_task_inherits_trace_context() -> None:
    context = TraceContext.create_root()
    async with bind_trace_context(context):
        assert (
            await asyncio.create_task(asyncio.sleep(0, result=require_trace_context())) == context
        )


@pytest.mark.asyncio
async def test_nested_binding_restores_root_context() -> None:
    root = TraceContext.create_root()
    child = root.create_child()
    async with bind_trace_context(root):
        async with bind_trace_context(child):
            assert require_trace_context() == child
        assert require_trace_context() == root


def test_span_record_with_nested_events_and_links_round_trip() -> None:
    context = TraceContext.create_root()
    now = datetime.now(UTC)
    record = SpanRecord(
        context,
        "run",
        TraceSource("core"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now,
        events=(SpanEvent("event", now, {"value": [1]}),),
        links=(SpanLink(context.create_child()),),
    )
    assert SpanRecord.from_dict(record.to_dict()) == record


def test_span_record_rendered_output_is_valid_json() -> None:
    import json

    now = datetime.now(UTC)
    record = SpanRecord(
        TraceContext.create_root(),
        "run",
        TraceSource("core"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now,
    )
    assert json.loads(json.dumps(record.to_dict()))["kind"] == "internal"


def test_trace_models_do_not_import_runtime() -> None:
    import alios_observability.tracing as tracing

    assert "alios_runtime" not in tracing.__dict__


def test_public_tracing_imports_have_no_global_context() -> None:
    assert current_trace_context() is None


@pytest.mark.asyncio
async def test_sibling_async_tasks_inherit_same_parent_snapshot() -> None:
    parent = TraceContext.create_root()

    async def read() -> TraceContext:
        return require_trace_context()

    async with bind_trace_context(parent):
        first, second = await asyncio.gather(
            asyncio.create_task(read()), asyncio.create_task(read())
        )
    assert first == parent and second == parent


def test_child_context_has_unique_span_id() -> None:
    parent = TraceContext.create_root()
    assert parent.create_child().span_id != parent.create_child().span_id


def test_multiple_children_share_trace_id() -> None:
    parent = TraceContext.create_root()
    assert parent.create_child().trace_id == parent.create_child().trace_id == parent.trace_id


@pytest.mark.asyncio
async def test_trace_binding_restores_after_child_failure() -> None:
    root = TraceContext.create_root()
    async with bind_trace_context(root):
        with pytest.raises(RuntimeError):
            async with bind_trace_context(root.create_child()):
                raise RuntimeError("failure")
        assert require_trace_context() == root


@pytest.mark.asyncio
async def test_trace_binding_restores_after_child_cancellation() -> None:
    root, started, release = TraceContext.create_root(), asyncio.Event(), asyncio.Event()

    async def worker() -> None:
        async with bind_trace_context(root.create_child()):
            started.set()
            await release.wait()

    async with bind_trace_context(root):
        task = asyncio.create_task(worker())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert require_trace_context() == root


@pytest.mark.asyncio
async def test_trace_context_round_trip_across_async_boundary() -> None:
    queue: asyncio.Queue[Mapping[str, object]] = asyncio.Queue()
    context = TraceContext.create_root(baggage={"region": "eu"})
    await queue.put(context.to_dict())
    assert TraceContext.from_dict(await queue.get()) == context


def test_span_record_with_alios_exception_round_trip() -> None:
    record = _error_record()
    unredacted = RedactionPolicy(include_default_rules=False)
    assert SpanRecord.from_dict(record.to_dict(unredacted)) == record


def test_span_record_with_ordinary_normalized_exception_is_safe() -> None:
    secret = "TRACE-C1A3-SECRET-74c912"
    exception = LogException.from_exception(ValueError(secret))
    now = datetime.now(UTC)
    record = SpanRecord(
        TraceContext.create_root(),
        "operation",
        TraceSource("integration"),
        SpanKind.INTERNAL,
        SpanStatus.ERROR,
        now,
        now,
        exception=exception,
    )
    assert exception.message == "An unexpected error occurred" and secret not in str(exception)
    assert SpanRecord.from_dict(record.to_dict()) == record


def test_span_record_rendering_redacts_all_sensitive_locations() -> None:
    secret = "TRACE-C1A3-SECRET-74c912"
    record = _error_record(secret)
    first = record.to_dict()
    second = record.to_dict()
    assert all(secret not in value for value in (str(first), repr(first), json.dumps(first)))
    assert record.context.baggage["api_key"] == secret
    first["name"] = "changed"
    assert second["name"] != "changed" and record.name == f"api_key={secret}"


def test_span_record_rendering_does_not_mutate_internal_record() -> None:
    record = _error_record()
    rendered = record.to_dict()
    attributes = rendered["attributes"]
    assert isinstance(attributes, dict)
    attributes["api_key"] = "changed"
    assert record.attributes["api_key"] == "TRACE-C1A3-SECRET-74c912"


def test_redacted_span_record_is_parseable() -> None:
    rendered = _error_record().to_dict()
    restored = SpanRecord.from_dict(rendered)
    assert restored.status is SpanStatus.ERROR and restored.name == "[REDACTED]"


def test_custom_redaction_policy_applies_to_span_record() -> None:
    secret = "TRACE-C1A3-SECRET-74c912"
    policy = RedactionPolicy(
        (RedactionRule("test-secret", value_patterns=(secret,), replacement="[MASKED]"),),
        include_default_rules=False,
    )
    rendered = _error_record(secret).to_dict(policy)
    assert "[MASKED]" in str(rendered) and secret not in str(rendered)


def test_default_redaction_policy_does_not_mutate_global_state() -> None:
    from alios_observability import default_redaction_policy

    policy = default_redaction_policy()
    original_rules = policy.rules
    _error_record().to_dict(RedactionPolicy(include_default_rules=False))
    assert default_redaction_policy() is policy and policy.rules == original_rules


@pytest.mark.asyncio
async def test_trace_models_do_not_create_global_tasks() -> None:
    import importlib

    current = asyncio.current_task()
    assert current is not None
    before = {task for task in asyncio.all_tasks() if task is not current and not task.done()}
    module = importlib.import_module("alios_observability.tracing")
    assert module is sys.modules["alios_observability.tracing"]
    await asyncio.sleep(0)
    after = {task for task in asyncio.all_tasks() if task is not current and not task.done()}
    assert after == before
    assert not any(isinstance(value, asyncio.Task) for value in vars(module).values())


def test_public_tracing_imports_preserve_class_identity() -> None:
    import alios_observability
    import alios_observability.tracing as tracing

    assert alios_observability.TraceContext is tracing.TraceContext
    assert alios_observability.SpanRecord is tracing.SpanRecord
    assert alios_observability.SpanEvent is tracing.SpanEvent
    assert alios_observability.SpanLink is tracing.SpanLink
    now = datetime.now(UTC)
    context = alios_observability.TraceContext.create_root()
    record = alios_observability.SpanRecord(
        context,
        "identity",
        alios_observability.TraceSource("integration"),
        alios_observability.SpanKind.INTERNAL,
        alios_observability.SpanStatus.OK,
        now,
        now,
    )
    assert isinstance(context, tracing.TraceContext) and isinstance(record, tracing.SpanRecord)


def test_public_tracing_imports_preserve_logging_exports() -> None:
    import alios_observability
    import alios_observability.logging as logging

    assert alios_observability.InMemoryLogSink is logging.InMemoryLogSink
    assert alios_observability.StructuredLogger is logging.StructuredLogger


def test_public_tracing_imports_preserve_metrics_exports() -> None:
    import alios_observability
    import alios_observability.metrics as metrics

    assert alios_observability.InMemoryMetricRegistry is metrics.InMemoryMetricRegistry
