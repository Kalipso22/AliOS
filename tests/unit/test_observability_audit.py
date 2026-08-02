import asyncio
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from typing import cast
from uuid import UUID, uuid4

import pytest
from alios_core import AuditRecordId, CorrelationId, LogRecordId, RunId, SpanId, TraceId
from alios_core.errors import (
    AuditContextError,
    AuditError,
    AuditIntegrityError,
    AuditSerializationError,
    AuditValidationError,
)
from alios_core.ids import EventId, TenantId, UserId
from alios_observability import (
    AuditAction,
    AuditActor,
    AuditActorKind,
    AuditCategory,
    AuditContext,
    AuditOutcome,
    AuditRecord,
    AuditSeverity,
    AuditTarget,
    RedactionPolicy,
    TraceContext,
    TraceSource,
    bind_audit_context,
    current_audit_context,
    require_audit_context,
)


def _actor(
    *,
    identifier: str = "user-1",
    tenant_id: TenantId | None = None,
    user_id: UserId | None = None,
    attributes: Mapping[str, object] | None = None,
) -> AuditActor:
    return AuditActor(AuditActorKind.USER, identifier, tenant_id, user_id, attributes or {})


def _record() -> AuditRecord:
    return AuditRecord(
        AuditRecordId(),
        datetime(2026, 1, 1, tzinfo=UTC),
        AuditCategory.SECURITY,
        AuditSeverity.NOTICE,
        AuditOutcome.SUCCESS,
        _actor(),
        AuditAction("read"),
        TraceSource("unit"),
        "Read resource",
    )


def _safe_policy() -> RedactionPolicy:
    return RedactionPolicy(include_default_rules=False)


def test_audit_record_id_generation() -> None:
    assert AuditRecordId().value.version == 4


def test_audit_record_id_string_round_trip() -> None:
    value = AuditRecordId()
    assert AuditRecordId(str(value)) == value


def test_audit_record_id_type_sensitive_equality() -> None:
    value = uuid4()
    assert AuditRecordId(value) != TraceId(value)


def test_audit_record_id_does_not_equal_event_id() -> None:
    value = uuid4()
    assert AuditRecordId(value) != EventId(value)


def test_audit_record_id_does_not_equal_log_record_id() -> None:
    value = uuid4()
    assert AuditRecordId(value) != LogRecordId(value)


def test_audit_record_id_rejects_non_v4_uuid() -> None:
    with pytest.raises(ValueError):
        AuditRecordId(UUID(int=0))


def test_audit_record_id_is_hashable() -> None:
    identifier = AuditRecordId()
    assert identifier in {identifier}


def test_audit_error_codes_are_stable() -> None:
    assert AuditValidationError.code == "audit_validation_error"


def test_audit_error_details_are_safe() -> None:
    assert AuditError("safe", {"field_name": "actor"}).details == {"field_name": "actor"}


def test_audit_error_serialization() -> None:
    assert AuditIntegrityError("invalid").to_dict()["code"] == "audit_integrity_error"


def test_audit_category_exact_values() -> None:
    assert [item.value for item in AuditCategory] == [
        "authentication",
        "authorization",
        "data_access",
        "configuration",
        "execution",
        "lifecycle",
        "security",
        "system",
    ]


def test_audit_category_parsing() -> None:
    assert AuditCategory.parse("security") is AuditCategory.SECURITY


def test_audit_category_rejects_unknown() -> None:
    with pytest.raises(AuditValidationError):
        AuditCategory.parse("other")


def test_audit_category_rejects_non_string() -> None:
    with pytest.raises(AuditValidationError):
        AuditCategory.parse(cast(str, 1))


def test_audit_severity_exact_values() -> None:
    assert AuditSeverity.CRITICAL.value == "critical"


def test_audit_severity_parsing() -> None:
    assert AuditSeverity.parse("warning") is AuditSeverity.WARNING


def test_audit_severity_rejects_unknown() -> None:
    with pytest.raises(AuditValidationError):
        AuditSeverity.parse("debug")


def test_audit_severity_rejects_non_string() -> None:
    with pytest.raises(AuditValidationError):
        AuditSeverity.parse(cast(str, None))


def test_audit_outcome_exact_values() -> None:
    assert AuditOutcome.DENIED.value == "denied"


def test_audit_outcome_parsing() -> None:
    assert AuditOutcome.parse("unknown") is AuditOutcome.UNKNOWN


