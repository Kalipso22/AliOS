import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from itertools import product
from typing import cast
from uuid import UUID, uuid4

import pytest
from alios_core import CorrelationId, RunId, SpanId, TraceId
from alios_core.errors import (
    ResourceConflictError,
    ResourceNotFoundError,
    SamplingError,
    SpanCompletionError,
    SpanLimitError,
    SpanProcessorClosedError,
    SpanProcessorError,
    SpanRepositoryCapacityError,
    SpanRepositoryClosedError,
    SpanRepositoryError,
    SpanStateError,
    SpanValidationError,
    TraceContextError,
    TraceSerializationError,
)
from alios_core.ids import TenantId, UserId
from alios_observability import (
    AlwaysOffSampler,
    AlwaysOnSampler,
    AlwaysRecordSampler,
    DefaultTracer,
    InMemorySpanRepository,
    ParentBasedSampler,
    RedactionPolicy,
    SamplingDecision,
    SamplingRequest,
    SamplingResult,
    SimpleSpanProcessor,
    SpanEvent,
    SpanFilter,
    SpanKind,
    SpanLimits,
    SpanLink,
    SpanProcessorStatus,
    SpanRecord,
    SpanRepositorySnapshot,
    SpanRepositoryStatus,
    SpanStatus,
    TraceContext,
    TraceIdRatioSampler,
    TraceSource,
    bind_trace_context,
    current_trace_context,
    require_trace_context,
)


def _context() -> TraceContext:
    return TraceContext.create_root(baggage={"tenant": "one"})


def _record() -> SpanRecord:
    now = datetime.now(UTC)
    return SpanRecord(
        _context(),
        "operation",
        TraceSource("component"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now + timedelta(seconds=1),
    )


def test_trace_id_generation() -> None:
    assert TraceId().value.version == 4


def test_span_id_generation() -> None:
    assert SpanId().value.version == 4


def test_trace_id_string_round_trip() -> None:
    value = TraceId()
    context = TraceContext(value, SpanId())
    assert TraceContext.from_dict(context.to_dict()).trace_id == value


def test_span_id_string_round_trip() -> None:
    value = SpanId()
    context = TraceContext(TraceId(), value)
    assert TraceContext.from_dict(context.to_dict()).span_id == value


def test_trace_id_does_not_equal_span_id() -> None:
    trace = TraceId()
    assert trace != SpanId(str(trace))


def test_trace_identifiers_are_hashable() -> None:
    assert len({TraceId(), SpanId()}) == 2


def test_trace_source_valid() -> None:
    assert TraceSource("A", "B", "C").component == "A"


def test_trace_source_requires_component() -> None:
    with pytest.raises(SpanValidationError):
        TraceSource(" ")


def test_trace_source_trims_fields() -> None:
    assert TraceSource(" A ", " B ").module == "B"


def test_trace_source_serialization() -> None:
    assert TraceSource("A").to_dict()["component"] == "A"


def test_trace_source_round_trip() -> None:
    source = TraceSource("A", "M", "O")
    assert TraceSource.from_dict(source.to_dict()) == source


def test_span_kind_exact_values() -> None:
    assert SpanKind.CLIENT.value == "client"


def test_span_kind_parsing() -> None:
    assert SpanKind.parse("server") is SpanKind.SERVER


def test_span_status_exact_values() -> None:
    assert SpanStatus.TIMEOUT.value == "timeout"


def test_span_status_parsing() -> None:
    assert SpanStatus.parse("error") is SpanStatus.ERROR


def test_trace_data_rejects_nan() -> None:
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"x": float("nan")})


def test_trace_data_rejects_naive_datetime() -> None:
    with pytest.raises(SpanValidationError):
        SpanEvent("event", datetime.now(), {})


def test_trace_baggage_valid() -> None:
    assert _context().baggage["tenant"] == "one"


def test_trace_baggage_is_immutable() -> None:
    baggage = _context().baggage
    with pytest.raises(TypeError):
        cast(dict[str, str], baggage)["tenant"] = "changed"


def test_trace_context_root_creation() -> None:
    assert _context().parent_span_id is None


def test_trace_context_child_retains_trace_id() -> None:
    context = _context()
    assert context.create_child().trace_id == context.trace_id


def test_trace_context_child_sets_parent_span() -> None:
    context = _context()
    assert context.create_child().parent_span_id == context.span_id


def test_trace_context_with_baggage() -> None:
    assert _context().with_baggage({"region": "eu"}).baggage["region"] == "eu"


def test_trace_context_round_trip() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()) == context


def test_current_trace_context_is_none_by_default() -> None:
    assert current_trace_context() is None


def test_require_trace_context_raises_when_unbound() -> None:
    with pytest.raises(TraceContextError):
        require_trace_context()


def test_sync_trace_context_binding() -> None:
    context = _context()
    with bind_trace_context(context):
        assert require_trace_context() == context
    assert current_trace_context() is None


def test_span_event_valid() -> None:
    assert SpanEvent("event", datetime.now(UTC)).name == "event"


def test_span_event_round_trip() -> None:
    event = SpanEvent("event", datetime.now(UTC), {"x": [1]})
    assert SpanEvent.from_dict(event.to_dict()) == event


def test_span_link_valid() -> None:
    context = _context()
    assert SpanLink(context).context == context


def test_span_link_round_trip() -> None:
    link = SpanLink(_context(), {"x": 1})
    assert SpanLink.from_dict(link.to_dict()) == link


def test_span_record_valid() -> None:
    assert _record().duration_ns == 1_000_000_000


def test_span_record_duration() -> None:
    assert _record().duration == timedelta(seconds=1)


def test_span_record_round_trip() -> None:
    record = _record()
    assert SpanRecord.from_dict(record.to_dict()) == record


def test_span_record_rejects_end_before_start() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanValidationError):
        SpanRecord(
            _context(),
            "x",
            TraceSource("x"),
            SpanKind.INTERNAL,
            SpanStatus.OK,
            now,
            now - timedelta(seconds=1),
        )


def test_trace_id_type_sensitive_equality() -> None:
    value = uuid4()
    assert TraceId(value) == TraceId(value)


def test_span_id_type_sensitive_equality() -> None:
    value = uuid4()
    assert SpanId(value) == SpanId(value)


def test_trace_id_rejects_non_v4_uuid() -> None:
    with pytest.raises(ValueError):
        TraceId(UUID("00000000-0000-1000-8000-000000000000"))


def test_span_id_rejects_non_v4_uuid() -> None:
    with pytest.raises(ValueError):
        SpanId(UUID("00000000-0000-1000-8000-000000000000"))


def test_trace_source_preserves_capitalization() -> None:
    source = TraceSource("RuntimeAPI", "AliOS.Runtime", "ExecuteTask")
    assert (source.component, source.module, source.operation) == (
        "RuntimeAPI",
        "AliOS.Runtime",
        "ExecuteTask",
    )


def test_trace_source_rejects_control_character() -> None:
    with pytest.raises(SpanValidationError):
        TraceSource("runtime\x01")


def test_trace_source_rejects_newline() -> None:
    with pytest.raises(SpanValidationError):
        TraceSource("runtime\nworker")


def test_trace_source_rejects_excessive_length() -> None:
    with pytest.raises(SpanValidationError):
        TraceSource("x" * 257)


def test_trace_source_is_immutable() -> None:
    source = TraceSource("runtime")
    with pytest.raises(FrozenInstanceError):
        setattr(source, "".join(("com", "ponent")), "changed")


def test_trace_source_serialization_is_independent() -> None:
    source = TraceSource("runtime")
    serialized = source.to_dict()
    serialized["component"] = "changed"
    assert source.component == "runtime"


def test_trace_source_from_dict_uses_strict_types() -> None:
    with pytest.raises(TraceSerializationError):
        TraceSource.from_dict({"component": 1})


def test_span_kind_rejects_unknown_value() -> None:
    with pytest.raises(SpanValidationError):
        SpanKind.parse("unknown")


def test_span_kind_rejects_non_string() -> None:
    with pytest.raises(SpanValidationError):
        SpanKind.parse(cast(str, 1))


def test_span_status_rejects_unknown_value() -> None:
    with pytest.raises(SpanValidationError):
        SpanStatus.parse("unknown")


def test_span_status_rejects_non_string() -> None:
    with pytest.raises(SpanValidationError):
        SpanStatus.parse(cast(str, 1))


def test_trace_data_accepts_nested_json_values() -> None:
    event = SpanEvent("event", datetime.now(UTC), {"nested": {"items": [None, True, 1]}})
    assert event.to_dict()["attributes"] == {"nested": {"items": [None, True, 1]}}


def test_trace_data_normalizes_identifier() -> None:
    identifier = TraceId()
    assert SpanEvent("event", datetime.now(UTC), {"id": identifier}).to_dict()["attributes"] == {
        "id": str(identifier)
    }


def test_trace_data_normalizes_str_enum() -> None:
    assert SpanEvent("event", datetime.now(UTC), {"kind": SpanKind.CLIENT}).to_dict()[
        "attributes"
    ] == {"kind": "client"}


def test_trace_data_normalizes_aware_datetime() -> None:
    timestamp = datetime.now(UTC)
    assert SpanEvent("event", datetime.now(UTC), {"at": timestamp}).to_dict()["attributes"] == {
        "at": timestamp.isoformat()
    }


def test_trace_data_rejects_positive_infinity() -> None:
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"value": float("inf")})


def test_trace_data_rejects_negative_infinity() -> None:
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"value": float("-inf")})


def test_trace_data_rejects_bytes() -> None:
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"value": b"bytes"})


def test_trace_data_rejects_set() -> None:
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"value": {"set"}})


def test_trace_data_rejects_non_string_mapping_key() -> None:
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"value": {1: "invalid"}})


def test_trace_data_rejects_function() -> None:
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"value": lambda: None})


def test_trace_data_rejects_coroutine() -> None:
    async def value() -> None:
        return None

    coroutine = value()
    try:
        with pytest.raises(TraceSerializationError):
            SpanEvent("event", datetime.now(UTC), {"value": coroutine})
    finally:
        coroutine.close()


def test_trace_data_rejects_cycle() -> None:
    value: dict[str, object] = {}
    value["self"] = value
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), value)


def test_trace_data_enforces_maximum_depth() -> None:
    value: object = "leaf"
    levels = 0
    while levels < 17:
        value = [value]
        levels += 1
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"value": value})


def test_trace_data_enforces_total_item_budget() -> None:
    values = {"".join(parts): 0 for parts in product("abcdefghijk", repeat=4)}
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), values)


def test_trace_data_error_omits_rejected_value() -> None:
    secret = "TRACE-C1A3-SECRET-74c912"
    with pytest.raises(TraceSerializationError) as error:
        SpanEvent("event", datetime.now(UTC), {"value": secret.encode()})
    assert secret not in str(error.value)


def test_trace_data_deep_thaw_is_independent() -> None:
    event = SpanEvent("event", datetime.now(UTC), {"nested": {"items": ["one"]}})
    rendered = event.to_dict()
    attributes = cast(dict[str, object], rendered["attributes"])
    nested = cast(dict[str, object], attributes["nested"])
    cast(list[str], nested["items"]).append("two")
    assert event.attributes["nested"] == {"items": ("one",)}


def test_trace_baggage_empty() -> None:
    assert TraceContext.create_root().baggage == {}


def test_trace_baggage_defensive_copy() -> None:
    baggage = {"region": "eu"}
    context = TraceContext.create_root(baggage=baggage)
    baggage["region"] = "us"
    assert context.baggage["region"] == "eu"


def test_trace_baggage_rejects_non_string_key() -> None:
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage=cast(dict[str, str], {1: "value"}))


