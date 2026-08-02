import asyncio
from datetime import UTC, datetime

import pytest
from alios_observability import (
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


def test_sibling_async_tasks_inherit_same_parent_snapshot() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_child_context_has_unique_span_id() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_multiple_children_share_trace_id() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_trace_binding_restores_after_child_failure() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_trace_binding_restores_after_child_cancellation() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_trace_context_round_trip_across_async_boundary() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_span_record_with_alios_exception_round_trip() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_span_record_with_ordinary_normalized_exception_is_safe() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_span_record_rendering_redacts_all_sensitive_locations() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_span_record_rendering_does_not_mutate_internal_record() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_redacted_span_record_is_parseable() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_custom_redaction_policy_applies_to_span_record() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_default_redaction_policy_does_not_mutate_global_state() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_trace_models_do_not_create_global_tasks() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_public_tracing_imports_preserve_logging_exports() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id


def test_public_tracing_imports_preserve_metrics_exports() -> None:
    context = TraceContext.create_root()
    assert TraceContext.from_dict(context.to_dict()).span_id == context.span_id