def test_audit_outcome_rejects_unknown() -> None:
    with pytest.raises(AuditValidationError):
        AuditOutcome.parse("passed")


def test_audit_outcome_rejects_non_string() -> None:
    with pytest.raises(AuditValidationError):
        AuditOutcome.parse(cast(str, True))


def test_audit_actor_kind_exact_values() -> None:
    assert AuditActorKind.ANONYMOUS.value == "anonymous"


def test_audit_actor_kind_parsing() -> None:
    assert AuditActorKind.parse("service") is AuditActorKind.SERVICE


def test_audit_actor_kind_rejects_unknown() -> None:
    with pytest.raises(AuditValidationError):
        AuditActorKind.parse("robot")


def test_audit_actor_kind_rejects_non_string() -> None:
    with pytest.raises(AuditValidationError):
        AuditActorKind.parse(cast(str, []))


def test_audit_data_accepts_nested_json_values() -> None:
    assert _actor(attributes={"nested": [1, {"ok": True}]}).attributes["nested"]


def test_audit_data_preserves_plain_string() -> None:
    assert _actor(attributes={"value": "Text"}).attributes["value"] == "Text"


def test_audit_data_normalizes_str_enum() -> None:
    assert _actor(attributes={"outcome": AuditOutcome.SUCCESS}).attributes["outcome"] == "success"


def test_audit_data_normalizes_identifier() -> None:
    identifier = RunId()
    assert _actor(attributes={"run": identifier}).attributes["run"] == str(identifier)


def test_audit_data_normalizes_aware_datetime() -> None:
    value = datetime.now(UTC)
    assert _actor(attributes={"at": value}).attributes["at"] == value.isoformat()


def test_audit_data_rejects_naive_datetime() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"at": datetime.now()})


def test_audit_data_rejects_nan() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": float("nan")})


def test_audit_data_rejects_positive_infinity() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": float("inf")})


def test_audit_data_rejects_negative_infinity() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": float("-inf")})


def test_audit_data_rejects_bytes() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": b"secret"})


def test_audit_data_rejects_bytearray() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": bytearray(b"x")})


def test_audit_data_rejects_set() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": {1}})


def test_audit_data_rejects_frozenset() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": frozenset({1})})


def test_audit_data_rejects_non_string_mapping_key() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes=cast(dict[str, object], {1: "x"}))


def test_audit_data_rejects_arbitrary_enum() -> None:
    class Other(Enum):
        VALUE = "value"

    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": Other.VALUE})


def test_audit_data_rejects_exception() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": RuntimeError("secret")})


def test_audit_data_rejects_function() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": test_audit_data_rejects_function})


@pytest.mark.asyncio
async def test_audit_data_rejects_coroutine() -> None:
    coroutine = asyncio.sleep(0)
    try:
        with pytest.raises(AuditSerializationError):
            _actor(attributes={"value": coroutine})
    finally:
        coroutine.close()


def test_audit_data_rejects_mapping_cycle() -> None:
    value: dict[str, object] = {}
    value["self"] = value
    with pytest.raises(AuditSerializationError):
        _actor(attributes=value)


def test_audit_data_rejects_sequence_cycle() -> None:
    value: list[object] = []
    value.append(value)
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": value})


def test_audit_data_enforces_maximum_depth() -> None:
    value: object = None
    for _ in "abcdefghijklmnopqr":
        value = [value]
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": value})


def test_audit_data_enforces_total_item_budget() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"values": [None] * 10_001})


def test_audit_data_enforces_maximum_string_length() -> None:
    with pytest.raises(AuditSerializationError):
        _actor(attributes={"value": "x" * 16_385})


def test_audit_data_error_omits_rejected_secret() -> None:
    secret = "AUDIT-UNIT-SECRET"
    with pytest.raises(AuditSerializationError) as captured:
        _actor(attributes={"value": RuntimeError(secret)})
    assert secret not in repr(captured.value.to_dict())


def test_audit_data_thaw_is_independent() -> None:
    actor = _actor(attributes={"nested": [1]})
    rendered = actor.to_dict(_safe_policy())
    cast(list[int], cast(dict[str, object], rendered["attributes"])["nested"]).append(2)
    assert actor.attributes["nested"] == (1,)


def test_audit_actor_user() -> None:
    assert _actor().kind is AuditActorKind.USER


def test_audit_actor_service() -> None:
    assert AuditActor(AuditActorKind.SERVICE, "api").identifier == "api"