def test_trace_baggage_rejects_non_string_value() -> None:
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage=cast(dict[str, str], {"key": 1}))


def test_trace_baggage_rejects_invalid_key() -> None:
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage={"invalid key": "value"})


def test_trace_baggage_rejects_double_underscore_key() -> None:
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage={"__private": "value"})


def test_trace_baggage_rejects_control_character() -> None:
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage={"key": "bad\x01"})


def test_trace_baggage_rejects_excessive_entries() -> None:
    baggage = {
        f"key{letter}{suffix}": "value"
        for letter in "abcdefghijklmnopqrstuvwxyz"
        for suffix in ("a", "b", "c")
    }
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage=baggage)


def test_trace_baggage_rejects_excessive_key_length() -> None:
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage={"a" * 129: "value"})


def test_trace_baggage_rejects_excessive_value_length() -> None:
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage={"key": "v" * 1025})


def test_trace_baggage_error_omits_value() -> None:
    secret = "TRACE-SECRET"
    with pytest.raises(TraceContextError) as error:
        TraceContext.create_root(baggage={"key": secret + "\x01"})
    assert secret not in str(error.value)


def test_trace_context_valid() -> None:
    context = TraceContext(TraceId(), SpanId(), sampled=False, baggage={"region": "eu"})
    assert context.sampled is False and context.baggage == {"region": "eu"}


def test_trace_context_rejects_wrong_trace_identifier() -> None:
    with pytest.raises(TraceContextError):
        TraceContext(cast(TraceId, SpanId()), SpanId())


def test_trace_context_rejects_wrong_span_identifier() -> None:
    with pytest.raises(TraceContextError):
        TraceContext(TraceId(), cast(SpanId, TraceId()))


def test_trace_context_rejects_wrong_parent_identifier() -> None:
    with pytest.raises(TraceContextError):
        TraceContext(TraceId(), SpanId(), cast(SpanId, TraceId()))


def test_trace_context_rejects_wrong_correlation_identifier() -> None:
    with pytest.raises(TraceContextError):
        TraceContext(TraceId(), SpanId(), correlation_id=cast(CorrelationId, RunId()))


def test_trace_context_rejects_wrong_run_identifier() -> None:
    with pytest.raises(TraceContextError):
        TraceContext(TraceId(), SpanId(), run_id=cast(RunId, CorrelationId()))


def test_trace_context_rejects_wrong_tenant_identifier() -> None:
    with pytest.raises(TraceContextError):
        TraceContext(TraceId(), SpanId(), tenant_id=cast(TenantId, UserId()))


def test_trace_context_rejects_wrong_user_identifier() -> None:
    with pytest.raises(TraceContextError):
        TraceContext(TraceId(), SpanId(), user_id=cast(UserId, TenantId()))


def test_trace_context_rejects_non_boolean_sampled() -> None:
    with pytest.raises(TraceContextError):
        TraceContext(TraceId(), SpanId(), sampled=cast(bool, "yes"))


def test_trace_context_rejects_parent_equal_to_span() -> None:
    span_id = SpanId()
    with pytest.raises(TraceContextError):
        TraceContext(TraceId(), span_id, span_id)


def test_trace_context_is_immutable() -> None:
    context = _context()
    with pytest.raises(FrozenInstanceError):
        setattr(context, "".join(("sam", "pled")), False)


def test_trace_context_root_generates_ids() -> None:
    context = TraceContext.create_root()
    assert isinstance(context.trace_id, TraceId) and isinstance(context.span_id, SpanId)


def test_trace_context_child_generates_new_span_id() -> None:
    context = TraceContext.create_root()
    assert context.create_child().span_id != context.span_id


def test_trace_context_child_retains_scope() -> None:
    context = TraceContext.create_root(
        correlation_id=CorrelationId(), run_id=RunId(), tenant_id=TenantId(), user_id=UserId()
    )
    child = context.create_child()
    assert (child.correlation_id, child.run_id, child.tenant_id, child.user_id) == (
        context.correlation_id,
        context.run_id,
        context.tenant_id,
        context.user_id,
    )


def test_trace_context_child_inherits_sampled() -> None:
    assert TraceContext.create_root(sampled=False).create_child().sampled is False


def test_trace_context_child_overrides_sampled() -> None:
    assert TraceContext.create_root(sampled=False).create_child(sampled=True).sampled is True


def test_trace_context_child_merges_baggage() -> None:
    child = TraceContext.create_root(baggage={"region": "eu", "tier": "free"}).create_child(
        baggage={"tier": "pro"}
    )
    assert child.baggage == {"region": "eu", "tier": "pro"}


def test_trace_context_child_does_not_mutate_parent() -> None:
    context = TraceContext.create_root(baggage={"region": "eu"})
    context.create_child(baggage={"region": "us"})
    assert context.baggage == {"region": "eu"}


def test_trace_context_with_baggage_does_not_mutate_original() -> None:
    context = TraceContext.create_root(baggage={"region": "eu"})
    replacement = context.with_baggage({"region": "us"})
    assert context.baggage["region"] == "eu" and replacement.baggage["region"] == "us"


def test_trace_context_serialization() -> None:
    context = _context()
    assert context.to_dict()["trace_id"] == str(context.trace_id)


def test_trace_context_serialization_is_independent() -> None:
    context = _context()
    serialized = context.to_dict()
    cast(dict[str, object], serialized["baggage"])["tenant"] = "changed"
    assert context.baggage["tenant"] == "one"


def test_trace_context_from_dict_rejects_invalid_uuid() -> None:
    rendered = _context().to_dict()
    rendered["trace_id"] = "not-a-uuid"
    with pytest.raises(TraceSerializationError):
        TraceContext.from_dict(rendered)


def test_trace_context_from_dict_rejects_wrong_source_type() -> None:
    rendered = _context().to_dict()
    cast(dict[str, object], rendered)["baggage"] = ["tenant"]
    with pytest.raises(TraceSerializationError):
        TraceContext.from_dict(rendered)


def test_trace_context_rendering_redacts_baggage() -> None:
    secret = "TRACE-C1A3-SECRET-74c912"
    rendered = TraceContext.create_root(baggage={"api_key": secret}).to_dict()
    assert secret not in str(rendered)


def test_async_trace_context_binding() -> None:
    import asyncio

    async def bound() -> TraceContext:
        context = _context()
        async with bind_trace_context(context):
            return require_trace_context()

    assert asyncio.run(bound()).baggage == {"tenant": "one"}


def test_nested_trace_binding_restores_parent() -> None:
    parent, child = _context(), _context().create_child()
    with bind_trace_context(parent):
        with bind_trace_context(child):
            assert require_trace_context() == child
        assert require_trace_context() == parent


def test_trace_binding_restores_after_exception() -> None:
    context = _context()
    with bind_trace_context(context):
        with pytest.raises(RuntimeError):
            with bind_trace_context(context.create_child()):
                raise RuntimeError("failure")
        assert require_trace_context() == context


def test_trace_binding_restores_after_cancellation() -> None:
    import asyncio

    async def cancel_bound_child() -> None:
        root, started, release = _context(), asyncio.Event(), asyncio.Event()

        async def child() -> None:
            async with bind_trace_context(root.create_child()):
                started.set()
                await release.wait()

        async with bind_trace_context(root):
            task = asyncio.create_task(child())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert require_trace_context() == root

    asyncio.run(cancel_bound_child())


def test_trace_binding_rejects_wrong_context_type() -> None:
    with pytest.raises(TraceContextError):
        bind_trace_context(cast(TraceContext, "invalid"))


def test_trace_binding_rejects_concurrent_reentry() -> None:
    binding = bind_trace_context(_context())
    with binding:
        with pytest.raises(TraceContextError):
            binding.__enter__()


def test_trace_binding_token_resets_once() -> None:
    binding = bind_trace_context(_context())
    binding.__enter__()
    binding.__exit__(None, None, None)
    binding.__exit__(None, None, None)
    assert current_trace_context() is None


def test_trace_binding_does_not_mutate_context() -> None:
    context = TraceContext.create_root(baggage={"region": "eu"})
    with bind_trace_context(context):
        assert require_trace_context().baggage == {"region": "eu"}
    assert context.baggage == {"region": "eu"} and current_trace_context() is None


def test_span_event_requires_name() -> None:
    with pytest.raises(SpanValidationError):
        SpanEvent(" ", datetime.now(UTC))


def test_span_event_rejects_naive_timestamp() -> None:
    with pytest.raises(SpanValidationError):
        SpanEvent("event", datetime.now())


def test_span_event_rejects_control_character() -> None:
    with pytest.raises(SpanValidationError):
        SpanEvent("event\x01", datetime.now(UTC))


def test_span_event_attributes_are_immutable() -> None:
    attributes = {"nested": {"value": 1}}
    event = SpanEvent("event", datetime.now(UTC), attributes)
    attributes["nested"]["value"] = 2
    with pytest.raises(TypeError):
        cast(dict[str, object], event.attributes)["other"] = "value"
    assert event.to_dict()["attributes"] == {"nested": {"value": 1}}


def test_span_event_serialization() -> None:
    event = SpanEvent("event", datetime.now(UTC), {"value": 1})
    assert event.to_dict()["name"] == "event"


def test_span_event_redacts_name() -> None:
    secret = "TRACE-SECRET"
    rendered = SpanEvent(f"api_key={secret}", datetime.now(UTC)).to_dict()
    assert secret not in str(rendered)


def test_span_event_redacts_attributes() -> None:
    secret = "TRACE-SECRET"
    rendered = SpanEvent("event", datetime.now(UTC), {"api_key": secret}).to_dict()
    assert secret not in str(rendered)


def test_span_event_serialization_is_independent() -> None:
    event = SpanEvent("event", datetime.now(UTC), {"nested": {"value": 1}})
    rendered = event.to_dict()
    cast(dict[str, object], rendered["attributes"])["nested"] = "changed"
    assert event.to_dict()["attributes"] == {"nested": {"value": 1}}


def test_span_link_rejects_invalid_context() -> None:
    with pytest.raises(SpanValidationError):
        SpanLink(cast(TraceContext, "invalid"))


def test_span_link_attributes_are_immutable() -> None:
    values = {"nested": {"value": 1}}
    link = SpanLink(_context(), values)
    values["nested"]["value"] = 2
    with pytest.raises(TypeError):
        cast(dict[str, object], link.attributes)["other"] = "value"
    assert link.to_dict()["attributes"] == {"nested": {"value": 1}}


def test_span_link_serialization() -> None:
    context = _context()
    rendered_context = cast(dict[str, object], SpanLink(context).to_dict()["context"])
    assert rendered_context["trace_id"] == str(context.trace_id)


def test_span_link_redacts_baggage() -> None:
    secret = "TRACE-SECRET"
    rendered = SpanLink(TraceContext.create_root(baggage={"api_key": secret})).to_dict()
    assert secret not in str(rendered)


def test_span_link_redacts_attributes() -> None:
    secret = "TRACE-SECRET"
    rendered = SpanLink(_context(), {"api_key": secret}).to_dict()
    assert secret not in str(rendered)


def test_span_link_serialization_is_independent() -> None:
    link = SpanLink(_context(), {"nested": {"value": 1}})
    rendered = link.to_dict()
    cast(dict[str, object], rendered["attributes"])["nested"] = "changed"
    assert link.to_dict()["attributes"] == {"nested": {"value": 1}}


def test_span_record_rejects_self_link() -> None:
    context = _context()
    now = datetime.now(UTC)
    with pytest.raises(SpanValidationError):
        SpanRecord(
            context,
            "x",
            TraceSource("x"),
            SpanKind.INTERNAL,
            SpanStatus.OK,
            now,
            now,
            links=(SpanLink(context),),
        )


