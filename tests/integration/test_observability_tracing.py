import asyncio
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest
from alios_core import RunId, TraceId
from alios_core.errors import (
    SpanCompletionError,
    SpanLimitError,
    SpanProcessorClosedError,
    SpanProcessorError,
    SpanRepositoryCapacityError,
    SpanValidationError,
    TracerClosedError,
    TraceSerializationError,
    ValidationError,
)
from alios_observability import (
    AlwaysOffSampler,
    AlwaysOnSampler,
    AlwaysRecordSampler,
    DefaultTracer,
    InMemorySpanRepository,
    LogException,
    ParentBasedSampler,
    RedactionPolicy,
    RedactionRule,
    SamplingRequest,
    SimpleSpanProcessor,
    SpanEvent,
    SpanFilter,
    SpanKind,
    SpanLimits,
    SpanLink,
    SpanRecord,
    SpanRepositorySnapshot,
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


@pytest.mark.asyncio
async def test_drop_tracer_accepts_payload_above_recording_limits() -> None:
    tracer = DefaultTracer(
        TraceSource("integration"), sampler=AlwaysOffSampler(), limits=SpanLimits(1, 1, 1)
    )
    span = await tracer.start_span("drop", attributes={"one": 1, "two": 2})
    assert not span.is_recording


@pytest.mark.asyncio
async def test_drop_tracer_large_payload_propagates_context() -> None:
    tracer = DefaultTracer(
        TraceSource("integration"), sampler=AlwaysOffSampler(), limits=SpanLimits(1, 1, 1)
    )
    parent = TraceContext.create_root()
    span = await tracer.start_span("drop", parent=parent, attributes={"one": 1, "two": 2})
    assert span.context.trace_id == parent.trace_id and not span.context.sampled


@pytest.mark.asyncio
async def test_drop_tracer_large_payload_produces_no_record() -> None:
    tracer = DefaultTracer(
        TraceSource("integration"), sampler=AlwaysOffSampler(), limits=SpanLimits(1, 1, 1)
    )
    assert await (await tracer.start_span("drop", attributes={"one": 1, "two": 2})).end() is None


@pytest.mark.asyncio
async def test_drop_tracer_large_payload_status_lifecycle() -> None:
    tracer = DefaultTracer(
        TraceSource("integration"), sampler=AlwaysOffSampler(), limits=SpanLimits(1, 1, 1)
    )
    span = await tracer.start_span("drop", attributes={"one": 1, "two": 2})
    assert (await tracer.status()).active_count == 1
    await span.end()
    assert (await tracer.status()).active_count == 0


@pytest.mark.asyncio
async def test_record_only_tracer_rejects_payload_above_limits() -> None:
    tracer = DefaultTracer(
        TraceSource("integration"), sampler=AlwaysRecordSampler(), limits=SpanLimits(1, 1, 1)
    )
    with pytest.raises(SpanLimitError):
        await tracer.start_span("record", attributes={"one": 1, "two": 2})


@pytest.mark.asyncio
async def test_always_on_tracer_rejects_payload_above_limits() -> None:
    tracer = DefaultTracer(
        TraceSource("integration"), sampler=AlwaysOnSampler(), limits=SpanLimits(1, 1, 1)
    )
    with pytest.raises(SpanLimitError):
        await tracer.start_span("sample", attributes={"one": 1, "two": 2})


@pytest.mark.asyncio
async def test_normalized_attribute_collision_rejected_through_tracer() -> None:
    with pytest.raises(SpanValidationError):
        await DefaultTracer(TraceSource("integration")).start_span(
            "work", attributes={"method": "GET", " method ": "POST"}
        )


@pytest.mark.asyncio
async def test_normalized_attribute_collision_leaves_tracer_usable() -> None:
    tracer = DefaultTracer(TraceSource("integration"))
    with pytest.raises(SpanValidationError):
        await tracer.start_span("bad", attributes={"method": "GET", " method ": "POST"})
    assert (await tracer.start_span("good")).is_recording


@pytest.mark.asyncio
async def test_non_recording_event_uses_registry_clock_equivalent_tracer_clock() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values = [now, now, now]
    tracer = DefaultTracer(TraceSource("integration"), sampler=AlwaysOffSampler(), clock=values.pop)
    assert await (await tracer.start_span("drop")).add_event("event") is None and not values


@pytest.mark.asyncio
async def test_non_recording_event_clock_failure_preserves_active_span() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values = [now, now]

    def clock() -> datetime:
        if values:
            return values.pop()
        raise RuntimeError("clock failure")

    tracer = DefaultTracer(TraceSource("integration"), sampler=AlwaysOffSampler(), clock=clock)
    span = await tracer.start_span("drop")
    with pytest.raises(RuntimeError):
        await span.add_event("event")
    assert not span.is_ended and (await tracer.status()).active_count == 1


def test_sampling_request_explicit_parent_round_trip() -> None:
    parent = TraceContext.create_root()
    request = SamplingRequest(
        TraceId(), parent, "work", TraceSource("integration"), SpanKind.INTERNAL
    )
    assert (
        SamplingRequest.from_dict(request.to_dict(RedactionPolicy(include_default_rules=False)))
        == request
    )


def test_sampling_request_empty_parent_payload_rejected() -> None:
    request = SamplingRequest(
        TraceId(), None, "work", TraceSource("integration"), SpanKind.INTERNAL
    )
    rendered = request.to_dict()
    cast(dict[str, object], rendered)["parent_context"] = {}
    with pytest.raises(TraceSerializationError):
        SamplingRequest.from_dict(rendered)


def _processing_stack(
    *, maximum_records: int = 10_000, sampler: object | None = None
) -> tuple[DefaultTracer, SimpleSpanProcessor, InMemorySpanRepository]:
    repository = InMemorySpanRepository(maximum_records=maximum_records)
    processor = SimpleSpanProcessor(repository)
    tracer = DefaultTracer(
        TraceSource("integration"),
        sampler=cast(AlwaysOnSampler, sampler) if sampler is not None else AlwaysOnSampler(),
        end_handler=processor,
    )
    return tracer, processor, repository


@pytest.mark.asyncio
async def test_tracer_processor_repository_root_span() -> None:
    tracer, _, repository = _processing_stack()
    span = await tracer.start_span("root")
    await span.end()
    assert (await repository.list())[0].context == span.context


@pytest.mark.asyncio
async def test_tracer_processor_repository_child_lineage() -> None:
    tracer, _, repository = _processing_stack()
    root = await tracer.start_span("root")
    child = await tracer.start_span("child", parent=root.context)
    await child.end()
    assert (await repository.list())[0].context.parent_span_id == root.context.span_id


@pytest.mark.asyncio
async def test_tracer_processor_repository_nested_scopes() -> None:
    tracer, _, repository = _processing_stack()
    async with tracer.start_as_current_span("root"):
        async with tracer.start_as_current_span("child"):
            pass
    assert await repository.count() == 2


@pytest.mark.asyncio
async def test_tracer_processor_repository_sibling_spans() -> None:
    tracer, _, repository = _processing_stack()
    root = await tracer.start_span("root")
    first = await tracer.start_span("first", parent=root.context)
    second = await tracer.start_span("second", parent=root.context)
    await first.end()
    await second.end()
    assert {item.context.parent_span_id for item in await repository.list()} == {
        root.context.span_id
    }


@pytest.mark.asyncio
async def test_tracer_processor_repository_concurrent_spans() -> None:
    tracer, _, repository = _processing_stack()
    spans = [await tracer.start_span(name) for name in ("one", "two", "three")]
    await asyncio.gather(*(span.end() for span in spans))
    assert await repository.count() == 3


@pytest.mark.asyncio
async def test_record_only_span_is_stored() -> None:
    tracer, _, repository = _processing_stack(sampler=AlwaysRecordSampler())
    await (await tracer.start_span("record")).end()
    assert not (await repository.list())[0].context.sampled


@pytest.mark.asyncio
async def test_sampled_span_is_stored() -> None:
    tracer, _, repository = _processing_stack()
    await (await tracer.start_span("sampled")).end()
    assert (await repository.list())[0].context.sampled


@pytest.mark.asyncio
async def test_dropped_span_is_not_stored() -> None:
    tracer, _, repository = _processing_stack(sampler=AlwaysOffSampler())
    await (await tracer.start_span("drop")).end()
    assert await repository.count() == 0


@pytest.mark.asyncio
async def test_manual_end_stores_record_once() -> None:
    tracer, _, repository = _processing_stack()
    span = await tracer.start_span("manual")
    await span.end()
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_scope_end_stores_record_once() -> None:
    tracer, _, repository = _processing_stack()
    async with tracer.start_as_current_span("scope"):
        pass
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_repeated_end_does_not_duplicate_repository_record() -> None:
    tracer, _, repository = _processing_stack()
    span = await tracer.start_span("repeat")
    await span.end()
    await span.end()
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_repository_filters_by_trace_id() -> None:
    tracer, _, repository = _processing_stack()
    span = await tracer.start_span("trace")
    await span.end()
    assert await repository.list(SpanFilter(trace_ids=frozenset({span.context.trace_id})))


@pytest.mark.asyncio
async def test_repository_filters_by_run_id() -> None:
    tracer, _, repository = _processing_stack()
    parent = TraceContext.create_root(run_id=RunId())
    span = await tracer.start_span("run", parent=parent)
    await span.end()
    assert parent.run_id is not None
    assert await repository.count(SpanFilter(run_ids=frozenset({parent.run_id}))) == 1


@pytest.mark.asyncio
async def test_repository_filters_by_name_and_status() -> None:
    tracer, _, repository = _processing_stack()
    span = await tracer.start_span("named")
    await span.set_status(SpanStatus.ERROR)
    await span.end()
    assert (
        await repository.count(
            SpanFilter(names=frozenset({"named"}), statuses=frozenset({SpanStatus.ERROR}))
        )
        == 1
    )


@pytest.mark.asyncio
async def test_repository_filters_by_attributes() -> None:
    tracer, _, repository = _processing_stack()
    span = await tracer.start_span("attribute", attributes={"region": "eu"})
    await span.end()
    assert await repository.count(SpanFilter(attribute_equals={"region": "eu"})) == 1


@pytest.mark.asyncio
async def test_repository_filters_by_duration() -> None:
    tracer, _, repository = _processing_stack()
    now = datetime.now(UTC)
    span = await tracer.start_span("duration", started_at=now)
    await span.end(ended_at=now)
    assert await repository.count(SpanFilter(maximum_duration_ns=0)) == 1


@pytest.mark.asyncio
async def test_repository_filter_pagination_is_deterministic() -> None:
    repository = InMemorySpanRepository()
    records = [_error_record(str(index)) for index in (3, 1, 2)]
    for record in records:
        await repository.add(record)
    assert await repository.list(SpanFilter(offset=1, limit=1)) == tuple(
        sorted(
            records,
            key=lambda item: (
                item.ended_at,
                item.started_at,
                str(item.context.trace_id),
                str(item.context.span_id),
            ),
        )[1:2]
    )


@pytest.mark.asyncio
async def test_repository_count_ignores_pagination() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_error_record("one"))
    await repository.add(_error_record("two"))
    assert await repository.count(SpanFilter(offset=10, limit=0)) == 2