def test_audit_actor_agent() -> None:
    assert AuditActor(AuditActorKind.AGENT, "planner").kind is AuditActorKind.AGENT


def test_audit_actor_system() -> None:
    assert AuditActor(AuditActorKind.SYSTEM, "alios").identifier == "alios"


def test_audit_actor_anonymous() -> None:
    assert AuditActor(AuditActorKind.ANONYMOUS).identifier is None


def test_audit_actor_anonymous_rejects_identifier() -> None:
    with pytest.raises(AuditValidationError):
        AuditActor(AuditActorKind.ANONYMOUS, "guest")


def test_audit_actor_non_anonymous_requires_identifier() -> None:
    with pytest.raises(AuditValidationError):
        AuditActor(AuditActorKind.USER)


def test_audit_actor_rejects_wrong_kind() -> None:
    with pytest.raises(AuditValidationError):
        AuditActor(cast(AuditActorKind, "user"), "id")


def test_audit_actor_rejects_wrong_tenant_id() -> None:
    with pytest.raises(AuditValidationError):
        _actor(tenant_id=cast(TenantId, UserId()))


def test_audit_actor_rejects_wrong_user_id() -> None:
    with pytest.raises(AuditValidationError):
        _actor(user_id=cast(UserId, TenantId()))


def test_audit_actor_attributes_are_immutable() -> None:
    with pytest.raises(TypeError):
        cast(dict[str, object], _actor().attributes)["x"] = 1


def test_audit_actor_caller_mapping_isolated() -> None:
    values = {"x": [1]}
    actor = _actor(attributes=values)
    values["x"].append(2)
    assert actor.attributes["x"] == (1,)


def test_audit_actor_serialization() -> None:
    assert _actor().to_dict(_safe_policy())["kind"] == "user"


def test_audit_actor_round_trip() -> None:
    actor = _actor(attributes={"x": 1})
    assert AuditActor.from_dict(actor.to_dict(_safe_policy())) == actor


def test_audit_actor_rendering_redacts_identifier() -> None:
    assert _actor(identifier="password=secret").to_dict()["identifier"] != "password=secret"


def test_audit_actor_rendering_redacts_attributes() -> None:
    assert "secret" not in repr(_actor(attributes={"password": "secret"}).to_dict())


def test_audit_actor_from_dict_uses_strict_types() -> None:
    with pytest.raises(AuditSerializationError):
        AuditActor.from_dict({"kind": 1})


def test_audit_actor_serialization_is_independent() -> None:
    actor = _actor(attributes={"x": [1]})
    cast(dict[str, object], actor.to_dict(_safe_policy())["attributes"])["x"] = []
    assert actor.attributes["x"] == (1,)


def test_audit_target_valid() -> None:
    assert AuditTarget("document", "doc-1").kind == "document"


def test_audit_target_requires_kind() -> None:
    with pytest.raises(AuditValidationError):
        AuditTarget("", "id")


def test_audit_target_requires_identifier() -> None:
    with pytest.raises(AuditValidationError):
        AuditTarget("document", " ")


def test_audit_target_rejects_invalid_kind_token() -> None:
    with pytest.raises(AuditValidationError):
        AuditTarget("bad kind", "id")


def test_audit_target_rejects_control_character() -> None:
    with pytest.raises(AuditValidationError):
        AuditTarget("document", "id\nsecret")


def test_audit_target_attributes_are_immutable() -> None:
    with pytest.raises(TypeError):
        cast(dict[str, object], AuditTarget("doc", "id").attributes)["x"] = 1


def test_audit_target_round_trip() -> None:
    target = AuditTarget("doc", "id", {"x": 1})
    assert AuditTarget.from_dict(target.to_dict(_safe_policy())) == target


def test_audit_target_redacts_identifier() -> None:
    assert "secret" not in repr(AuditTarget("doc", "password=secret").to_dict())


def test_audit_target_redacts_attributes() -> None:
    assert "secret" not in repr(AuditTarget("doc", "id", {"password": "secret"}).to_dict())


def test_audit_target_from_dict_uses_strict_types() -> None:
    with pytest.raises(AuditSerializationError):
        AuditTarget.from_dict({"kind": "doc", "identifier": 4})


def test_audit_action_valid() -> None:
    assert AuditAction("data/read").name == "data/read"


def test_audit_action_requires_name() -> None:
    with pytest.raises(AuditValidationError):
        AuditAction("")


def test_audit_action_rejects_invalid_name() -> None:
    with pytest.raises(AuditValidationError):
        AuditAction("read data")