def test_span_record_requires_trace_context() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanValidationError):
        SpanRecord(
            cast(TraceContext, None),
            "x",
            TraceSource("x"),
            SpanKind.INTERNAL,
            SpanStatus.OK,
            now,
            now,
        )


def test_span_record_requires_trace_source() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanValidationError):
        SpanRecord(
            _context(), "x", cast(TraceSource, None), SpanKind.INTERNAL, SpanStatus.OK, now, now
        )


def test_span_record_requires_span_kind() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanValidationError):
        SpanRecord(_context(), "x", TraceSource("x"), cast(SpanKind, None), SpanStatus.OK, now, now)


def test_span_record_requires_span_status() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanValidationError):
        SpanRecord(
            _context(), "x", TraceSource("x"), SpanKind.INTERNAL, cast(SpanStatus, None), now, now
        )


def test_span_record_requires_name() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanValidationError):
        SpanRecord(_context(), " ", TraceSource("x"), SpanKind.INTERNAL, SpanStatus.OK, now, now)


def test_span_record_duration_ns() -> None:
    assert _record().duration_ns == 1_000_000_000


def test_trace_data_str_enum_normalizes_to_plain_string() -> None:
    event = SpanEvent("event", datetime.now(UTC), {"kind": SpanKind.CLIENT})
    assert event.to_dict()["attributes"] == {"kind": "client"}


def test_trace_attribute_naive_datetime_raises_serialization_error() -> None:
    with pytest.raises(TraceSerializationError):
        SpanEvent("event", datetime.now(UTC), {"at": datetime.now()})


def test_trace_context_from_dict_rejects_empty_parent_span_id() -> None:
    value = _context().to_dict()
    value["parent_span_id"] = ""
    with pytest.raises(TraceSerializationError):
        TraceContext.from_dict(value)


def test_trace_context_from_dict_rejects_empty_correlation_id() -> None:
    value = _context().to_dict()
    value["correlation_id"] = ""
    with pytest.raises(TraceSerializationError):
        TraceContext.from_dict(value)


def test_trace_context_from_dict_rejects_empty_run_id() -> None:
    value = _context().to_dict()
    value["run_id"] = ""
    with pytest.raises(TraceSerializationError):
        TraceContext.from_dict(value)


def test_trace_context_from_dict_rejects_empty_tenant_id() -> None:
    value = _context().to_dict()
    value["tenant_id"] = ""
    with pytest.raises(TraceSerializationError):
        TraceContext.from_dict(value)


def test_trace_context_from_dict_rejects_empty_user_id() -> None:
    value = _context().to_dict()
    value["user_id"] = ""
    with pytest.raises(TraceSerializationError):
        TraceContext.from_dict(value)


def test_trace_context_create_child_rejects_non_mapping_baggage() -> None:
    with pytest.raises(TraceContextError):
        _context().create_child(baggage=cast(dict[str, str], []))


def test_trace_context_with_baggage_rejects_non_mapping_values() -> None:
    with pytest.raises(TraceContextError):
        _context().with_baggage(cast(dict[str, str], []))


# Active tracing contracts -------------------------------------------------


def test_sampling_decision_exact_values() -> None:
    assert tuple(item.value for item in SamplingDecision) == (
        "drop",
        "record_only",
        "record_and_sample",
    )


def test_sampling_decision_parsing() -> None:
    assert SamplingDecision.parse("record_only") is SamplingDecision.RECORD_ONLY


def test_sampling_decision_rejects_unknown_value() -> None:
    with pytest.raises(SamplingError):
        SamplingDecision.parse("sometimes")


def test_sampling_result_drop_flags() -> None:
    result = SamplingResult(SamplingDecision.DROP)
    assert not result.is_recording and not result.is_sampled


def test_sampling_result_record_only_flags() -> None:
    result = SamplingResult(SamplingDecision.RECORD_ONLY)
    assert result.is_recording and not result.is_sampled


def test_sampling_result_record_and_sample_flags() -> None:
    result = SamplingResult(SamplingDecision.RECORD_AND_SAMPLE)
    assert result.is_recording and result.is_sampled


def test_sampling_result_attributes_are_immutable() -> None:
    values = {"nested": {"value": 1}}
    result = SamplingResult(SamplingDecision.DROP, values)
    values["nested"]["value"] = 2
    with pytest.raises(TypeError):
        cast(dict[str, object], result.attributes)["other"] = 1
    assert result.to_dict(RedactionPolicy(include_default_rules=False))["attributes"] == {
        "nested": {"value": 1}
    }


def test_sampling_result_serialization() -> None:
    assert SamplingResult(SamplingDecision.RECORD_ONLY, {"value": 1}).to_dict(
        RedactionPolicy(include_default_rules=False)
    ) == {"decision": "record_only", "attributes": {"value": 1}}


def test_sampling_result_round_trip() -> None:
    result = SamplingResult(SamplingDecision.RECORD_AND_SAMPLE, {"value": 1})
    assert (
        SamplingResult.from_dict(result.to_dict(RedactionPolicy(include_default_rules=False)))
        == result
    )


def test_sampling_result_from_dict_uses_strict_types() -> None:
    with pytest.raises(TraceSerializationError):
        SamplingResult.from_dict({"decision": 1, "attributes": {}})


def test_sampling_request_valid() -> None:
    parent = _context()
    request = SamplingRequest(TraceId(), parent, "work", TraceSource("test"), SpanKind.CLIENT)
    assert request.parent_context == parent and request.name == "work"


def test_sampling_request_rejects_wrong_trace_id() -> None:
    with pytest.raises(SamplingError):
        SamplingRequest(
            cast(TraceId, SpanId()), None, "work", TraceSource("test"), SpanKind.INTERNAL
        )


def test_sampling_request_rejects_wrong_parent() -> None:
    with pytest.raises(SamplingError):
        SamplingRequest(
            TraceId(), cast(TraceContext, "parent"), "work", TraceSource("test"), SpanKind.INTERNAL
        )


def test_sampling_request_rejects_wrong_source() -> None:
    with pytest.raises(SamplingError):
        SamplingRequest(TraceId(), None, "work", cast(TraceSource, "source"), SpanKind.INTERNAL)


def test_sampling_request_rejects_wrong_kind() -> None:
    with pytest.raises(SamplingError):
        SamplingRequest(TraceId(), None, "work", TraceSource("test"), cast(SpanKind, "kind"))


def test_sampling_request_attributes_are_immutable() -> None:
    values = {"nested": [1]}
    request = SamplingRequest(
        TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL, values
    )
    values["nested"].append(2)
    assert request.to_dict(RedactionPolicy(include_default_rules=False))["attributes"] == {
        "nested": [1]
    }


def test_sampling_request_links_are_immutable() -> None:
    links = (SpanLink(_context()),)
    request = SamplingRequest(
        TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL, links=links
    )
    assert request.links == links and request.links is links


def test_sampling_request_rejects_duplicate_links() -> None:
    link = SpanLink(_context())
    with pytest.raises(SpanValidationError):
        SamplingRequest(
            TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL, links=(link, link)
        )


def test_sampling_request_serialization() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    assert request.to_dict(RedactionPolicy(include_default_rules=False))["name"] == "work"


def test_sampling_request_round_trip() -> None:
    request = SamplingRequest(TraceId(), _context(), "work", TraceSource("test"), SpanKind.INTERNAL)
    policy = RedactionPolicy(include_default_rules=False)
    assert SamplingRequest.from_dict(request.to_dict(policy)) == request


def test_sampling_request_rendering_redacts_attributes() -> None:
    secret = "ACTIVE-SECRET"
    request = SamplingRequest(
        TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL, {"api_key": secret}
    )
    assert secret not in str(request.to_dict())


@pytest.mark.asyncio
async def test_always_on_sampler_records_and_samples() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    assert (
        await AlwaysOnSampler().should_sample(request)
    ).decision is SamplingDecision.RECORD_AND_SAMPLE


@pytest.mark.asyncio
async def test_always_off_sampler_drops() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    assert (await AlwaysOffSampler().should_sample(request)).decision is SamplingDecision.DROP


@pytest.mark.asyncio
async def test_always_record_sampler_records_only() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    assert (
        await AlwaysRecordSampler().should_sample(request)
    ).decision is SamplingDecision.RECORD_ONLY


@pytest.mark.asyncio
async def test_sampler_rejects_wrong_request_type() -> None:
    with pytest.raises(SamplingError):
        await AlwaysOnSampler().should_sample(cast(SamplingRequest, "request"))


def test_ratio_sampler_rejects_boolean_ratio() -> None:
    with pytest.raises(SamplingError):
        TraceIdRatioSampler(True)


def test_ratio_sampler_rejects_nan_ratio() -> None:
    with pytest.raises(SamplingError):
        TraceIdRatioSampler(float("nan"))


def test_ratio_sampler_rejects_negative_ratio() -> None:
    with pytest.raises(SamplingError):
        TraceIdRatioSampler(-0.1)


def test_ratio_sampler_rejects_ratio_above_one() -> None:
    with pytest.raises(SamplingError):
        TraceIdRatioSampler(1.1)


@pytest.mark.asyncio
async def test_ratio_sampler_zero_always_drops() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    assert (await TraceIdRatioSampler(0).should_sample(request)).decision is SamplingDecision.DROP


@pytest.mark.asyncio
async def test_ratio_sampler_one_always_samples() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    assert (
        await TraceIdRatioSampler(1).should_sample(request)
    ).decision is SamplingDecision.RECORD_AND_SAMPLE


@pytest.mark.asyncio
async def test_ratio_sampler_is_deterministic() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    sampler = TraceIdRatioSampler(0.5)
    assert await sampler.should_sample(request) == await sampler.should_sample(request)


def test_ratio_sampler_serialization() -> None:
    assert TraceIdRatioSampler(0.25).to_dict() == {"ratio": 0.25}


def test_ratio_sampler_round_trip() -> None:
    sampler = TraceIdRatioSampler(0.25)
    assert TraceIdRatioSampler.from_dict(sampler.to_dict()) == sampler


@pytest.mark.asyncio
async def test_parent_sampler_delegates_root() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    assert (
        await ParentBasedSampler(AlwaysRecordSampler()).should_sample(request)
    ).decision is SamplingDecision.RECORD_ONLY


@pytest.mark.asyncio
async def test_parent_sampler_samples_sampled_parent() -> None:
    request = SamplingRequest(
        TraceId(),
        TraceContext.create_root(sampled=True),
        "work",
        TraceSource("test"),
        SpanKind.INTERNAL,
    )
    assert (await ParentBasedSampler(AlwaysOffSampler()).should_sample(request)).is_sampled


@pytest.mark.asyncio
async def test_parent_sampler_drops_unsampled_parent() -> None:
    request = SamplingRequest(
        TraceId(),
        TraceContext.create_root(sampled=False),
        "work",
        TraceSource("test"),
        SpanKind.INTERNAL,
    )
    assert not (await ParentBasedSampler(AlwaysOnSampler()).should_sample(request)).is_recording


def test_span_limits_defaults() -> None:
    assert SpanLimits() == SpanLimits(128, 128, 128)


def test_span_limits_rejects_zero_attribute_limit() -> None:
    with pytest.raises(SpanLimitError):
        SpanLimits(maximum_attributes=0)


def test_span_limits_rejects_boolean_limit() -> None:
    with pytest.raises(SpanLimitError):
        SpanLimits(maximum_events=True)