@pytest.mark.asyncio
async def test_repository_snapshot_round_trip() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_error_record("safe"))
    snapshot = await repository.snapshot()
    rendered = snapshot.to_dict(RedactionPolicy(include_default_rules=False))
    assert SpanRepositorySnapshot.from_dict(rendered) == snapshot


@pytest.mark.asyncio
async def test_repository_filtered_snapshot() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_error_record("value"))
    assert (await repository.snapshot(SpanFilter(names=frozenset({"missing"})))).filtered


@pytest.mark.asyncio
async def test_repository_snapshot_remains_unchanged_after_add() -> None:
    repository = InMemorySpanRepository()
    snapshot = await repository.snapshot()
    await repository.add(_error_record("later"))
    assert snapshot.records == ()


@pytest.mark.asyncio
async def test_repository_snapshot_remains_unchanged_after_reset() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_error_record("before"))
    snapshot = await repository.snapshot()
    await repository.reset()
    assert len(snapshot.records) == 1


@pytest.mark.asyncio
async def test_repository_snapshot_redacts_sensitive_record_data() -> None:
    secret, repository = "C1B2-SNAPSHOT-SECRET", InMemorySpanRepository()
    await repository.add(_error_record(secret))
    assert secret not in repr((await repository.snapshot()).to_dict())