def test_audit_action_rejects_double_underscore_name() -> None:
    with pytest.raises(AuditValidationError):
        AuditAction("__private")


def test_audit_action_rejects_control_character() -> None:
    with pytest.raises(AuditValidationError):
        AuditAction("read\n")


def test_audit_action_attributes_are_immutable() -> None:
    with pytest.raises(TypeError):
        cast(dict[str, object], AuditAction("read").attributes)["x"] = 1


def test_audit_action_round_trip() -> None:
    action = AuditAction("read", {"x": 1})
    assert AuditAction.from_dict(action.to_dict(_safe_policy())) == action


def test_audit_action_redacts_attributes() -> None:
    assert "secret" not in repr(AuditAction("read", {"password": "secret"}).to_dict())


def test_audit_action_from_dict_uses_strict_types() -> None:
    with pytest.raises(AuditSerializationError):
        AuditAction.from_dict({"name": []})


def test_audit_context_empty() -> None:
    assert AuditContext().trace_id is None


def test_audit_context_valid_identifiers() -> None:
    trace, span = TraceId(), SpanId()
    assert AuditContext(trace_id=trace, span_id=span).trace_id == trace


def test_audit_context_rejects_wrong_correlation_id() -> None:
    with pytest.raises(AuditValidationError):
        AuditContext(correlation_id=cast(CorrelationId, RunId()))


def test_audit_context_rejects_wrong_run_id() -> None:
    with pytest.raises(AuditValidationError):
        AuditContext(run_id=cast(RunId, CorrelationId()))


def test_audit_context_rejects_wrong_tenant_id() -> None:
    with pytest.raises(AuditValidationError):
        AuditContext(tenant_id=cast(TenantId, UserId()))


def test_audit_context_rejects_wrong_user_id() -> None:
    with pytest.raises(AuditValidationError):
        AuditContext(user_id=cast(UserId, TenantId()))


def test_audit_context_rejects_wrong_trace_id() -> None:
    with pytest.raises(AuditValidationError):
        AuditContext(trace_id=cast(TraceId, SpanId()), span_id=SpanId())


def test_audit_context_rejects_wrong_span_id() -> None:
    with pytest.raises(AuditValidationError):
        AuditContext(trace_id=TraceId(), span_id=cast(SpanId, TraceId()))


def test_audit_context_rejects_wrong_parent_span_id() -> None:
    with pytest.raises(AuditValidationError):
        AuditContext(trace_id=TraceId(), span_id=SpanId(), parent_span_id=cast(SpanId, TraceId()))


def test_audit_context_requires_trace_and_span_together() -> None:
    with pytest.raises(AuditContextError):
        AuditContext(trace_id=TraceId())


def test_audit_context_parent_requires_trace() -> None:
    with pytest.raises(AuditContextError):
        AuditContext(parent_span_id=SpanId())


def test_audit_context_rejects_parent_equal_to_span() -> None:
    span = SpanId()
    with pytest.raises(AuditContextError):
        AuditContext(trace_id=TraceId(), span_id=span, parent_span_id=span)


def test_audit_context_metadata_is_immutable() -> None:
    with pytest.raises(TypeError):
        cast(dict[str, object], AuditContext().metadata)["x"] = 1


def test_audit_context_caller_metadata_isolated() -> None:
    values = {"x": [1]}
    context = AuditContext(metadata=values)
    values["x"].append(2)
    assert context.metadata["x"] == (1,)


def test_audit_context_from_trace_context() -> None:
    trace = TraceContext.create_root(correlation_id=CorrelationId())
    assert AuditContext.from_trace_context(trace).trace_id == trace.trace_id


def test_audit_context_from_trace_context_omits_baggage() -> None:
    assert (
        AuditContext.from_trace_context(TraceContext.create_root(baggage={"secret": "x"})).metadata
        == {}
    )


def test_audit_context_from_trace_context_does_not_mutate_trace() -> None:
    trace = TraceContext.create_root()
    AuditContext.from_trace_context(trace, metadata={"x": 1})
    assert trace.baggage == {}


def test_audit_context_with_metadata() -> None:
    assert AuditContext().with_metadata(region="eu").metadata["region"] == "eu"


def test_audit_context_with_metadata_does_not_mutate_original() -> None:
    context = AuditContext()
    context.with_metadata(x=1)
    assert context.metadata == {}


def test_audit_context_serialization() -> None:
    assert AuditContext().to_dict()["trace_id"] is None