def test_span_limits_rejects_excessive_limit() -> None:
    with pytest.raises(SpanLimitError):
        SpanLimits(maximum_links=100_001)


def test_span_limits_serialization() -> None:
    assert SpanLimits(1, 2, 3).to_dict()["maximum_events"] == 2


def test_span_limits_round_trip() -> None:
    limits = SpanLimits(1, 2, 3)
    assert SpanLimits.from_dict(limits.to_dict()) == limits


@pytest.mark.asyncio
async def test_recording_span_initial_state() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    assert span.is_recording and not span.is_ended and span.context.sampled


@pytest.mark.asyncio
async def test_recording_span_set_attribute() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.set_attribute("Region", "eu")
    assert cast(SpanRecord, await span.end()).attributes == {"Region": "eu"}


@pytest.mark.asyncio
async def test_recording_span_replace_attribute_at_limit() -> None:
    span = await DefaultTracer(TraceSource("test"), limits=SpanLimits(1, 1, 1)).start_span(
        "work", attributes={"region": "eu"}
    )
    await span.set_attribute("region", "us")
    assert cast(SpanRecord, await span.end()).attributes == {"region": "us"}


@pytest.mark.asyncio
async def test_recording_span_rejects_new_attribute_over_limit() -> None:
    span = await DefaultTracer(TraceSource("test"), limits=SpanLimits(1, 1, 1)).start_span("work")
    await span.set_attribute("first", 1)
    with pytest.raises(SpanLimitError):
        await span.set_attribute("second", 2)
    assert cast(SpanRecord, await span.end()).attributes == {"first": 1}


@pytest.mark.asyncio
async def test_recording_span_set_attributes_is_atomic() -> None:
    span = await DefaultTracer(TraceSource("test"), limits=SpanLimits(1, 1, 1)).start_span("work")
    with pytest.raises(SpanLimitError):
        await span.set_attributes({"first": 1, "second": 2})
    assert cast(SpanRecord, await span.end()).attributes == {}


@pytest.mark.asyncio
async def test_recording_span_rejects_invalid_attribute_name() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    with pytest.raises(SpanValidationError):
        await span.set_attribute("bad\nname", 1)


@pytest.mark.asyncio
async def test_recording_span_rejects_attribute_after_end() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.end()
    with pytest.raises(SpanStateError):
        await span.set_attribute("value", 1)


@pytest.mark.asyncio
async def test_recording_span_attribute_input_isolation() -> None:
    value = {"nested": [1]}
    span = await DefaultTracer(TraceSource("test")).start_span("work", attributes=value)
    value["nested"].append(2)
    assert cast(SpanRecord, await span.end()).attributes == {"nested": (1,)}


@pytest.mark.asyncio
async def test_recording_span_add_event() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    event = await span.add_event("started", attributes={"phase": 1})
    assert event is not None and cast(SpanRecord, await span.end()).events == (event,)


@pytest.mark.asyncio
async def test_recording_span_event_uses_clock() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    span = await DefaultTracer(TraceSource("test"), clock=lambda: now).start_span("work")
    assert cast(SpanEvent, await span.add_event("started")).timestamp == now


