from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from itertools import product
from typing import cast
from uuid import UUID, uuid4

import pytest
from alios_core import CorrelationId, RunId, SpanId, TraceId
from alios_core.errors import SpanValidationError, TraceContextError, TraceSerializationError
from alios_core.ids import TenantId, UserId
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


def test_trace_binding_rejects_reentry() -> None:
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


def test_trace_attribute_naive_datetime_is_rejected() -> None:
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