@pytest.mark.asyncio
async def test_repository_remove_and_reuse_capacity() -> None:
    repository, record = InMemorySpanRepository(maximum_records=1), _error_record("first")
    await repository.add(record)
    await repository.remove(record.context.trace_id, record.context.span_id)
    await repository.add(_error_record("second"))
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_repository_filtered_reset() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_error_record("selected"))
    assert await repository.reset(SpanFilter(source_components=frozenset({"integration"}))) == 1


@pytest.mark.asyncio
async def test_repository_paginated_reset() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_error_record("one"))
    await repository.add(_error_record("two"))
    assert await repository.reset(SpanFilter(limit=1)) == 1 and await repository.count() == 1


@pytest.mark.asyncio
async def test_repository_reset_after_close() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_error_record("closed"))
    await repository.close()
    assert await repository.reset() == 1


@pytest.mark.asyncio
async def test_repository_reads_after_close() -> None:
    repository, record = InMemorySpanRepository(), _error_record("read")
    await repository.add(record)
    await repository.close()
    assert await repository.get(record.context.trace_id, record.context.span_id) is record


@pytest.mark.asyncio
async def test_processor_close_rejects_future_records() -> None:
    processor = SimpleSpanProcessor(InMemorySpanRepository())
    await processor.close()
    with pytest.raises(SpanProcessorClosedError):
        await processor.on_end(_error_record("future"))