def test_audit_context_round_trip() -> None:
    context = AuditContext(correlation_id=CorrelationId(), metadata={"x": 1})
    assert AuditContext.from_dict(context.to_dict(_safe_policy())) == context


def test_audit_context_from_dict_rejects_empty_identifier() -> None:
    with pytest.raises(AuditSerializationError):
        AuditContext.from_dict({"run_id": ""})


def test_audit_context_from_dict_rejects_invalid_uuid() -> None:
    with pytest.raises(AuditSerializationError):
        AuditContext.from_dict({"run_id": "invalid"})


def test_audit_context_from_dict_rejects_wrong_source_type() -> None:
    with pytest.raises(AuditSerializationError):
        AuditContext.from_dict(cast(dict[str, object], []))


def test_audit_context_rendering_redacts_metadata() -> None:
    assert "secret" not in repr(AuditContext(metadata={"password": "secret"}).to_dict())


def test_audit_context_serialization_is_independent() -> None:
    context = AuditContext(metadata={"x": [1]})
    cast(dict[str, object], context.to_dict(_safe_policy())["metadata"])["x"] = []
    assert context.metadata["x"] == (1,)


def test_current_audit_context_is_none_by_default() -> None:
    assert current_audit_context() is None


def test_require_audit_context_raises_when_unbound() -> None:
    with pytest.raises(AuditContextError):
        require_audit_context()


def test_sync_audit_context_binding() -> None:
    context = AuditContext(run_id=RunId())
    with bind_audit_context(context):
        assert require_audit_context() == context
    assert current_audit_context() is None


@pytest.mark.asyncio
async def test_async_audit_context_binding() -> None:
    context = AuditContext()
    async with bind_audit_context(context):
        assert current_audit_context() is context


def test_nested_audit_binding_restores_parent() -> None:
    parent, child = AuditContext(run_id=RunId()), AuditContext(run_id=RunId())
    with bind_audit_context(parent):
        with bind_audit_context(child):
            assert require_audit_context() == child
        assert require_audit_context() == parent


def test_audit_binding_restores_after_exception() -> None:
    with pytest.raises(RuntimeError):
        with bind_audit_context(AuditContext()):
            raise RuntimeError("safe")
    assert current_audit_context() is None


@pytest.mark.asyncio
async def test_audit_binding_restores_after_cancellation() -> None:
    async def cancelled() -> None:
        async with bind_audit_context(AuditContext()):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancelled()
    assert current_audit_context() is None


def test_audit_binding_rejects_wrong_context() -> None:
    with pytest.raises(AuditContextError):
        bind_audit_context(cast(AuditContext, object()))


def test_audit_binding_rejects_active_reentry() -> None:
    binding = bind_audit_context(AuditContext())
    with binding:
        with pytest.raises(AuditContextError):
            binding.__enter__()


def test_audit_binding_token_resets_once() -> None:
    binding = bind_audit_context(AuditContext())
    binding.__enter__()
    binding.__exit__()
    binding.__exit__()
    assert current_audit_context() is None


def test_audit_binding_does_not_mutate_context() -> None:
    context = AuditContext(metadata={"x": 1})
    with bind_audit_context(context):
        pass
    assert context.metadata["x"] == 1


@pytest.mark.asyncio
async def test_audit_binding_child_task_inherits_snapshot() -> None:
    context = AuditContext(run_id=RunId())
    async with bind_audit_context(context):
        assert (
            await asyncio.create_task(asyncio.sleep(0, result=require_audit_context())) == context
        )


def test_audit_record_valid() -> None:
    assert _record().summary == "Read resource"


def test_audit_record_requires_record_id() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), record_id=cast(AuditRecordId, None))


def test_audit_record_requires_aware_timestamp() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), occurred_at=datetime.now())


def test_audit_record_requires_category() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), category=cast(AuditCategory, "security"))


def test_audit_record_requires_severity() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), severity=cast(AuditSeverity, "notice"))


def test_audit_record_requires_outcome() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), outcome=cast(AuditOutcome, "success"))


def test_audit_record_requires_actor() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), actor=cast(AuditActor, object()))


def test_audit_record_requires_action() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), action=cast(AuditAction, object()))


def test_audit_record_requires_source() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), source=cast(TraceSource, object()))


def test_audit_record_requires_context() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), context=cast(AuditContext, object()))


def test_audit_record_requires_summary() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), summary=" ")


def test_audit_record_rejects_control_character_in_summary() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), summary="bad\nsummary")


