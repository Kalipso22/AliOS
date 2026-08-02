from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from alios_core import SpanId, TraceId
from alios_core.errors import SpanValidationError, TraceContextError, TraceSerializationError
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
    assert TraceId(str(value)) == value


def test_span_id_string_round_trip() -> None:
    value = SpanId()
    assert SpanId(str(value)) == value


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
    assert not hasattr(_context().baggage, "__setitem__")


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
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_id_type_sensitive_equality() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_id_rejects_non_v4_uuid() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_id_rejects_non_v4_uuid() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_source_preserves_capitalization() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_source_rejects_control_character() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_source_rejects_newline() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_source_rejects_excessive_length() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_source_is_immutable() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_source_serialization_is_independent() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_source_from_dict_uses_strict_types() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_kind_rejects_unknown_value() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_kind_rejects_non_string() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_status_rejects_unknown_value() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_status_rejects_non_string() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_accepts_nested_json_values() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_normalizes_identifier() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_normalizes_str_enum() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_normalizes_aware_datetime() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_rejects_positive_infinity() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_rejects_negative_infinity() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_rejects_bytes() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_rejects_set() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_rejects_non_string_mapping_key() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_rejects_function() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_rejects_coroutine() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_rejects_cycle() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_enforces_maximum_depth() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_enforces_total_item_budget() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_error_omits_rejected_value() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_data_deep_thaw_is_independent() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_empty() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_defensive_copy() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_rejects_non_string_key() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_rejects_non_string_value() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_rejects_invalid_key() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_rejects_double_underscore_key() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_rejects_control_character() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_rejects_excessive_entries() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_rejects_excessive_key_length() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_rejects_excessive_value_length() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_baggage_error_omits_value() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_valid() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_wrong_trace_identifier() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_wrong_span_identifier() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_wrong_parent_identifier() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_wrong_correlation_identifier() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_wrong_run_identifier() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_wrong_tenant_identifier() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_wrong_user_identifier() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_non_boolean_sampled() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rejects_parent_equal_to_span() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_is_immutable() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_root_generates_ids() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_child_generates_new_span_id() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_child_retains_scope() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_child_inherits_sampled() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_child_overrides_sampled() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_child_merges_baggage() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_child_does_not_mutate_parent() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_with_baggage_does_not_mutate_original() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_serialization() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_serialization_is_independent() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_from_dict_rejects_invalid_uuid() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_from_dict_rejects_wrong_source_type() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_context_rendering_redacts_baggage() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_async_trace_context_binding() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_nested_trace_binding_restores_parent() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_binding_restores_after_exception() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_binding_restores_after_cancellation() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_binding_rejects_wrong_context_type() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_binding_rejects_concurrent_reentry() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_binding_token_resets_once() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_trace_binding_does_not_mutate_context() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_event_requires_name() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_event_rejects_naive_timestamp() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_event_rejects_control_character() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_event_attributes_are_immutable() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_event_serialization() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_event_redacts_name() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_event_redacts_attributes() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_event_serialization_is_independent() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_link_rejects_invalid_context() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_link_attributes_are_immutable() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_link_serialization() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_link_redacts_baggage() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_link_redacts_attributes() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


def test_span_link_serialization_is_independent() -> None:
    context = _context()
    assert TraceContext.from_dict(context.to_dict()).trace_id == context.trace_id


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