@pytest.mark.asyncio
async def test_processor_external_repository_survives_close() -> None:
    repository = InMemorySpanRepository()
    await SimpleSpanProcessor(repository).close()
    await repository.add(_error_record("survives"))
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_processor_owned_repository_closes() -> None:
    repository = InMemorySpanRepository()
    await SimpleSpanProcessor(repository, close_repository=True).close()
    assert (await repository.status()).closed


@pytest.mark.asyncio
async def test_processor_repository_capacity_failure_reaches_tracer() -> None:
    tracer, _, repository = _processing_stack(maximum_records=1)
    await (await tracer.start_span("first")).end()
    with pytest.raises(SpanCompletionError):
        await (await tracer.start_span("second")).end()
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_processor_repository_failure_marks_tracer_completion_failed() -> None:
    tracer, _, _ = _processing_stack(maximum_records=1)
    await (await tracer.start_span("first")).end()
    with pytest.raises(SpanCompletionError):
        await (await tracer.start_span("second")).end()
    assert (await tracer.status()).failed_completion_count == 1


@pytest.mark.asyncio
async def test_processor_failure_does_not_reopen_span() -> None:
    tracer, _, _ = _processing_stack(maximum_records=1)
    await (await tracer.start_span("first")).end()
    span = await tracer.start_span("second")
    with pytest.raises(SpanCompletionError):
        await span.end()
    assert span.is_ended


@pytest.mark.asyncio
async def test_active_span_completes_after_tracer_close_into_repository() -> None:
    tracer, _, repository = _processing_stack()
    span = await tracer.start_span("active")
    await tracer.close()
    await span.end()
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_active_span_fails_safely_after_processor_close() -> None:
    tracer, processor, repository = _processing_stack()
    span = await tracer.start_span("active")
    await processor.close()
    with pytest.raises(SpanCompletionError):
        await span.end()
    assert span.is_ended and await repository.count() == 0