def test_audit_record_rejects_invalid_reason_code() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), reason_code="bad reason")


def test_audit_record_accepts_optional_target() -> None:
    assert replace(_record(), target=AuditTarget("doc", "1")).target is not None


def test_audit_record_attributes_are_immutable() -> None:
    with pytest.raises(TypeError):
        cast(dict[str, object], replace(_record(), attributes={"x": 1}).attributes)["x"] = 2


def test_audit_record_caller_attributes_isolated() -> None:
    values = {"x": [1]}
    record = replace(_record(), attributes=values)
    values["x"].append(2)
    assert record.attributes["x"] == (1,)


def test_audit_record_tags_are_immutable() -> None:
    assert replace(_record(), tags=frozenset({"security"})).tags == frozenset({"security"})


def test_audit_record_rejects_invalid_tag() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), tags=frozenset({"bad tag"}))


def test_audit_record_rejects_too_many_tags() -> None:
    with pytest.raises(AuditValidationError):
        replace(
            _record(),
            tags=frozenset(
                f"tag{index}" for index, _ in enumerate("abcdefghijklmnopqrstuvwxyzABCDEFG")
            ),
        )


def test_audit_record_rejects_duplicate_list_tags() -> None:
    with pytest.raises(AuditValidationError):
        replace(_record(), tags=cast(frozenset[str], ["one", "one"]))


def test_audit_record_does_not_infer_outcome() -> None:
    assert replace(_record(), severity=AuditSeverity.ERROR).outcome is AuditOutcome.SUCCESS


def test_audit_record_is_immutable() -> None:
    def mutate(value: object) -> None:
        delattr(value, "summary")

    with pytest.raises(FrozenInstanceError):
        mutate(_record())


def test_audit_record_create_generates_id() -> None:
    assert (
        AuditRecord.create(
            category=AuditCategory.SYSTEM,
            severity=AuditSeverity.INFO,
            outcome=AuditOutcome.SUCCESS,
            actor=_actor(),
            action=AuditAction("run"),
            source=TraceSource("unit"),
            summary="run",
        ).record_id.value.version
        == 4
    )


def test_audit_record_create_uses_clock() -> None:
    now = datetime.now(UTC)
    record = AuditRecord.create(
        category=AuditCategory.SYSTEM,
        severity=AuditSeverity.INFO,
        outcome=AuditOutcome.SUCCESS,
        actor=_actor(),
        action=AuditAction("run"),
        source=TraceSource("unit"),
        summary="run",
        clock=lambda: now,
    )
    assert record.occurred_at == now


def test_audit_record_create_explicit_time_skips_clock() -> None:
    now = datetime.now(UTC)
    record = AuditRecord.create(
        category=AuditCategory.SYSTEM,
        severity=AuditSeverity.INFO,
        outcome=AuditOutcome.SUCCESS,
        actor=_actor(),
        action=AuditAction("run"),
        source=TraceSource("unit"),
        summary="run",
        occurred_at=now,
        clock=lambda: (_ for _ in ()).throw(RuntimeError()),
    )
    assert record.occurred_at == now


def test_audit_record_create_explicit_id() -> None:
    identifier = AuditRecordId()
    record = AuditRecord.create(
        category=AuditCategory.SYSTEM,
        severity=AuditSeverity.INFO,
        outcome=AuditOutcome.SUCCESS,
        actor=_actor(),
        action=AuditAction("run"),
        source=TraceSource("unit"),
        summary="run",
        record_id=identifier,
    )
    assert record.record_id is identifier


def test_audit_record_create_uses_bound_context() -> None:
    context = AuditContext(run_id=RunId())
    with bind_audit_context(context):
        record = AuditRecord.create(
            category=AuditCategory.SYSTEM,
            severity=AuditSeverity.INFO,
            outcome=AuditOutcome.SUCCESS,
            actor=_actor(),
            action=AuditAction("run"),
            source=TraceSource("unit"),
            summary="run",
        )
    assert record.context is context


def test_audit_record_create_uses_empty_context_when_unbound() -> None:
    record = AuditRecord.create(
        category=AuditCategory.SYSTEM,
        severity=AuditSeverity.INFO,
        outcome=AuditOutcome.SUCCESS,
        actor=_actor(),
        action=AuditAction("run"),
        source=TraceSource("unit"),
        summary="run",
    )
    assert record.context == AuditContext()