@pytest.mark.asyncio
async def test_recording_span_rejects_event_before_start() -> None:
    start = datetime(2026, 1, 2, tzinfo=UTC)
    span = await DefaultTracer(TraceSource("test"), clock=lambda: start).start_span("work")
    with pytest.raises(SpanValidationError):
        await span.add_event("early", timestamp=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_recording_span_rejects_out_of_order_event() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    span = await DefaultTracer(TraceSource("test"), clock=lambda: start).start_span("work")
    await span.add_event("later", timestamp=start + timedelta(seconds=2))
    with pytest.raises(SpanValidationError):
        await span.add_event("earlier", timestamp=start + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_recording_span_allows_equal_event_timestamp() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    span = await DefaultTracer(TraceSource("test"), clock=lambda: now).start_span("work")
    first = await span.add_event("first", timestamp=now)
    second = await span.add_event("second", timestamp=now)
    assert cast(SpanRecord, await span.end()).events == (first, second)


@pytest.mark.asyncio
async def test_recording_span_event_limit() -> None:
    span = await DefaultTracer(TraceSource("test"), limits=SpanLimits(1, 1, 1)).start_span("work")
    await span.add_event("first")
    with pytest.raises(SpanLimitError):
        await span.add_event("second")


@pytest.mark.asyncio
async def test_recording_span_add_link() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    link = SpanLink(TraceContext.create_root())
    assert await span.add_link(link) and cast(SpanRecord, await span.end()).links == (link,)


@pytest.mark.asyncio
async def test_recording_span_rejects_self_link() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    with pytest.raises(SpanValidationError):
        await span.add_link(SpanLink(span.context))


@pytest.mark.asyncio
async def test_recording_span_rejects_duplicate_link() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    link = SpanLink(TraceContext.create_root())
    await span.add_link(link)
    with pytest.raises(SpanValidationError):
        await span.add_link(link)


@pytest.mark.asyncio
async def test_recording_span_link_limit() -> None:
    span = await DefaultTracer(TraceSource("test"), limits=SpanLimits(1, 1, 1)).start_span("work")
    await span.add_link(SpanLink(TraceContext.create_root()))
    with pytest.raises(SpanLimitError):
        await span.add_link(SpanLink(TraceContext.create_root()))


@pytest.mark.asyncio
async def test_recording_span_rejects_event_after_end() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.end()
    with pytest.raises(SpanStateError):
        await span.add_event("late")


@pytest.mark.asyncio
async def test_recording_span_rejects_link_after_end() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.end()
    with pytest.raises(SpanStateError):
        await span.add_link(SpanLink(TraceContext.create_root()))


@pytest.mark.asyncio
async def test_recording_span_set_status() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.set_status(SpanStatus.TIMEOUT, "slow")
    assert cast(SpanRecord, await span.end()).status is SpanStatus.TIMEOUT


@pytest.mark.asyncio
async def test_recording_span_rejects_wrong_status() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    with pytest.raises(SpanValidationError):
        await span.set_status(cast(SpanStatus, "ok"))


@pytest.mark.asyncio
async def test_recording_span_status_message_validation() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    with pytest.raises(SpanValidationError):
        await span.set_status(SpanStatus.ERROR, "bad\x01")


@pytest.mark.asyncio
async def test_recording_span_records_alios_error() -> None:
    from alios_core.errors import ValidationError

    span = await DefaultTracer(TraceSource("test")).start_span("work")
    exception = await span.record_exception(ValidationError("invalid", {"field": "name"}))
    assert exception is not None and cast(SpanRecord, await span.end()).exception == exception


@pytest.mark.asyncio
async def test_recording_span_records_ordinary_exception_safely() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    exception = await span.record_exception(ValueError("ACTIVE-SECRET"))
    assert exception is not None and "ACTIVE-SECRET" not in str(exception)


@pytest.mark.asyncio
async def test_recording_span_exception_sets_error_status() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.record_exception(ValueError("invalid"))
    assert cast(SpanRecord, await span.end()).status is SpanStatus.ERROR


@pytest.mark.asyncio
async def test_recording_span_rejects_second_exception() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.record_exception(ValueError("one"))
    with pytest.raises(SpanStateError):
        await span.record_exception(ValueError("two"))


@pytest.mark.asyncio
async def test_recording_span_cancelled_error_propagates() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    with pytest.raises(asyncio.CancelledError):
        await span.record_exception(asyncio.CancelledError())


@pytest.mark.asyncio
async def test_recording_span_keyboard_interrupt_propagates() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    with pytest.raises(KeyboardInterrupt):
        await span.record_exception(KeyboardInterrupt())


@pytest.mark.asyncio
async def test_recording_span_rejects_exception_after_end() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.end()
    with pytest.raises(SpanStateError):
        await span.record_exception(ValueError("late"))


@pytest.mark.asyncio
async def test_recording_span_end_creates_record() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    record = await span.end()
    assert isinstance(record, SpanRecord) and record.status is SpanStatus.UNSET


@pytest.mark.asyncio
async def test_recording_span_end_uses_clock() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    span = await DefaultTracer(TraceSource("test"), clock=lambda: now).start_span("work")
    assert cast(SpanRecord, await span.end()).ended_at == now


@pytest.mark.asyncio
async def test_recording_span_rejects_end_before_start() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    span = await DefaultTracer(TraceSource("test"), clock=lambda: now).start_span("work")
    with pytest.raises(SpanValidationError):
        await span.end(ended_at=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_recording_span_repeated_end_returns_same_record() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    assert await span.end() is await span.end()


@pytest.mark.asyncio
async def test_recording_span_concurrent_end_calls_handler_once() -> None:
    class Handler:
        def __init__(self) -> None:
            self.calls = 0

        async def on_end(self, record: SpanRecord) -> None:
            self.calls += 1

    handler = Handler()
    span = await DefaultTracer(TraceSource("test"), end_handler=handler).start_span("work")
    first, second = await asyncio.gather(span.end(), span.end())
    assert first is second and handler.calls == 1


@pytest.mark.asyncio
async def test_recording_span_handler_failure_marks_span_ended() -> None:
    class FailingHandler:
        async def on_end(self, record: SpanRecord) -> None:
            raise RuntimeError("handler failure")

    span = await DefaultTracer(TraceSource("test"), end_handler=FailingHandler()).start_span("work")
    with pytest.raises(SpanCompletionError):
        await span.end()
    assert span.is_ended


@pytest.mark.asyncio
async def test_recording_span_handler_failure_is_not_retried() -> None:
    class FailingHandler:
        def __init__(self) -> None:
            self.calls = 0

        async def on_end(self, record: SpanRecord) -> None:
            self.calls += 1
            raise RuntimeError("handler failure")

    handler = FailingHandler()
    span = await DefaultTracer(TraceSource("test"), end_handler=handler).start_span("work")
    with pytest.raises(SpanCompletionError):
        await span.end()
    assert await span.end() is not None and handler.calls == 1


@pytest.mark.asyncio
async def test_recording_span_mutation_after_end() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    await span.end()
    with pytest.raises(SpanStateError):
        await span.set_status(SpanStatus.OK)


@pytest.mark.asyncio
async def test_non_recording_span_context() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    assert not span.is_recording and not span.context.sampled


@pytest.mark.asyncio
async def test_non_recording_span_does_not_store_attributes() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    await span.set_attribute("region", "eu")
    assert await span.end() is None


@pytest.mark.asyncio
async def test_non_recording_span_add_event_returns_none() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    assert await span.add_event("event") is None


@pytest.mark.asyncio
async def test_non_recording_span_add_link_returns_false() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    assert not await span.add_link(SpanLink(TraceContext.create_root()))


@pytest.mark.asyncio
async def test_non_recording_span_does_not_inspect_ordinary_exception() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    assert await span.record_exception(ValueError("DROP-SECRET")) is None


@pytest.mark.asyncio
async def test_non_recording_span_process_control_exception_propagates() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    with pytest.raises(asyncio.CancelledError):
        await span.record_exception(asyncio.CancelledError())


@pytest.mark.asyncio
async def test_non_recording_span_end_returns_none() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    assert await span.end() is None


@pytest.mark.asyncio
async def test_non_recording_span_repeated_end() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    assert await span.end() is None and await span.end() is None


@pytest.mark.asyncio
async def test_non_recording_span_mutation_after_end() -> None:
    span = await DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler()).start_span("work")
    await span.end()
    with pytest.raises(SpanStateError):
        await span.set_attribute("late", 1)


@pytest.mark.asyncio
async def test_default_tracer_configuration() -> None:
    tracer = DefaultTracer(TraceSource("test"))
    assert tracer.source == TraceSource("test") and not (await tracer.status()).closed


@pytest.mark.asyncio
async def test_tracer_starts_root_span() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("root", root=True)
    assert span.parent_context is None


@pytest.mark.asyncio
async def test_tracer_uses_bound_parent() -> None:
    parent = _context()
    with bind_trace_context(parent):
        span = await DefaultTracer(TraceSource("test")).start_span("child")
    assert span.parent_context == parent and span.context.parent_span_id == parent.span_id


@pytest.mark.asyncio
async def test_tracer_explicit_parent_overrides_bound_context() -> None:
    bound, explicit = _context(), _context()
    with bind_trace_context(bound):
        span = await DefaultTracer(TraceSource("test")).start_span("child", parent=explicit)
    assert span.parent_context == explicit


@pytest.mark.asyncio
async def test_tracer_root_ignores_bound_context() -> None:
    with bind_trace_context(_context()):
        span = await DefaultTracer(TraceSource("test")).start_span("root", root=True)
    assert span.parent_context is None


@pytest.mark.asyncio
async def test_tracer_rejects_root_with_parent() -> None:
    with pytest.raises(TraceContextError):
        await DefaultTracer(TraceSource("test")).start_span("root", root=True, parent=_context())


@pytest.mark.asyncio
async def test_tracer_child_uses_parent_trace_id() -> None:
    parent = _context()
    span = await DefaultTracer(TraceSource("test")).start_span("child", parent=parent)
    assert span.context.trace_id == parent.trace_id


@pytest.mark.asyncio
async def test_tracer_explicit_root_trace_id() -> None:
    identifier = TraceId()
    span = await DefaultTracer(TraceSource("test")).start_span("root", trace_id=identifier)
    assert span.context.trace_id == identifier


@pytest.mark.asyncio
async def test_tracer_rejects_child_trace_id() -> None:
    with pytest.raises(TraceContextError):
        await DefaultTracer(TraceSource("test")).start_span(
            "child", parent=_context(), trace_id=TraceId()
        )


@pytest.mark.asyncio
async def test_tracer_rejects_span_id_equal_to_parent() -> None:
    parent = _context()
    with pytest.raises(TraceContextError):
        await DefaultTracer(TraceSource("test")).start_span(
            "child", parent=parent, span_id=parent.span_id
        )


def test_span_record_duration_ns_is_exact_for_one_microsecond() -> None:
    now = datetime.now(UTC)
    record = SpanRecord(
        _context(),
        "x",
        TraceSource("x"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now + timedelta(microseconds=1),
    )
    assert record.duration_ns == 1_000


def test_span_record_duration_ns_is_exact_for_multi_day_duration() -> None:
    now = datetime.now(UTC)
    record = SpanRecord(
        _context(),
        "x",
        TraceSource("x"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now + timedelta(days=3, microseconds=1),
    )
    assert record.duration_ns == 259_200_000_001_000


def test_span_record_from_dict_does_not_treat_empty_exception_as_none() -> None:
    value = _record().to_dict()
    value["exception"] = {}
    with pytest.raises(TraceSerializationError):
        SpanRecord.from_dict(value)


def test_trace_text_rejects_delete_control_character() -> None:
    with pytest.raises(SpanValidationError):
        TraceSource("runtime\x7f")


def test_trace_baggage_rejects_delete_control_character() -> None:
    with pytest.raises(TraceContextError):
        TraceContext.create_root(baggage={"key": "bad\x7f"})


def test_sampling_request_from_dict_accepts_none_parent() -> None:
    request = SamplingRequest(TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL)
    rendered = request.to_dict(RedactionPolicy(include_default_rules=False))
    assert SamplingRequest.from_dict(rendered).parent_context is None


def test_sampling_request_from_dict_restores_explicit_parent() -> None:
    parent = TraceContext.create_root()
    request = SamplingRequest(TraceId(), parent, "work", TraceSource("test"), SpanKind.INTERNAL)
    rendered = request.to_dict(RedactionPolicy(include_default_rules=False))
    assert SamplingRequest.from_dict(rendered).parent_context == parent


def test_sampling_request_from_dict_rejects_empty_parent_mapping() -> None:
    rendered = SamplingRequest(
        TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL
    ).to_dict()
    cast(dict[str, object], rendered)["parent_context"] = {}
    with pytest.raises(TraceSerializationError):
        SamplingRequest.from_dict(rendered)


def test_sampling_request_from_dict_rejects_malformed_parent_mapping() -> None:
    rendered = SamplingRequest(
        TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL
    ).to_dict()
    cast(dict[str, object], rendered)["parent_context"] = {"trace_id": "bad"}
    with pytest.raises(TraceSerializationError):
        SamplingRequest.from_dict(rendered)


def test_sampling_request_from_dict_rejects_non_mapping_parent() -> None:
    rendered = SamplingRequest(
        TraceId(), None, "work", TraceSource("test"), SpanKind.INTERNAL
    ).to_dict()
    cast(dict[str, object], rendered)["parent_context"] = "parent"
    with pytest.raises(TraceSerializationError):
        SamplingRequest.from_dict(rendered)


@pytest.mark.asyncio
async def test_drop_span_allows_initial_attributes_above_recording_limit() -> None:
    tracer = DefaultTracer(
        TraceSource("test"), sampler=AlwaysOffSampler(), limits=SpanLimits(1, 1, 1)
    )
    assert not (await tracer.start_span("drop", attributes={"first": 1, "second": 2})).is_recording


@pytest.mark.asyncio
async def test_drop_span_allows_initial_links_above_recording_limit() -> None:
    tracer = DefaultTracer(
        TraceSource("test"), sampler=AlwaysOffSampler(), limits=SpanLimits(1, 1, 1)
    )
    links = (SpanLink(TraceContext.create_root()), SpanLink(TraceContext.create_root()))
    assert not (await tracer.start_span("drop", links=links)).is_recording


@pytest.mark.asyncio
async def test_drop_span_above_limits_updates_started_and_dropped_counts() -> None:
    tracer = DefaultTracer(
        TraceSource("test"), sampler=AlwaysOffSampler(), limits=SpanLimits(1, 1, 1)
    )
    span = await tracer.start_span("drop", attributes={"first": 1, "second": 2})
    before_end = await tracer.status()
    await span.end()
    assert (before_end.started_count, before_end.dropped_count, before_end.active_count) == (
        1,
        1,
        1,
    )
    assert (await tracer.status()).active_count == 0


@pytest.mark.asyncio
async def test_drop_span_above_limits_does_not_invoke_handler() -> None:
    class Handler:
        def __init__(self) -> None:
            self.calls = 0

        async def on_end(self, record: SpanRecord) -> None:
            self.calls += 1

    handler = Handler()
    tracer = DefaultTracer(
        TraceSource("test"),
        sampler=AlwaysOffSampler(),
        end_handler=handler,
        limits=SpanLimits(1, 1, 1),
    )
    await (await tracer.start_span("drop", attributes={"first": 1, "second": 2})).end()
    assert handler.calls == 0


@pytest.mark.asyncio
async def test_record_only_span_enforces_initial_attribute_limit() -> None:
    tracer = DefaultTracer(
        TraceSource("test"), sampler=AlwaysRecordSampler(), limits=SpanLimits(1, 1, 1)
    )
    with pytest.raises(SpanLimitError):
        await tracer.start_span("record", attributes={"first": 1, "second": 2})


@pytest.mark.asyncio
async def test_record_only_span_enforces_initial_link_limit() -> None:
    tracer = DefaultTracer(
        TraceSource("test"), sampler=AlwaysRecordSampler(), limits=SpanLimits(1, 1, 1)
    )
    links = (SpanLink(TraceContext.create_root()), SpanLink(TraceContext.create_root()))
    with pytest.raises(SpanLimitError):
        await tracer.start_span("record", links=links)


@pytest.mark.asyncio
async def test_sampled_span_enforces_initial_attribute_limit() -> None:
    tracer = DefaultTracer(
        TraceSource("test"), sampler=AlwaysOnSampler(), limits=SpanLimits(1, 1, 1)
    )
    with pytest.raises(SpanLimitError):
        await tracer.start_span("sample", attributes={"first": 1, "second": 2})


@pytest.mark.asyncio
async def test_sampled_span_enforces_initial_link_limit() -> None:
    tracer = DefaultTracer(
        TraceSource("test"), sampler=AlwaysOnSampler(), limits=SpanLimits(1, 1, 1)
    )
    links = (SpanLink(TraceContext.create_root()), SpanLink(TraceContext.create_root()))
    with pytest.raises(SpanLimitError):
        await tracer.start_span("sample", links=links)


@pytest.mark.asyncio
async def test_recording_limit_failure_does_not_change_tracer_status() -> None:
    tracer = DefaultTracer(TraceSource("test"), limits=SpanLimits(1, 1, 1))
    with pytest.raises(SpanLimitError):
        await tracer.start_span("sample", attributes={"first": 1, "second": 2})
    assert (await tracer.status()).started_count == 0


@pytest.mark.asyncio
async def test_recording_span_set_attributes_rejects_normalized_name_collision() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work")
    with pytest.raises(SpanValidationError):
        await span.set_attributes({"method": "GET", " method ": "POST"})


@pytest.mark.asyncio
async def test_recording_span_collision_failure_is_atomic() -> None:
    span = await DefaultTracer(TraceSource("test")).start_span("work", attributes={"region": "eu"})
    with pytest.raises(SpanValidationError):
        await span.set_attributes({"method": "GET", " method ": "POST"})
    assert cast(SpanRecord, await span.end()).attributes == {"region": "eu"}


@pytest.mark.asyncio
async def test_tracer_initial_attributes_reject_normalized_name_collision() -> None:
    with pytest.raises(SpanValidationError):
        await DefaultTracer(TraceSource("test")).start_span(
            "work", attributes={"method": "GET", " method ": "POST"}
        )


@pytest.mark.asyncio
async def test_tracer_initial_attribute_collision_does_not_change_status() -> None:
    tracer = DefaultTracer(TraceSource("test"))
    with pytest.raises(SpanValidationError):
        await tracer.start_span("work", attributes={"method": "GET", " method ": "POST"})
    assert (await tracer.status()).started_count == 0


def test_sampler_attributes_may_override_initial_attributes() -> None:
    result = SamplingResult(SamplingDecision.RECORD_AND_SAMPLE, {"method": "POST"})
    assert {**{"method": "GET"}, **result.attributes}["method"] == "POST"


def test_sampler_attribute_mapping_rejects_internal_normalized_collision() -> None:
    with pytest.raises(SpanValidationError):
        SamplingResult(SamplingDecision.RECORD_AND_SAMPLE, {"method": "GET", " method ": "POST"})


@pytest.mark.asyncio
async def test_non_recording_span_add_event_uses_injected_clock() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values = [now, now, now]
    tracer = DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler(), clock=values.pop)
    span = await tracer.start_span("drop")
    assert await span.add_event("event") is None and not values


@pytest.mark.asyncio
async def test_non_recording_span_add_event_explicit_timestamp_skips_clock() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values = [now, now]
    tracer = DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler(), clock=values.pop)
    span = await tracer.start_span("drop")
    assert await span.add_event("event", timestamp=now) is None and not values


@pytest.mark.asyncio
async def test_non_recording_span_add_event_rejects_naive_injected_clock() -> None:
    aware = datetime(2026, 1, 1, tzinfo=UTC)
    values = [datetime(2026, 1, 1), aware, aware]
    tracer = DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler(), clock=values.pop)
    span = await tracer.start_span("drop")
    with pytest.raises(SpanValidationError):
        await span.add_event("event")


@pytest.mark.asyncio
async def test_non_recording_event_clock_failure_does_not_end_span() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values = [now, now]

    def clock() -> datetime:
        if values:
            return values.pop()
        raise RuntimeError("clock failure")

    span = await DefaultTracer(
        TraceSource("test"), sampler=AlwaysOffSampler(), clock=clock
    ).start_span("drop")
    with pytest.raises(RuntimeError):
        await span.add_event("event")
    assert not span.is_ended


@pytest.mark.asyncio
async def test_non_recording_event_clock_failure_does_not_change_tracer_status() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    values = [now, now]

    def clock() -> datetime:
        if values:
            return values.pop()
        raise RuntimeError("clock failure")

    tracer = DefaultTracer(TraceSource("test"), sampler=AlwaysOffSampler(), clock=clock)
    span = await tracer.start_span("drop")
    with pytest.raises(RuntimeError):
        await span.add_event("event")
    assert (await tracer.status()).active_count == 1


def test_span_filter_defaults() -> None:
    value = SpanFilter()
    assert value.trace_ids is None and value.limit is None and value.offset == 0


def test_span_filter_trace_ids() -> None:
    identifier = TraceId()
    assert SpanFilter(trace_ids=frozenset({identifier})).trace_ids == frozenset({identifier})


def test_span_filter_span_ids() -> None:
    identifier = SpanId()
    assert SpanFilter(span_ids=frozenset({identifier})).span_ids == frozenset({identifier})


def test_span_filter_parent_span_ids() -> None:
    identifier = SpanId()
    assert SpanFilter(parent_span_ids=frozenset({identifier})).parent_span_ids == frozenset(
        {identifier}
    )


def test_span_filter_scope_identifiers() -> None:
    run, tenant, user = RunId(), TenantId(), UserId()
    value = SpanFilter(
        run_ids=frozenset({run}), tenant_ids=frozenset({tenant}), user_ids=frozenset({user})
    )
    assert (value.run_ids, value.tenant_ids, value.user_ids) == (
        frozenset({run}),
        frozenset({tenant}),
        frozenset({user}),
    )


def test_span_filter_names() -> None:
    assert SpanFilter(names=frozenset({" operation "})).names == frozenset({"operation"})


def test_span_filter_source_fields() -> None:
    value = SpanFilter(source_components=frozenset({" api "}), source_modules=frozenset({" web "}))
    assert value.source_components == frozenset({"api"}) and value.source_modules == frozenset(
        {"web"}
    )


def test_span_filter_kinds() -> None:
    assert SpanFilter(kinds=frozenset({SpanKind.CLIENT})).kinds == frozenset({SpanKind.CLIENT})


def test_span_filter_statuses() -> None:
    assert SpanFilter(statuses=frozenset({SpanStatus.ERROR})).statuses == frozenset(
        {SpanStatus.ERROR}
    )


def test_span_filter_boolean_predicates() -> None:
    value = SpanFilter(sampled=False, has_exception=True, root_only=False)
    assert value.sampled is False and value.has_exception is True and value.root_only is False


def test_span_filter_attribute_subset() -> None:
    assert SpanFilter(attribute_equals={" region ": "eu"}).attribute_equals == {"region": "eu"}


def test_span_filter_time_ranges() -> None:
    now = datetime.now(UTC)
    assert (
        SpanFilter(started_after=now, started_before=now + timedelta(seconds=1)).started_after
        == now
    )


def test_span_filter_duration_range() -> None:
    value = SpanFilter(minimum_duration_ns=1, maximum_duration_ns=2)
    assert (value.minimum_duration_ns, value.maximum_duration_ns) == (1, 2)


def test_span_filter_accepts_limit_zero() -> None:
    assert SpanFilter(limit=0).limit == 0


def test_span_filter_rejects_wrong_trace_id() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(trace_ids=cast(frozenset[TraceId], frozenset({SpanId()})))


def test_span_filter_rejects_wrong_span_id() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(span_ids=cast(frozenset[SpanId], frozenset({TraceId()})))


def test_span_filter_rejects_wrong_scope_identifier() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(run_ids=cast(frozenset[RunId], frozenset({TenantId()})))


def test_span_filter_rejects_wrong_kind() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(kinds=cast(frozenset[SpanKind], frozenset({"client"})))


def test_span_filter_rejects_wrong_status() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(statuses=cast(frozenset[SpanStatus], frozenset({"ok"})))


def test_span_filter_rejects_non_boolean_sampled() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(sampled=cast(bool, 1))


def test_span_filter_rejects_non_boolean_exception_flag() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(has_exception=cast(bool, "yes"))


def test_span_filter_rejects_non_boolean_root_only() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(root_only=cast(bool, 0))


def test_span_filter_rejects_naive_started_after() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(started_after=datetime.now())


def test_span_filter_rejects_naive_ended_before() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(ended_before=datetime.now())


def test_span_filter_rejects_inverted_start_range() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanRepositoryError):
        SpanFilter(started_after=now, started_before=now)


