import asyncio
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from alios_core import TraceId
from alios_core.errors import SpanCompletionError, TracerClosedError, ValidationError
from alios_observability import (
    AlwaysOffSampler,
    AlwaysOnSampler,
    AlwaysRecordSampler,
    DefaultTracer,
    LogException,
    ParentBasedSampler,
    RedactionPolicy,
    RedactionRule,
    SpanEvent,
    SpanKind,
    SpanLink,
    SpanRecord,
    SpanStatus,
    TraceContext,
    TraceIdRatioSampler,
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


# Active tracer integration ------------------------------------------------


@pytest.mark.asyncio
async def test_tracer_root_span_lifecycle() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    span = await tracer.start_span("root", root=True)
    record = await span.end()
    assert record is not None and record.context.parent_span_id is None


@pytest.mark.asyncio
async def test_tracer_parent_child_lineage() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    parent = await tracer.start_span("parent")
    child = await tracer.start_span("child", parent=parent.context)
    assert child.context.trace_id == parent.context.trace_id
    assert child.context.parent_span_id == parent.context.span_id


@pytest.mark.asyncio
async def test_tracer_nested_current_span_scopes() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    async with tracer.start_as_current_span("parent") as parent:
        async with tracer.start_as_current_span("child") as child:
            assert child.context.parent_span_id == parent.context.span_id
        assert current_trace_context() == parent.context
    assert current_trace_context() is None


@pytest.mark.asyncio
async def test_tracer_sibling_spans_share_trace_id() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    parent = await tracer.start_span("parent")
    first = await tracer.start_span("first", parent=parent.context)
    second = await tracer.start_span("second", parent=parent.context)
    assert first.context.trace_id == second.context.trace_id == parent.context.trace_id


@pytest.mark.asyncio
async def test_tracer_concurrent_root_spans_are_isolated() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    first, second = await asyncio.gather(tracer.start_span("first"), tracer.start_span("second"))
    assert first.context.trace_id != second.context.trace_id


@pytest.mark.asyncio
async def test_tracer_concurrent_child_spans_share_parent() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    parent = await tracer.start_span("parent")
    first, second = await asyncio.gather(
        tracer.start_span("first", parent=parent.context),
        tracer.start_span("second", parent=parent.context),
    )
    assert first.context.parent_span_id == second.context.parent_span_id == parent.context.span_id


@pytest.mark.asyncio
async def test_tracer_explicit_parent_across_async_boundary() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    parent = await tracer.start_span("parent")
    queue: asyncio.Queue[TraceContext] = asyncio.Queue()
    await queue.put(parent.context)
    child = await tracer.start_span("child", parent=await queue.get())
    assert child.context.parent_span_id == parent.context.span_id


@pytest.mark.asyncio
async def test_always_on_scope_produces_record() -> None:
    tracer = DefaultTracer(TraceSource("integration"), sampler=AlwaysOnSampler())
    async with tracer.start_as_current_span("record"):
        pass
    assert (await tracer.status()).completed_count == 1


@pytest.mark.asyncio
async def test_always_off_scope_produces_no_record() -> None:
    tracer = DefaultTracer(TraceSource("integration"), sampler=AlwaysOffSampler())
    async with tracer.start_as_current_span("drop") as span:
        assert not span.is_recording
    status = await tracer.status()
    assert status.dropped_count == 1 and status.completed_count == 0


@pytest.mark.asyncio
async def test_record_only_scope_produces_unsampled_record() -> None:
    tracer = DefaultTracer(TraceSource("integration"), sampler=AlwaysRecordSampler())
    async with tracer.start_as_current_span("record") as span:
        assert span.is_recording and not span.context.sampled
    assert (await tracer.status()).completed_count == 1


@pytest.mark.asyncio
async def test_ratio_sampler_is_stable_across_tracers() -> None:
    identifier = TraceId()
    first = await DefaultTracer(TraceSource("first"), sampler=TraceIdRatioSampler(0.5)).start_span(
        "work", trace_id=identifier
    )
    second = await DefaultTracer(
        TraceSource("second"), sampler=TraceIdRatioSampler(0.5)
    ).start_span("work", trace_id=identifier)
    assert first.is_recording == second.is_recording


@pytest.mark.asyncio
async def test_span_attributes_events_links_and_exception_complete_record() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    span = await tracer.start_span("work", attributes={"region": "eu"})
    await span.add_event("event", attributes={"phase": 1})
    await span.add_link(SpanLink(TraceContext.create_root()))
    await span.record_exception(ValueError("ordinary"))
    record = await span.end()
    assert record is not None and record.events and record.links and record.exception is not None


@pytest.mark.asyncio
async def test_span_completion_record_round_trip() -> None:
    span = await DefaultTracer(TraceSource("integration")).start_span("work")
    record = await span.end()
    assert record is not None and SpanRecord.from_dict(record.to_dict()) == record


@pytest.mark.asyncio
async def test_completion_handler_receives_immutable_record() -> None:
    received: list[SpanRecord] = []

    class Handler:
        async def on_end(self, record: SpanRecord) -> None:
            received.append(record)

    span = await DefaultTracer(TraceSource("integration"), end_handler=Handler()).start_span("work")
    await span.end()
    with pytest.raises(TypeError):
        cast(dict[str, object], received[0].attributes)["region"] = "eu"


@pytest.mark.asyncio
async def test_completion_handler_failure_does_not_reopen_span() -> None:
    class Handler:
        async def on_end(self, record: SpanRecord) -> None:
            raise RuntimeError("handler")

    span = await DefaultTracer(TraceSource("integration"), end_handler=Handler()).start_span("work")
    with pytest.raises(SpanCompletionError):
        await span.end()
    assert span.is_ended


@pytest.mark.asyncio
async def test_concurrent_span_end_delivers_one_record() -> None:
    records: list[SpanRecord] = []

    class Handler:
        async def on_end(self, record: SpanRecord) -> None:
            records.append(record)

    span = await DefaultTracer(TraceSource("integration"), end_handler=Handler()).start_span("work")
    await asyncio.gather(span.end(), span.end())
    assert len(records) == 1


@pytest.mark.asyncio
async def test_completion_after_tracer_close_updates_status() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    span = await tracer.start_span("work")
    await tracer.close()
    await span.end()
    assert (await tracer.status()).completed_count == 1


@pytest.mark.asyncio
async def test_scope_normal_exit_sets_ok_record() -> None:
    received: list[SpanRecord] = []

    class Handler:
        async def on_end(self, record: SpanRecord) -> None:
            received.append(record)

    async with DefaultTracer(
        TraceSource("integration"), end_handler=Handler()
    ).start_as_current_span("work"):
        pass
    assert received[0].status is SpanStatus.OK


@pytest.mark.asyncio
async def test_scope_ordinary_exception_records_safe_error() -> None:
    received: list[SpanRecord] = []

    class Handler:
        async def on_end(self, record: SpanRecord) -> None:
            received.append(record)

    tracer = DefaultTracer(TraceSource("integration"), end_handler=Handler())
    with pytest.raises(ValueError):
        async with tracer.start_as_current_span("work"):
            raise ValueError("SCOPE-SECRET")
    assert received[0].exception is not None and "SCOPE-SECRET" not in str(received[0].exception)


@pytest.mark.asyncio
async def test_scope_timeout_records_timeout_status() -> None:
    received: list[SpanRecord] = []

    class Handler:
        async def on_end(self, record: SpanRecord) -> None:
            received.append(record)

    tracer = DefaultTracer(TraceSource("integration"), end_handler=Handler())
    with pytest.raises(TimeoutError):
        async with tracer.start_as_current_span("work"):
            raise TimeoutError("slow")
    assert received[0].status is SpanStatus.TIMEOUT


@pytest.mark.asyncio
async def test_scope_cancellation_records_cancelled_status() -> None:
    received: list[SpanRecord] = []

    class Handler:
        async def on_end(self, record: SpanRecord) -> None:
            received.append(record)

    tracer = DefaultTracer(TraceSource("integration"), end_handler=Handler())
    with pytest.raises(asyncio.CancelledError):
        async with tracer.start_as_current_span("work"):
            raise asyncio.CancelledError()
    assert received[0].status is SpanStatus.CANCELLED


@pytest.mark.asyncio
async def test_scope_failure_restores_parent_context() -> None:
    parent = TraceContext.create_root()
    tracer = DefaultTracer(TraceSource("integration"))
    with bind_trace_context(parent):
        with pytest.raises(ValueError):
            async with tracer.start_as_current_span("work"):
                raise ValueError("failure")
        assert current_trace_context() == parent


@pytest.mark.asyncio
async def test_scope_manual_end_does_not_duplicate_record() -> None:
    records: list[SpanRecord] = []

    class Handler:
        async def on_end(self, record: SpanRecord) -> None:
            records.append(record)

    tracer = DefaultTracer(TraceSource("integration"), end_handler=Handler())
    async with tracer.start_as_current_span("work") as span:
        await span.end()
    assert len(records) == 1


@pytest.mark.asyncio
async def test_tracer_status_tracks_mixed_span_decisions() -> None:
    recording = DefaultTracer(TraceSource("record"))
    dropped = DefaultTracer(TraceSource("drop"), sampler=AlwaysOffSampler())
    await (await recording.start_span("record")).end()
    await (await dropped.start_span("drop")).end()
    assert (await recording.status()).recording_count == 1 and (
        await dropped.status()
    ).dropped_count == 1


@pytest.mark.asyncio
async def test_tracer_close_rejects_new_spans() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    await tracer.close()
    with pytest.raises(TracerClosedError):
        await tracer.start_span("late")


@pytest.mark.asyncio
async def test_existing_spans_finish_after_close() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    span = await tracer.start_span("work")
    await tracer.close()
    assert await span.end() is not None


@pytest.mark.asyncio
async def test_two_tracers_remain_isolated() -> None:
    first, second = DefaultTracer(TraceSource("first")), DefaultTracer(TraceSource("second"))
    await first.start_span("one")
    assert (await first.status()).started_count == 1 and (await second.status()).started_count == 0


@pytest.mark.asyncio
async def test_parent_sampler_propagates_sampled_parent() -> None:
    sampler = ParentBasedSampler(AlwaysOffSampler())
    tracer = DefaultTracer(TraceSource("integration"), sampler=sampler)
    parent = TraceContext.create_root(sampled=True)
    span = await tracer.start_span("child", parent=parent)
    assert span.is_recording and span.context.sampled


@pytest.mark.asyncio
async def test_parent_sampler_drops_unsampled_parent() -> None:
    sampler = ParentBasedSampler(AlwaysOnSampler())
    tracer = DefaultTracer(TraceSource("integration"), sampler=sampler)
    parent = TraceContext.create_root(sampled=False)
    span = await tracer.start_span("child", parent=parent)
    assert not span.is_recording and not span.context.sampled


@pytest.mark.asyncio
async def test_tracer_import_creates_no_global_tasks() -> None:
    import importlib

    current = asyncio.current_task()
    assert current is not None
    before = {task for task in asyncio.all_tasks() if task is not current and not task.done()}
    module = importlib.import_module("alios_observability.tracing")
    await asyncio.sleep(0)
    after = {task for task in asyncio.all_tasks() if task is not current and not task.done()}
    assert module is sys.modules["alios_observability.tracing"] and after == before