def test_audit_record_serialization() -> None:
    assert _record().to_dict(_safe_policy())["category"] == "security"


def test_audit_record_round_trip() -> None:
    record = _record()
    assert AuditRecord.from_dict(record.to_dict(_safe_policy())) == record


def test_audit_record_rendered_output_is_json_serializable() -> None:
    assert json.loads(json.dumps(_record().to_dict()))["outcome"] == "success"


def test_audit_record_integrity_digest_format() -> None:
    digest = _record().integrity_digest()
    assert len(digest) == 64 and digest == digest.lower()


def test_audit_record_integrity_digest_is_deterministic() -> None:
    record = _record()
    assert record.integrity_digest() == record.integrity_digest()


def test_audit_record_integrity_digest_equal_records() -> None:
    record = _record()
    assert record.integrity_digest() == replace(record).integrity_digest()


def test_audit_record_integrity_digest_ignores_mapping_insertion_order() -> None:
    record = _record()
    assert (
        replace(record, attributes={"a": 1, "b": 2}).integrity_digest()
        == replace(record, attributes={"b": 2, "a": 1}).integrity_digest()
    )


def test_audit_record_integrity_digest_normalizes_datetime_to_utc() -> None:
    record = _record()
    other = record.occurred_at.astimezone(timezone(timedelta(hours=3)))
    assert record.integrity_digest() == replace(record, occurred_at=other).integrity_digest()