def test_span_filter_rejects_inverted_end_range() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanRepositoryError):
        SpanFilter(ended_after=now + timedelta(seconds=1), ended_before=now)


def test_span_filter_rejects_negative_duration() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(minimum_duration_ns=-1)


def test_span_filter_rejects_boolean_duration() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(maximum_duration_ns=True)


def test_span_filter_rejects_inverted_duration_range() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(minimum_duration_ns=2, maximum_duration_ns=1)


def test_span_filter_rejects_negative_limit() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(limit=-1)


def test_span_filter_rejects_boolean_limit() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(limit=True)


def test_span_filter_rejects_negative_offset() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(offset=-1)


def test_span_filter_rejects_boolean_offset() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(offset=True)


def test_span_filter_rejects_normalized_attribute_collision() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter(attribute_equals={"key": 1, " key ": 2})


def test_span_filter_attributes_are_immutable() -> None:
    values = SpanFilter(attribute_equals={"nested": [1]}).attribute_equals
    with pytest.raises(TypeError):
        cast(dict[str, object], values)["nested"] = [2]


def test_span_filter_matches_trace_id() -> None:
    record = _record()
    assert SpanFilter(trace_ids=frozenset({record.context.trace_id})).matches(record)


def test_span_filter_matches_span_id() -> None:
    record = _record()
    assert SpanFilter(span_ids=frozenset({record.context.span_id})).matches(record)


def test_span_filter_matches_parent_span() -> None:
    record = _record()
    assert not SpanFilter(parent_span_ids=frozenset({SpanId()})).matches(record)


def test_span_filter_matches_root_only() -> None:
    assert SpanFilter(root_only=True).matches(_record())


def test_span_filter_matches_non_root_only() -> None:
    assert not SpanFilter(root_only=False).matches(_record())


def test_span_filter_matches_name() -> None:
    assert SpanFilter(names=frozenset({"operation"})).matches(_record())


def test_span_filter_matches_source() -> None:
    assert SpanFilter(source_components=frozenset({"component"})).matches(_record())


def test_span_filter_matches_kind() -> None:
    assert SpanFilter(kinds=frozenset({SpanKind.INTERNAL})).matches(_record())


def test_span_filter_matches_status() -> None:
    assert SpanFilter(statuses=frozenset({SpanStatus.OK})).matches(_record())


def test_span_filter_matches_sampled() -> None:
    assert SpanFilter(sampled=True).matches(_record())


def test_span_filter_matches_exception_presence() -> None:
    assert SpanFilter(has_exception=False).matches(_record())


def test_span_filter_matches_attribute_subset() -> None:
    assert SpanFilter(attribute_equals={}).matches(_record())


def test_span_filter_rejects_missing_attribute() -> None:
    assert not SpanFilter(attribute_equals={"missing": 1}).matches(_record())


def test_span_filter_matches_start_time() -> None:
    record = _record()
    assert SpanFilter(started_after=record.started_at - timedelta(microseconds=1)).matches(record)


def test_span_filter_matches_end_time() -> None:
    record = _record()
    assert SpanFilter(ended_before=record.ended_at + timedelta(microseconds=1)).matches(record)


def test_span_filter_matches_duration() -> None:
    assert SpanFilter(minimum_duration_ns=1_000_000_000).matches(_record())


def test_span_filter_combines_predicates() -> None:
    assert (
        SpanFilter(names=frozenset({"operation"}), statuses=frozenset({SpanStatus.ERROR})).matches(
            _record()
        )
        is False
    )


def test_span_filter_rejects_unknown_record_type() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter().matches(cast(SpanRecord, object()))


def test_span_filter_serialization() -> None:
    assert SpanFilter(names=frozenset({"b", "a"})).to_dict()["names"] == ["a", "b"]


def test_span_filter_round_trip() -> None:
    value = SpanFilter(trace_ids=frozenset({TraceId()}), names=frozenset({"run"}), limit=3)
    assert (
        SpanFilter.from_dict(value.to_dict(RedactionPolicy(include_default_rules=False))) == value
    )


def test_span_filter_serialization_is_independent() -> None:
    rendered = SpanFilter(names=frozenset({"run"})).to_dict()
    cast(list[str], rendered["names"]).append("other")
    assert SpanFilter(names=frozenset({"run"})).names == frozenset({"run"})


def test_span_filter_rendering_redacts_attributes() -> None:
    rendered = SpanFilter(attribute_equals={"password": "secret"}).to_dict()
    assert "secret" not in repr(rendered)


def test_span_filter_from_dict_uses_strict_types() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter.from_dict({"limit": "1"})


def test_span_filter_from_dict_rejects_invalid_identifier() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter.from_dict({"trace_ids": ["invalid"]})


def test_span_filter_from_dict_rejects_unknown_enum() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanFilter.from_dict({"kinds": ["unknown"]})


def test_span_repository_status_valid() -> None:
    assert SpanRepositoryStatus(0, 1, False, datetime.now(UTC)).record_count == 0


def test_span_repository_status_rejects_negative_count() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanRepositoryStatus(-1, 1, False, datetime.now(UTC))


def test_span_repository_status_rejects_boolean_count() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanRepositoryStatus(True, 1, False, datetime.now(UTC))


def test_span_repository_status_rejects_count_over_capacity() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanRepositoryStatus(2, 1, False, datetime.now(UTC))


def test_span_repository_status_rejects_naive_timestamp() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanRepositoryStatus(0, 1, False, datetime.now())


def test_span_repository_status_serialization() -> None:
    assert SpanRepositoryStatus(0, 1, False, datetime.now(UTC)).to_dict()["closed"] is False


def test_span_repository_status_round_trip() -> None:
    status = SpanRepositoryStatus(1, 2, True, datetime.now(UTC))
    assert SpanRepositoryStatus.from_dict(status.to_dict()) == status


def test_span_repository_snapshot_empty() -> None:
    now = datetime.now(UTC)
    assert SpanRepositorySnapshot(SpanRepositoryStatus(0, 1, False, now), (), now).records == ()


def test_span_repository_snapshot_populated() -> None:
    record = _record()
    status = SpanRepositoryStatus(1, 2, False, record.started_at)
    assert SpanRepositorySnapshot(status, (record,), record.ended_at).records == (record,)


def test_span_repository_snapshot_rejects_wrong_status() -> None:
    with pytest.raises(SpanRepositoryError):
        SpanRepositorySnapshot(cast(SpanRepositoryStatus, object()), (), datetime.now(UTC))


def test_span_repository_snapshot_rejects_wrong_record() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanRepositoryError):
        SpanRepositorySnapshot(
            SpanRepositoryStatus(1, 2, False, now), cast(tuple[SpanRecord, ...], (object(),)), now
        )