@pytest.mark.asyncio
async def test_concurrent_repository_final_slot() -> None:
    repository = InMemorySpanRepository(maximum_records=1)
    results = await asyncio.gather(
        *(repository.add(_error_record(value)) for value in ("a", "b")), return_exceptions=True
    )
    assert sum(result is None for result in results) == 1 and any(
        isinstance(result, SpanRepositoryCapacityError) for result in results
    )


@pytest.mark.asyncio
async def test_concurrent_processor_deliveries_are_lossless() -> None:
    repository, processor = InMemorySpanRepository(), None
    processor = SimpleSpanProcessor(repository)
    await asyncio.gather(*(processor.on_end(_error_record(value)) for value in ("a", "b", "c")))
    assert await repository.count() == 3


@pytest.mark.asyncio
async def test_span_repository_errors_do_not_expose_secret_values() -> None:
    secret, repository = "C1B2-REPOSITORY-SECRET", InMemorySpanRepository(maximum_records=1)
    await repository.add(_error_record(secret))
    with pytest.raises(SpanRepositoryCapacityError) as captured:
        await repository.add(_error_record("other"))
    assert secret not in str(captured.value) and secret not in repr(captured.value.to_dict())


@pytest.mark.asyncio
async def test_span_processor_errors_do_not_expose_secret_values() -> None:
    secret, repository = "C1B2-PROCESSOR-SECRET", InMemorySpanRepository(maximum_records=1)
    await repository.add(_error_record("existing"))
    with pytest.raises(SpanProcessorError) as captured:
        await SimpleSpanProcessor(repository).on_end(_error_record(secret))
    assert secret not in repr(captured.value) and secret not in repr(captured.value.to_dict())


def test_public_span_repository_exports_preserve_class_identity() -> None:
    import alios_observability
    import alios_observability.tracing as tracing

    assert alios_observability.InMemorySpanRepository is tracing.InMemorySpanRepository


@pytest.mark.asyncio
async def test_span_processing_import_creates_no_global_tasks() -> None:
    before = asyncio.all_tasks()
    import alios_observability.tracing  # noqa: F401

    await asyncio.sleep(0)
    assert asyncio.all_tasks() == before


class _IntegrationBlockingRepository(InMemorySpanRepository):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def add(self, record: SpanRecord) -> None:
        self.entered.set()
        await self.release.wait()
        await super().add(record)


@pytest.mark.asyncio
async def test_processor_force_flush_waits_for_in_flight_store() -> None:
    repository = _IntegrationBlockingRepository()
    processor = SimpleSpanProcessor(repository)
    delivery = asyncio.create_task(processor.on_end(_error_record("flush")))
    await repository.entered.wait()
    flush = asyncio.create_task(processor.force_flush())
    assert not flush.done()
    repository.release.set()
    await asyncio.gather(delivery, flush)


@pytest.mark.asyncio
async def test_processor_close_waits_for_in_flight_store() -> None:
    repository = _IntegrationBlockingRepository()
    processor = SimpleSpanProcessor(repository)
    delivery = asyncio.create_task(processor.on_end(_error_record("close")))
    await repository.entered.wait()
    closing = asyncio.create_task(processor.close())
    assert not closing.done()
    repository.release.set()
    await asyncio.gather(delivery, closing)


@pytest.mark.asyncio
async def test_concurrent_repository_query_during_writes() -> None:
    repository = InMemorySpanRepository()
    additions = tuple(repository.add(_error_record(value)) for value in ("one", "two", "three"))
    results = await asyncio.gather(repository.list(), *additions)
    assert isinstance(results[0], tuple) and await repository.count() == 3


@pytest.mark.asyncio
async def test_concurrent_repository_reset_during_queries_has_no_deadlock() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_error_record("reset"))
    listed, removed, counted = await asyncio.gather(
        repository.list(), repository.reset(), repository.count()
    )
    assert isinstance(listed, tuple) and removed == 1 and counted in (0, 1)