def test_audit_record_integrity_digest_changes_with_record_id() -> None:
    record = _record()
    assert (
        record.integrity_digest() != replace(record, record_id=AuditRecordId()).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_summary() -> None:
    record = _record()
    assert record.integrity_digest() != replace(record, summary="Other").integrity_digest()


def test_audit_record_integrity_digest_changes_with_attributes() -> None:
    record = _record()
    assert record.integrity_digest() != replace(record, attributes={"x": 1}).integrity_digest()


def test_audit_record_integrity_digest_changes_with_tags() -> None:
    record = _record()
    assert record.integrity_digest() != replace(record, tags=frozenset({"tag"})).integrity_digest()


def test_audit_record_integrity_digest_unaffected_by_redaction() -> None:
    record = replace(_record(), attributes={"password": "secret"})
    before = record.integrity_digest()
    record.to_dict()
    assert record.integrity_digest() == before


def test_audit_record_verify_integrity_digest_true() -> None:
    record = _record()
    assert record.verify_integrity_digest(record.integrity_digest())


def test_audit_record_verify_integrity_digest_false() -> None:
    assert not _record().verify_integrity_digest("0" * 64)


def test_audit_record_verify_integrity_rejects_uppercase_digest() -> None:
    with pytest.raises(AuditIntegrityError):
        _record().verify_integrity_digest("A" * 64)


def test_audit_record_verify_integrity_rejects_wrong_length() -> None:
    with pytest.raises(AuditIntegrityError):
        _record().verify_integrity_digest("0" * 63)


def test_audit_record_verify_integrity_rejects_non_hex() -> None:
    with pytest.raises(AuditIntegrityError):
        _record().verify_integrity_digest("z" * 64)


def test_audit_record_integrity_digest_contains_no_secret() -> None:
    secret = "AUDIT-DIGEST-SECRET"
    assert secret not in replace(_record(), attributes={"value": secret}).integrity_digest()


def test_audit_record_create_does_not_capture_trace_implicitly() -> None:
    assert (
        AuditRecord.create(
            category=AuditCategory.SYSTEM,
            severity=AuditSeverity.INFO,
            outcome=AuditOutcome.SUCCESS,
            actor=_actor(),
            action=AuditAction("run"),
            source=TraceSource("unit"),
            summary="run",
        ).context.trace_id
        is None
    )


def test_audit_record_create_rejects_naive_clock() -> None:
    with pytest.raises(AuditValidationError):
        AuditRecord.create(
            category=AuditCategory.SYSTEM,
            severity=AuditSeverity.INFO,
            outcome=AuditOutcome.SUCCESS,
            actor=_actor(),
            action=AuditAction("run"),
            source=TraceSource("unit"),
            summary="run",
            clock=datetime.now,
        )


def test_audit_record_create_clock_failure_has_no_context_side_effect() -> None:
    context = AuditContext(run_id=RunId())
    with bind_audit_context(context):
        with pytest.raises(RuntimeError):
            AuditRecord.create(
                category=AuditCategory.SYSTEM,
                severity=AuditSeverity.INFO,
                outcome=AuditOutcome.SUCCESS,
                actor=_actor(),
                action=AuditAction("run"),
                source=TraceSource("unit"),
                summary="run",
                clock=lambda: (_ for _ in ()).throw(RuntimeError("safe")),
            )
        assert require_audit_context() is context


def test_audit_record_from_dict_uses_strict_types() -> None:
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict({"record_id": 1})


def test_audit_record_from_dict_rejects_invalid_record_id() -> None:
    rendered = _record().to_dict(_safe_policy())
    rendered["record_id"] = "invalid"
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict(rendered)


def test_audit_record_from_dict_rejects_unknown_category() -> None:
    rendered = _record().to_dict(_safe_policy())
    rendered["category"] = "other"
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict(rendered)


def test_audit_record_from_dict_rejects_unknown_severity() -> None:
    rendered = _record().to_dict(_safe_policy())
    rendered["severity"] = "debug"
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict(rendered)


def test_audit_record_from_dict_rejects_unknown_outcome() -> None:
    rendered = _record().to_dict(_safe_policy())
    rendered["outcome"] = "other"
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict(rendered)


def test_audit_record_from_dict_rejects_malformed_actor() -> None:
    rendered = _record().to_dict(_safe_policy())
    rendered["actor"] = {}
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict(rendered)


def test_audit_record_from_dict_rejects_malformed_context() -> None:
    rendered = _record().to_dict(_safe_policy())
    rendered["context"] = {"trace_id": "bad"}
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict(rendered)


def test_audit_record_from_dict_rejects_non_mapping_target() -> None:
    rendered = _record().to_dict(_safe_policy())
    rendered["target"] = "target"
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict(rendered)


def test_audit_record_from_dict_rejects_non_string_tag() -> None:
    rendered = _record().to_dict(_safe_policy())
    rendered["tags"] = [1]
    with pytest.raises(AuditSerializationError):
        AuditRecord.from_dict(rendered)


def test_audit_record_rendering_redacts_actor() -> None:
    assert "secret" not in repr(
        replace(_record(), actor=AuditActor(AuditActorKind.USER, "password=secret")).to_dict()
    )


def test_audit_record_rendering_redacts_target() -> None:
    assert "secret" not in repr(
        replace(_record(), target=AuditTarget("doc", "password=secret")).to_dict()
    )


def test_audit_record_rendering_redacts_action() -> None:
    assert "secret" not in repr(
        replace(_record(), action=AuditAction("read", {"password": "secret"})).to_dict()
    )


def test_audit_record_rendering_redacts_summary() -> None:
    assert "secret" not in repr(replace(_record(), summary="password=secret").to_dict())


def test_audit_record_rendering_redacts_context() -> None:
    assert "secret" not in repr(
        replace(_record(), context=AuditContext(metadata={"password": "secret"})).to_dict()
    )


def test_audit_record_rendering_redacts_attributes() -> None:
    assert "secret" not in repr(replace(_record(), attributes={"password": "secret"}).to_dict())


def test_audit_record_original_unchanged_after_rendering() -> None:
    record = replace(_record(), attributes={"password": "secret"})
    record.to_dict()
    assert record.attributes["password"] == "secret"


def test_audit_record_serialization_is_independent() -> None:
    record = replace(_record(), attributes={"x": [1]})
    rendered = record.to_dict(_safe_policy())
    cast(dict[str, object], rendered["attributes"])["x"] = []
    assert record.attributes["x"] == (1,)


def test_redacted_audit_record_is_parseable() -> None:
    assert AuditRecord.from_dict(
        replace(_record(), attributes={"password": "secret"}).to_dict()
    ).attributes


def test_audit_record_integrity_digest_changes_with_timestamp() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, occurred_at=record.occurred_at + timedelta(seconds=1)).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_category() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, category=AuditCategory.SYSTEM).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_severity() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, severity=AuditSeverity.ERROR).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_outcome() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, outcome=AuditOutcome.FAILURE).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_actor() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, actor=AuditActor(AuditActorKind.USER, "other")).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_action() -> None:
    record = _record()
    assert (
        record.integrity_digest() != replace(record, action=AuditAction("write")).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_target() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, target=AuditTarget("doc", "1")).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_context() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, context=AuditContext(run_id=RunId())).integrity_digest()
    )


def test_audit_record_integrity_digest_changes_with_reason_code() -> None:
    record = _record()
    assert record.integrity_digest() != replace(record, reason_code="denied").integrity_digest()