def test_span_repository_snapshot_rejects_unsorted_records() -> None:
    first = _record()
    second = SpanRecord(
        TraceContext.create_root(),
        "two",
        first.source,
        first.kind,
        first.status,
        first.started_at,
        first.ended_at - timedelta(microseconds=1),
    )
    with pytest.raises(SpanRepositoryError):
        SpanRepositorySnapshot(
            SpanRepositoryStatus(2, 2, False, first.started_at), (first, second), first.ended_at
        )


def test_span_repository_snapshot_rejects_duplicate_record_key() -> None:
    record = _record()
    with pytest.raises(SpanRepositoryError):
        SpanRepositorySnapshot(
            SpanRepositoryStatus(2, 2, False, record.started_at), (record, record), record.ended_at
        )


def test_span_repository_snapshot_rejects_naive_time() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanRepositoryError):
        SpanRepositorySnapshot(SpanRepositoryStatus(0, 1, False, now), (), datetime.now())


def test_span_repository_snapshot_rejects_time_before_creation() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanRepositoryError):
        SpanRepositorySnapshot(
            SpanRepositoryStatus(0, 1, False, now), (), now - timedelta(seconds=1)
        )


def test_unfiltered_span_snapshot_requires_full_record_count() -> None:
    now = datetime.now(UTC)
    with pytest.raises(SpanRepositoryError):
        SpanRepositorySnapshot(SpanRepositoryStatus(1, 1, False, now), (), now)


def test_filtered_span_snapshot_allows_partial_records() -> None:
    now = datetime.now(UTC)
    assert SpanRepositorySnapshot(SpanRepositoryStatus(1, 1, False, now), (), now, True).filtered


def test_span_repository_snapshot_serialization() -> None:
    now = datetime.now(UTC)
    value = SpanRepositorySnapshot(SpanRepositoryStatus(0, 1, False, now), (), now)
    assert value.to_dict()["records"] == []


def test_span_repository_snapshot_round_trip() -> None:
    record = _record()
    value = SpanRepositorySnapshot(
        SpanRepositoryStatus(1, 1, False, record.started_at), (record,), record.ended_at
    )
    rendered = value.to_dict(RedactionPolicy(include_default_rules=False))
    assert SpanRepositorySnapshot.from_dict(rendered) == value


@pytest.mark.asyncio
async def test_span_repository_default_construction() -> None:
    assert (await InMemorySpanRepository().status()).maximum_records == 10_000


def test_span_repository_rejects_zero_capacity() -> None:
    with pytest.raises(SpanRepositoryError):
        InMemorySpanRepository(maximum_records=0)


def test_span_repository_rejects_boolean_capacity() -> None:
    with pytest.raises(SpanRepositoryError):
        InMemorySpanRepository(maximum_records=True)


def test_span_repository_rejects_excessive_capacity() -> None:
    with pytest.raises(SpanRepositoryError):
        InMemorySpanRepository(maximum_records=1_000_001)


def test_span_repository_rejects_naive_constructor_clock() -> None:
    with pytest.raises(SpanRepositoryError):
        InMemorySpanRepository(clock=datetime.now)


@pytest.mark.asyncio
async def test_span_repository_instances_are_isolated() -> None:
    first, second = InMemorySpanRepository(), InMemorySpanRepository()
    await first.add(_record())
    assert await second.count() == 0


@pytest.mark.asyncio
async def test_span_repository_add() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_span_repository_add_preserves_record_identity() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    assert await repository.get(record.context.trace_id, record.context.span_id) is record


@pytest.mark.asyncio
async def test_span_repository_rejects_wrong_record_type() -> None:
    with pytest.raises(SpanRepositoryError):
        await InMemorySpanRepository().add(cast(SpanRecord, object()))


@pytest.mark.asyncio
async def test_span_repository_rejects_duplicate_record() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    with pytest.raises(ResourceConflictError):
        await repository.add(record)


@pytest.mark.asyncio
async def test_span_repository_capacity_limit() -> None:
    repository = InMemorySpanRepository(maximum_records=1)
    await repository.add(_record())
    with pytest.raises(SpanRepositoryCapacityError):
        await repository.add(_record())


@pytest.mark.asyncio
async def test_span_repository_capacity_failure_is_atomic() -> None:
    repository, first = InMemorySpanRepository(maximum_records=1), _record()
    await repository.add(first)
    with pytest.raises(SpanRepositoryCapacityError):
        await repository.add(_record())
    assert await repository.list() == (first,)


@pytest.mark.asyncio
async def test_span_repository_get() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    assert await repository.get(record.context.trace_id, record.context.span_id) == record


@pytest.mark.asyncio
async def test_span_repository_get_optional_existing() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    assert await repository.get_optional(record.context.trace_id, record.context.span_id) is record


@pytest.mark.asyncio
async def test_span_repository_get_optional_missing() -> None:
    assert await InMemorySpanRepository().get_optional(TraceId(), SpanId()) is None


@pytest.mark.asyncio
async def test_span_repository_get_missing() -> None:
    with pytest.raises(ResourceNotFoundError):
        await InMemorySpanRepository().get(TraceId(), SpanId())


@pytest.mark.asyncio
async def test_span_repository_empty_list() -> None:
    assert await InMemorySpanRepository().list() == ()


@pytest.mark.asyncio
async def test_span_repository_list_all() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    assert await repository.list() == (record,)


@pytest.mark.asyncio
async def test_span_repository_list_filters_records() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    assert await repository.list(SpanFilter(names=frozenset({"missing"}))) == ()


@pytest.mark.asyncio
async def test_span_repository_list_limit_zero() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    assert await repository.list(SpanFilter(limit=0)) == ()


@pytest.mark.asyncio
async def test_span_repository_count_all() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_span_repository_count_ignores_limit() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    assert await repository.count(SpanFilter(limit=0)) == 1


@pytest.mark.asyncio
async def test_span_repository_remove_existing() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    assert await repository.remove(record.context.trace_id, record.context.span_id)


@pytest.mark.asyncio
async def test_span_repository_remove_missing() -> None:
    assert not await InMemorySpanRepository().remove(TraceId(), SpanId())


@pytest.mark.asyncio
async def test_span_repository_reset_all() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    assert await repository.reset() == 1 and await repository.count() == 0


@pytest.mark.asyncio
async def test_span_repository_reset_filtered() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    assert await repository.reset(SpanFilter(names=frozenset({record.name}))) == 1


@pytest.mark.asyncio
async def test_span_repository_close() -> None:
    repository = InMemorySpanRepository()
    await repository.close()
    assert (await repository.status()).closed


@pytest.mark.asyncio
async def test_span_repository_repeated_close() -> None:
    repository = InMemorySpanRepository()
    await repository.close()
    await repository.close()
    assert (await repository.status()).record_count == 0


@pytest.mark.asyncio
async def test_span_repository_add_after_close() -> None:
    repository = InMemorySpanRepository()
    await repository.close()
    with pytest.raises(SpanRepositoryClosedError):
        await repository.add(_record())


def test_span_processor_status_valid() -> None:
    assert SpanProcessorStatus(1, 1, 0, 0, False, datetime.now(UTC)).processed_count == 1


def test_span_processor_status_rejects_invalid_invariant() -> None:
    with pytest.raises(SpanProcessorError):
        SpanProcessorStatus(1, 0, 0, 0, False, datetime.now(UTC))


def test_span_processor_status_rejects_boolean_count() -> None:
    with pytest.raises(SpanProcessorError):
        SpanProcessorStatus(True, 1, 0, 0, False, datetime.now(UTC))


def test_span_processor_status_serialization() -> None:
    assert (
        SpanProcessorStatus(0, 0, 0, 0, False, datetime.now(UTC)).to_dict()["received_count"] == 0
    )


def test_span_processor_status_round_trip() -> None:
    value = SpanProcessorStatus(1, 0, 1, 0, True, datetime.now(UTC))
    assert SpanProcessorStatus.from_dict(value.to_dict()) == value


@pytest.mark.asyncio
async def test_simple_span_processor_initial_status() -> None:
    assert (await SimpleSpanProcessor(InMemorySpanRepository()).status()).received_count == 0


@pytest.mark.asyncio
async def test_simple_span_processor_processes_record() -> None:
    repository, processor, record = InMemorySpanRepository(), None, _record()
    processor = SimpleSpanProcessor(repository)
    await processor.on_end(record)
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_simple_span_processor_rejects_wrong_record() -> None:
    processor = SimpleSpanProcessor(InMemorySpanRepository())
    with pytest.raises(SpanProcessorError):
        await processor.on_end(cast(SpanRecord, object()))


@pytest.mark.asyncio
async def test_simple_span_processor_wraps_repository_failure() -> None:
    repository, record = InMemorySpanRepository(maximum_records=1), _record()
    await repository.add(record)
    with pytest.raises(SpanProcessorError):
        await SimpleSpanProcessor(repository).on_end(_record())


@pytest.mark.asyncio
async def test_simple_span_processor_status_tracks_success() -> None:
    processor = SimpleSpanProcessor(InMemorySpanRepository())
    await processor.on_end(_record())
    assert (await processor.status()).processed_count == 1


@pytest.mark.asyncio
async def test_simple_span_processor_force_flush() -> None:
    processor = SimpleSpanProcessor(InMemorySpanRepository())
    await processor.force_flush()
    assert (await processor.status()).in_flight_count == 0


@pytest.mark.asyncio
async def test_simple_span_processor_close() -> None:
    processor = SimpleSpanProcessor(InMemorySpanRepository())
    await processor.close()
    assert (await processor.status()).closed


@pytest.mark.asyncio
async def test_simple_span_processor_repeated_close() -> None:
    processor = SimpleSpanProcessor(InMemorySpanRepository())
    await processor.close()
    await processor.close()
    assert (await processor.status()).closed is True


@pytest.mark.asyncio
async def test_simple_span_processor_rejects_after_close() -> None:
    processor = SimpleSpanProcessor(InMemorySpanRepository())
    await processor.close()
    with pytest.raises(SpanProcessorClosedError):
        await processor.on_end(_record())


@pytest.mark.asyncio
async def test_simple_span_processor_external_repository_remains_open() -> None:
    repository, processor = InMemorySpanRepository(), None
    processor = SimpleSpanProcessor(repository)
    await processor.close()
    assert not (await repository.status()).closed


@pytest.mark.asyncio
async def test_simple_span_processor_owned_repository_closes() -> None:
    repository = InMemorySpanRepository()
    await SimpleSpanProcessor(repository, close_repository=True).close()
    assert (await repository.status()).closed


@pytest.mark.asyncio
async def test_simple_span_processor_force_flush_after_close() -> None:
    processor = SimpleSpanProcessor(InMemorySpanRepository())
    await processor.close()
    await processor.force_flush()
    assert (await processor.status()).in_flight_count == 0


def test_span_filter_matches_scope_identifiers() -> None:
    now, correlation, run = datetime.now(UTC), CorrelationId(), RunId()
    context = TraceContext.create_root(correlation_id=correlation, run_id=run)
    record = SpanRecord(
        context, "scope", TraceSource("unit"), SpanKind.INTERNAL, SpanStatus.OK, now, now
    )
    assert SpanFilter(correlation_ids=frozenset({correlation}), run_ids=frozenset({run})).matches(
        record
    )


@pytest.mark.asyncio
async def test_span_repository_concurrent_final_slot() -> None:
    repository = InMemorySpanRepository(maximum_records=1)
    results = await asyncio.gather(
        repository.add(_record()), repository.add(_record()), return_exceptions=True
    )
    assert sum(result is None for result in results) == 1


@pytest.mark.asyncio
async def test_span_repository_error_omits_record_values() -> None:
    secret, repository = "UNIT-REPOSITORY-SECRET", InMemorySpanRepository(maximum_records=1)
    await repository.add(_record())
    with pytest.raises(SpanRepositoryCapacityError) as captured:
        await repository.add(
            SpanRecord(
                TraceContext.create_root(),
                secret,
                TraceSource("unit"),
                SpanKind.INTERNAL,
                SpanStatus.OK,
                datetime.now(UTC),
                datetime.now(UTC),
            )
        )
    assert secret not in repr(captured.value.to_dict())


@pytest.mark.asyncio
async def test_span_repository_get_rejects_wrong_trace_id() -> None:
    with pytest.raises(SpanRepositoryError):
        await InMemorySpanRepository().get(cast(TraceId, SpanId()), SpanId())


@pytest.mark.asyncio
async def test_span_repository_get_rejects_wrong_span_id() -> None:
    with pytest.raises(SpanRepositoryError):
        await InMemorySpanRepository().get(TraceId(), cast(SpanId, TraceId()))


@pytest.mark.asyncio
async def test_span_repository_list_canonical_order() -> None:
    repository, now = InMemorySpanRepository(), datetime.now(UTC)
    later = SpanRecord(
        TraceContext.create_root(),
        "later",
        TraceSource("unit"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now + timedelta(seconds=2),
    )
    earlier = SpanRecord(
        TraceContext.create_root(),
        "earlier",
        TraceSource("unit"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now + timedelta(seconds=1),
    )
    await repository.add(later)
    await repository.add(earlier)
    assert await repository.list() == (earlier, later)


@pytest.mark.asyncio
async def test_span_repository_list_applies_offset_after_sort() -> None:
    repository, now = InMemorySpanRepository(), datetime.now(UTC)
    first = SpanRecord(
        TraceContext.create_root(),
        "first",
        TraceSource("unit"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now,
    )
    second = SpanRecord(
        TraceContext.create_root(),
        "second",
        TraceSource("unit"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now + timedelta(seconds=1),
    )
    await repository.add(second)
    await repository.add(first)
    assert await repository.list(SpanFilter(offset=1)) == (second,)


@pytest.mark.asyncio
async def test_span_repository_list_applies_limit_after_offset() -> None:
    repository, now = InMemorySpanRepository(), datetime.now(UTC)
    records = tuple(
        SpanRecord(
            TraceContext.create_root(),
            name,
            TraceSource("unit"),
            SpanKind.INTERNAL,
            SpanStatus.OK,
            now,
            now + timedelta(seconds=index),
        )
        for index, name in enumerate(("zero", "one", "two"))
    )
    for record in records:
        await repository.add(record)
    assert await repository.list(SpanFilter(offset=1, limit=1)) == (records[1],)


@pytest.mark.asyncio
async def test_span_repository_count_filters_records() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    assert await repository.count(SpanFilter(names=frozenset({"absent"}))) == 0


@pytest.mark.asyncio
async def test_span_repository_count_ignores_offset() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    assert await repository.count(SpanFilter(offset=5)) == 1


@pytest.mark.asyncio
async def test_span_repository_query_returns_immutable_tuple() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    assert isinstance(await repository.list(), tuple)


@pytest.mark.asyncio
async def test_span_repository_snapshot_complete() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    snapshot = await repository.snapshot()
    assert len(snapshot.records) == snapshot.status.record_count == 1 and not snapshot.filtered


@pytest.mark.asyncio
async def test_span_repository_snapshot_filtered() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    snapshot = await repository.snapshot(SpanFilter(limit=0))
    assert snapshot.filtered and snapshot.records == () and snapshot.status.record_count == 1


@pytest.mark.asyncio
async def test_span_repository_snapshot_uses_injected_clock() -> None:
    created, collected = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    values = iter((created, collected))
    snapshot = await InMemorySpanRepository(clock=lambda: next(values)).snapshot()
    assert snapshot.collected_at == collected


@pytest.mark.asyncio
async def test_span_repository_snapshot_rejects_naive_clock() -> None:
    aware, naive = datetime.now(UTC), datetime.now()
    values = iter((aware, naive))
    repository = InMemorySpanRepository(clock=lambda: next(values))
    with pytest.raises(SpanRepositoryError):
        await repository.snapshot()


@pytest.mark.asyncio
async def test_span_repository_snapshot_rejects_clock_before_creation() -> None:
    created = datetime(2026, 1, 2, tzinfo=UTC)
    values = iter((created, created - timedelta(seconds=1)))
    repository = InMemorySpanRepository(clock=lambda: next(values))
    with pytest.raises(SpanRepositoryError):
        await repository.snapshot()


@pytest.mark.asyncio
async def test_old_span_snapshot_unchanged_after_add() -> None:
    repository = InMemorySpanRepository()
    snapshot = await repository.snapshot()
    await repository.add(_record())
    assert snapshot.records == ()


@pytest.mark.asyncio
async def test_old_span_snapshot_unchanged_after_reset() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    snapshot = await repository.snapshot()
    await repository.reset()
    assert len(snapshot.records) == 1


def test_span_repository_snapshot_serialization_is_independent() -> None:
    now = datetime.now(UTC)
    snapshot = SpanRepositorySnapshot(SpanRepositoryStatus(0, 1, False, now), (), now)
    rendered = snapshot.to_dict()
    cast(list[object], rendered["records"]).append({})
    assert snapshot.records == ()


def test_span_repository_snapshot_rendering_redacts_records() -> None:
    secret, now = "UNIT-SNAPSHOT-SECRET", datetime.now(UTC)
    record = SpanRecord(
        TraceContext.create_root(),
        "safe",
        TraceSource("unit"),
        SpanKind.INTERNAL,
        SpanStatus.OK,
        now,
        now,
        attributes={"password": secret},
    )
    snapshot = SpanRepositorySnapshot(SpanRepositoryStatus(1, 1, False, now), (record,), now)
    assert secret not in repr(snapshot.to_dict())


@pytest.mark.asyncio
async def test_span_repository_remove_frees_capacity() -> None:
    repository, record = InMemorySpanRepository(maximum_records=1), _record()
    await repository.add(record)
    await repository.remove(record.context.trace_id, record.context.span_id)
    await repository.add(_record())
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_span_repository_reset_respects_pagination() -> None:
    repository, now = InMemorySpanRepository(), datetime.now(UTC)
    records = tuple(
        SpanRecord(
            TraceContext.create_root(),
            name,
            TraceSource("unit"),
            SpanKind.INTERNAL,
            SpanStatus.OK,
            now,
            now + timedelta(seconds=index),
        )
        for index, name in enumerate(("first", "second", "third"))
    )
    for record in records:
        await repository.add(record)
    assert await repository.reset(SpanFilter(offset=1, limit=1)) == 1
    assert await repository.list() == (records[0], records[2])


@pytest.mark.asyncio
async def test_span_repository_reset_empty_match() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    assert await repository.reset(SpanFilter(names=frozenset({"none"}))) == 0


@pytest.mark.asyncio
async def test_span_repository_reset_frees_capacity() -> None:
    repository = InMemorySpanRepository(maximum_records=1)
    await repository.add(_record())
    await repository.reset()
    await repository.add(_record())
    assert await repository.count() == 1


@pytest.mark.asyncio
async def test_span_repository_status_tracks_reset() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    await repository.reset()
    assert (await repository.status()).record_count == 0


@pytest.mark.asyncio
async def test_span_repository_get_after_close() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    await repository.close()
    assert await repository.get(record.context.trace_id, record.context.span_id) is record


@pytest.mark.asyncio
async def test_span_repository_list_after_close() -> None:
    repository = InMemorySpanRepository()
    await repository.close()
    assert await repository.list() == ()


@pytest.mark.asyncio
async def test_span_repository_snapshot_after_close() -> None:
    repository = InMemorySpanRepository()
    await repository.close()
    assert (await repository.snapshot()).status.closed


@pytest.mark.asyncio
async def test_span_repository_remove_after_close() -> None:
    repository, record = InMemorySpanRepository(), _record()
    await repository.add(record)
    await repository.close()
    assert await repository.remove(record.context.trace_id, record.context.span_id)


@pytest.mark.asyncio
async def test_span_repository_reset_after_close() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    await repository.close()
    assert await repository.reset() == 1


@pytest.mark.asyncio
async def test_span_repository_close_does_not_clear_records() -> None:
    repository = InMemorySpanRepository()
    await repository.add(_record())
    await repository.close()
    assert await repository.count() == 1


class _BlockingSpanRepository(InMemorySpanRepository):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def add(self, record: SpanRecord) -> None:
        self.entered.set()
        await self.release.wait()
        await super().add(record)


@pytest.mark.asyncio
async def test_simple_span_processor_failure_preserves_cause() -> None:
    repository = InMemorySpanRepository(maximum_records=1)
    await repository.add(_record())
    with pytest.raises(SpanProcessorError) as captured:
        await SimpleSpanProcessor(repository).on_end(_record())
    assert isinstance(captured.value.__cause__, SpanRepositoryCapacityError)


@pytest.mark.asyncio
async def test_simple_span_processor_status_tracks_failure() -> None:
    repository = InMemorySpanRepository(maximum_records=1)
    await repository.add(_record())
    processor = SimpleSpanProcessor(repository)
    with pytest.raises(SpanProcessorError):
        await processor.on_end(_record())
    assert (await processor.status()).failed_count == 1


@pytest.mark.asyncio
async def test_simple_span_processor_concurrent_processing() -> None:
    processor, repository = SimpleSpanProcessor(InMemorySpanRepository()), InMemorySpanRepository()
    processor = SimpleSpanProcessor(repository)
    await asyncio.gather(processor.on_end(_record()), processor.on_end(_record()))
    assert (await processor.status()).processed_count == 2


@pytest.mark.asyncio
async def test_simple_span_processor_force_flush_waits_for_in_flight() -> None:
    repository = _BlockingSpanRepository()
    processor = SimpleSpanProcessor(repository)
    delivery = asyncio.create_task(processor.on_end(_record()))
    await repository.entered.wait()
    flush = asyncio.create_task(processor.force_flush())
    assert not flush.done()
    repository.release.set()
    await delivery
    await flush


@pytest.mark.asyncio
async def test_simple_span_processor_close_waits_for_in_flight() -> None:
    repository = _BlockingSpanRepository()
    processor = SimpleSpanProcessor(repository)
    delivery = asyncio.create_task(processor.on_end(_record()))
    await repository.entered.wait()
    closing = asyncio.create_task(processor.close())
    assert not closing.done()
    repository.release.set()
    await delivery
    await closing


class _CountingCloseRepository(InMemorySpanRepository):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.close_count = 0
        self.fail = fail

    async def close(self) -> None:
        self.close_count += 1
        if self.fail:
            raise RuntimeError("safe close failure")
        await super().close()


@pytest.mark.asyncio
async def test_simple_span_processor_owned_repository_closes_once() -> None:
    repository = _CountingCloseRepository()
    processor = SimpleSpanProcessor(repository, close_repository=True)
    await asyncio.gather(processor.close(), processor.close())
    assert repository.close_count == 1


@pytest.mark.asyncio
async def test_simple_span_processor_repository_close_failure() -> None:
    repository = _CountingCloseRepository(fail=True)
    processor = SimpleSpanProcessor(repository, close_repository=True)
    with pytest.raises(SpanProcessorError):
        await processor.close()
    assert (await processor.status()).closed


@pytest.mark.asyncio
async def test_simple_span_processor_creates_no_background_task() -> None:
    before = asyncio.all_tasks()
    SimpleSpanProcessor(InMemorySpanRepository())
    assert asyncio.all_tasks() == before
