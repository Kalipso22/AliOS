import asyncio
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum, IntEnum, IntFlag, StrEnum
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alios_core import (
    AuditFilterError,
    AuditLedgerCapacityError,
    AuditLedgerClosedError,
    AuditLedgerError,
    AuditLedgerId,
    AuditLedgerVerificationError,
    AuditRecordId,
    AuditSnapshotError,
    CorrelationId,
    LogRecordId,
    RunId,
    SpanId,
    TraceId,
)
from alios_core.errors import (
    AuditContextError,
    AuditError,
    AuditIntegrityError,
    AuditSerializationError,
    AuditValidationError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from alios_core.ids import EventId, TenantId, UserId
from alios_observability import (
    AuditAction,
    AuditActor,
    AuditActorKind,
    AuditCategory,
    AuditContext,
    AuditFilter,
    AuditLedgerEntry,
    AuditLedgerSnapshot,
    AuditLedgerStatus,
    AuditLedgerVerificationFailure,
    AuditLedgerVerificationResult,
    AuditOutcome,
    AuditRecord,
    AuditSeverity,
    AuditTarget,
    InMemoryAuditLedger,
    RedactionPolicy,
    RedactionRule,
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


def _created_record(**overrides: Any) -> AuditRecord:
    values: dict[str, Any] = {
        "category": AuditCategory.SYSTEM,
        "severity": AuditSeverity.INFO,
        "outcome": AuditOutcome.SUCCESS,
        "actor": _actor(),
        "action": AuditAction("run"),
        "source": TraceSource("unit"),
        "summary": "run",
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return AuditRecord.create(**values)


def _safe_policy() -> RedactionPolicy:
    return RedactionPolicy(include_default_rules=False)


def _ledger_entry(
    *,
    ledger_id: AuditLedgerId | None = None,
    record: AuditRecord | None = None,
    sequence: int = 1,
    appended_at: datetime | None = None,
    previous_digest: str = "0" * 64,
) -> AuditLedgerEntry:
    return AuditLedgerEntry.create(
        ledger_id=ledger_id or AuditLedgerId(),
        sequence=sequence,
        record=record or _record(),
        appended_at=appended_at or datetime(2026, 1, 1, tzinfo=UTC),
        previous_digest=previous_digest,
    )


class _LedgerClock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)

    def __call__(self) -> datetime:
        return self.values.pop(0)


def _secret_ledger_record() -> AuditRecord:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    return replace(
        _record(),
        actor=AuditActor(AuditActorKind.USER, secret),
        target=AuditTarget("document", secret),
        summary=secret,
        context=AuditContext(metadata={"password": secret}),
        attributes={"password": secret},
    )


def _filter_entry(*, sequence: int = 1) -> AuditLedgerEntry:
    trace_id, span_id, parent_id = TraceId(), SpanId(), SpanId()
    record = replace(
        _record(),
        category=AuditCategory.SECURITY,
        severity=AuditSeverity.NOTICE,
        outcome=AuditOutcome.SUCCESS,
        actor=AuditActor(
            AuditActorKind.USER,
            "Alice",
            TenantId(),
            UserId(),
            {"role": "admin"},
        ),
        action=AuditAction("document.read", {"mode": "safe"}),
        source=TraceSource("api", "documents", "read"),
        target=AuditTarget("document", "doc-1", {"class": "internal"}),
        reason_code="allowed",
        attributes={"region": "eu"},
        tags=frozenset({"security", "access"}),
        context=AuditContext(
            correlation_id=CorrelationId(),
            run_id=RunId(),
            tenant_id=TenantId(),
            user_id=UserId(),
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_id,
            metadata={"environment": "test"},
        ),
    )
    return _ledger_entry(
        sequence=sequence,
        record=record,
        previous_digest="0" * 64 if sequence == 1 else "1" * 64,
    )


def _empty_snapshot(*, filtered: bool = False) -> AuditLedgerSnapshot:
    ledger_id = AuditLedgerId()
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    status = AuditLedgerStatus(ledger_id, 0, 10, 1, None, False, instant, None)
    verification = AuditLedgerVerificationResult(True, 0, ledger_id, None, None, None)
    return AuditLedgerSnapshot(status, (), instant, filtered, 0, verification)


def _populated_snapshot(*, filtered: bool = False) -> AuditLedgerSnapshot:
    ledger_id = AuditLedgerId()
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    entry = _ledger_entry(ledger_id=ledger_id, appended_at=instant)
    status = AuditLedgerStatus(ledger_id, 1, 10, 2, entry.entry_digest, False, instant, instant)
    verification = AuditLedgerVerificationResult(True, 1, ledger_id, 1, 1, entry.entry_digest)
    return AuditLedgerSnapshot(status, (entry,), instant, filtered, 1, verification)


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


def test_audit_data_rejects_int_enum() -> None:
    class Number(IntEnum):
        ONE = 1

    with pytest.raises(AuditSerializationError):
        AuditAction("read", {"value": Number.ONE})


def test_audit_data_rejects_int_flag() -> None:
    class Permission(IntFlag):
        READ = 1

    with pytest.raises(AuditSerializationError):
        AuditAction("read", {"value": Permission.READ})


def test_audit_data_still_accepts_plain_integer() -> None:
    assert AuditAction("read", {"value": 1}).attributes["value"] == 1


def test_audit_data_still_normalizes_str_enum() -> None:
    class Label(StrEnum):
        SAFE = "safe"

    assert AuditAction("read", {"value": Label.SAFE}).attributes["value"] == "safe"


def test_audit_data_enum_error_omits_secret() -> None:
    secret = "AUDIT-C2A1-STRICT-SECRET-2df84a"

    class Secret(Enum):
        VALUE = secret

    with pytest.raises(AuditSerializationError) as captured:
        AuditAction("read", {"nested": [Secret.VALUE]})
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_context_from_trace_context_accepts_none_metadata() -> None:
    assert AuditContext.from_trace_context(TraceContext.create_root(), metadata=None).metadata == {}


def test_audit_context_from_trace_context_accepts_empty_mapping() -> None:
    metadata: dict[str, object] = {}
    context = AuditContext.from_trace_context(TraceContext.create_root(), metadata=metadata)
    assert context.metadata == {} and context.metadata is not metadata


def test_audit_context_from_trace_context_rejects_empty_list_metadata() -> None:
    with pytest.raises(AuditContextError):
        AuditContext.from_trace_context(TraceContext.create_root(), metadata=cast(Any, []))


def test_audit_context_from_trace_context_rejects_empty_tuple_metadata() -> None:
    with pytest.raises(AuditContextError):
        AuditContext.from_trace_context(TraceContext.create_root(), metadata=cast(Any, ()))


def test_audit_context_from_trace_context_rejects_empty_string_metadata() -> None:
    with pytest.raises(AuditContextError):
        AuditContext.from_trace_context(TraceContext.create_root(), metadata=cast(Any, ""))


def test_audit_context_from_trace_context_rejects_false_metadata() -> None:
    with pytest.raises(AuditContextError):
        AuditContext.from_trace_context(TraceContext.create_root(), metadata=cast(Any, False))


def test_audit_context_from_trace_context_failure_preserves_bound_context() -> None:
    bound = AuditContext(run_id=RunId())
    with bind_audit_context(bound):
        with pytest.raises(AuditContextError):
            AuditContext.from_trace_context(TraceContext.create_root(), metadata=cast(Any, []))
        assert current_audit_context() is bound
    assert current_audit_context() is None


def test_audit_context_from_trace_context_secret_failure_is_safe() -> None:
    secret = "AUDIT-C2A1-STRICT-SECRET-2df84a"

    class Secret(Enum):
        VALUE = secret

    with pytest.raises(AuditContextError) as captured:
        AuditContext.from_trace_context(
            TraceContext.create_root(), metadata={"nested": [Secret.VALUE]}
        )
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_record_create_none_id_generates_identifier() -> None:
    assert type(_created_record(record_id=None).record_id) is AuditRecordId


def test_audit_record_create_preserves_explicit_identifier() -> None:
    identifier = AuditRecordId()
    assert _created_record(record_id=identifier).record_id is identifier


def test_audit_record_create_rejects_zero_record_id() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(record_id=0)


def test_audit_record_create_rejects_false_record_id() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(record_id=False)


def test_audit_record_create_rejects_empty_string_record_id() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(record_id="")


def test_audit_record_create_rejects_empty_list_record_id() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(record_id=[])


def test_audit_record_create_accepts_none_attributes() -> None:
    assert _created_record(attributes=None).attributes == {}


def test_audit_record_create_accepts_empty_mapping_attributes() -> None:
    attributes: dict[str, object] = {}
    record = _created_record(attributes=attributes)
    assert record.attributes == {} and record.attributes is not attributes


def test_audit_record_create_rejects_empty_list_attributes() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(attributes=[])


def test_audit_record_create_rejects_empty_tuple_attributes() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(attributes=())


def test_audit_record_create_rejects_empty_string_attributes() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(attributes="")


def test_audit_record_create_rejects_false_attributes() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(attributes=False)


def test_audit_record_create_attribute_failure_preserves_bound_context() -> None:
    bound = AuditContext(run_id=RunId())
    with bind_audit_context(bound):
        with pytest.raises(AuditValidationError):
            _created_record(attributes=[])
        assert current_audit_context() is bound


def test_audit_record_create_attribute_failure_skips_explicit_clock() -> None:
    with pytest.raises(AuditValidationError):
        _created_record(
            attributes=[],
            clock=lambda: (_ for _ in ()).throw(AssertionError("clock called")),
        )


def test_audit_record_create_attribute_error_omits_secret() -> None:
    secret = "AUDIT-C2A1-STRICT-SECRET-2df84a"

    class Secret(Enum):
        VALUE = secret

    with pytest.raises(AuditSerializationError) as captured:
        _created_record(attributes={"nested": [Secret.VALUE]})
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_record_from_dict_accepts_unique_list_tags() -> None:
    value = _record().to_dict(_safe_policy())
    value["tags"] = ["security", "access"]
    assert AuditRecord.from_dict(value).tags == frozenset({"security", "access"})


def test_audit_record_from_dict_accepts_unique_tuple_tags() -> None:
    value = cast(dict[str, object], _record().to_dict(_safe_policy()))
    value["tags"] = ("security", "access")
    assert AuditRecord.from_dict(value).tags == frozenset({"security", "access"})


def test_audit_record_from_dict_rejects_duplicate_list_tags() -> None:
    value = _record().to_dict(_safe_policy())
    value["tags"] = ["security", "security"]
    with pytest.raises(AuditSerializationError) as captured:
        AuditRecord.from_dict(value)
    assert isinstance(captured.value.__cause__, AuditValidationError)


def test_audit_record_from_dict_rejects_duplicate_tuple_tags() -> None:
    value = cast(dict[str, object], _record().to_dict(_safe_policy()))
    value["tags"] = ("security", "security")
    with pytest.raises(AuditSerializationError) as captured:
        AuditRecord.from_dict(value)
    assert isinstance(captured.value.__cause__, AuditValidationError)


def test_audit_record_duplicate_tag_error_omits_values() -> None:
    secret = "AUDIT-C2A1-STRICT-SECRET-2df84a"
    value = _record().to_dict(_safe_policy())
    value["tags"] = [secret, secret]
    with pytest.raises(AuditSerializationError) as captured:
        AuditRecord.from_dict(value)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_ledger_id_generation() -> None:
    assert AuditLedgerId().value.version == 4


def test_audit_ledger_id_string_round_trip() -> None:
    identifier = AuditLedgerId()
    assert AuditLedgerId(str(identifier)) == identifier


def test_audit_ledger_id_type_sensitive_equality() -> None:
    identifier = AuditLedgerId()
    assert identifier != TraceId(identifier.value)


def test_audit_ledger_id_does_not_equal_record_id() -> None:
    identifier = AuditLedgerId()
    assert identifier != AuditRecordId(identifier.value)


def test_audit_ledger_id_rejects_non_v4_uuid() -> None:
    with pytest.raises(ValueError):
        AuditLedgerId(UUID(int=0))


def test_audit_ledger_id_is_hashable() -> None:
    identifier = AuditLedgerId()
    assert identifier in {identifier}


def test_audit_ledger_id_uuid_and_json_conversion() -> None:
    value = uuid4()
    identifier = AuditLedgerId(value)
    assert identifier.value == value and identifier.to_json() == str(value)


def test_audit_ledger_id_does_not_equal_other_identifier_types() -> None:
    identifier = AuditLedgerId()
    assert identifier != EventId(identifier.value)
    assert identifier != SpanId(identifier.value)
    assert identifier != CorrelationId(identifier.value)


def test_audit_ledger_error_codes_are_stable() -> None:
    assert (
        AuditLedgerError.code,
        AuditLedgerClosedError.code,
        AuditLedgerCapacityError.code,
        AuditLedgerVerificationError.code,
    ) == (
        "audit_ledger_error",
        "audit_ledger_closed",
        "audit_ledger_capacity",
        "audit_ledger_verification_error",
    )


def test_audit_ledger_error_details_are_safe() -> None:
    error = AuditLedgerError("safe", details={"sequence": 1})
    assert error.to_dict()["details"] == {"sequence": 1}


def test_audit_ledger_digest_accepts_lowercase_sha256() -> None:
    assert _ledger_entry().entry_digest.islower() and len(_ledger_entry().entry_digest) == 64


def test_audit_ledger_digest_rejects_uppercase() -> None:
    entry = _ledger_entry()
    with pytest.raises(AuditLedgerVerificationError):
        replace(entry, entry_digest=entry.entry_digest.upper())


def test_audit_ledger_digest_rejects_wrong_length() -> None:
    with pytest.raises(AuditLedgerVerificationError):
        replace(_ledger_entry(), record_digest="0" * 63)


def test_audit_ledger_digest_rejects_non_hex() -> None:
    with pytest.raises(AuditLedgerVerificationError):
        replace(_ledger_entry(), previous_digest="g" * 64)


def test_audit_ledger_digest_rejects_non_string() -> None:
    with pytest.raises(AuditLedgerVerificationError):
        replace(_ledger_entry(), entry_digest=cast(Any, b"0" * 64))


def test_audit_ledger_digest_rejects_whitespace() -> None:
    with pytest.raises(AuditLedgerVerificationError):
        replace(_ledger_entry(), record_digest=" " + "0" * 63)


def test_audit_ledger_digest_rejects_prefix() -> None:
    with pytest.raises(AuditLedgerVerificationError):
        replace(_ledger_entry(), previous_digest="0x" + "0" * 62)


def test_audit_ledger_entry_digest_is_deterministic() -> None:
    ledger_id, record = AuditLedgerId(), _record()
    assert _ledger_entry(ledger_id=ledger_id, record=record) == _ledger_entry(
        ledger_id=ledger_id, record=record
    )


def test_audit_ledger_entry_digest_normalizes_datetime_to_utc() -> None:
    ledger_id, record = AuditLedgerId(), _record()
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    shifted = instant.astimezone(timezone(timedelta(hours=3)))
    assert (
        _ledger_entry(ledger_id=ledger_id, record=record, appended_at=instant).entry_digest
        == _ledger_entry(ledger_id=ledger_id, record=record, appended_at=shifted).entry_digest
    )


def test_audit_ledger_entry_digest_changes_with_ledger_id() -> None:
    record = _record()
    assert _ledger_entry(record=record).entry_digest != _ledger_entry(record=record).entry_digest


def test_audit_ledger_entry_digest_changes_with_sequence() -> None:
    first = _ledger_entry()
    second = _ledger_entry(
        ledger_id=first.ledger_id,
        record=first.record,
        sequence=2,
        previous_digest=first.previous_digest,
    )
    assert first.entry_digest != second.entry_digest


def test_audit_ledger_entry_digest_changes_with_record_digest() -> None:
    ledger_id = AuditLedgerId()
    assert (
        _ledger_entry(ledger_id=ledger_id).entry_digest
        != _ledger_entry(
            ledger_id=ledger_id, record=replace(_record(), record_id=AuditRecordId())
        ).entry_digest
    )


def test_audit_ledger_entry_digest_changes_with_previous_digest() -> None:
    first = _ledger_entry()
    second = _ledger_entry(
        ledger_id=first.ledger_id,
        record=first.record,
        sequence=2,
        previous_digest="1" * 64,
    )
    assert first.entry_digest != second.entry_digest


def test_audit_ledger_entry_digest_changes_with_appended_time() -> None:
    ledger_id, record = AuditLedgerId(), _record()
    assert (
        _ledger_entry(ledger_id=ledger_id, record=record).entry_digest
        != _ledger_entry(
            ledger_id=ledger_id,
            record=record,
            appended_at=datetime(2026, 1, 2, tzinfo=UTC),
        ).entry_digest
    )


def test_audit_ledger_entry_valid() -> None:
    assert _ledger_entry().sequence == 1


def test_audit_ledger_entry_factory() -> None:
    entry = _ledger_entry()
    assert entry.record_digest == entry.record.integrity_digest()


def test_audit_ledger_entry_requires_ledger_id() -> None:
    with pytest.raises(AuditLedgerError):
        replace(_ledger_entry(), ledger_id=cast(Any, AuditRecordId()))


def test_audit_ledger_entry_rejects_boolean_sequence() -> None:
    with pytest.raises(AuditLedgerError):
        replace(_ledger_entry(), sequence=True)


def test_audit_ledger_entry_rejects_zero_sequence() -> None:
    with pytest.raises(AuditLedgerError):
        replace(_ledger_entry(), sequence=0)


def test_audit_ledger_entry_requires_record() -> None:
    with pytest.raises(AuditLedgerError):
        replace(_ledger_entry(), record=cast(Any, object()))


def test_audit_ledger_entry_rejects_naive_append_time() -> None:
    with pytest.raises(AuditLedgerError):
        _ledger_entry(appended_at=datetime(2026, 1, 1))


def test_audit_ledger_entry_first_requires_genesis_digest() -> None:
    with pytest.raises(AuditLedgerVerificationError):
        _ledger_entry(previous_digest="1" * 64)


def test_audit_ledger_entry_rejects_record_digest_mismatch() -> None:
    with pytest.raises(AuditLedgerVerificationError):
        replace(_ledger_entry(), record_digest="1" * 64)


def test_audit_ledger_entry_rejects_entry_digest_mismatch() -> None:
    with pytest.raises(AuditLedgerVerificationError):
        replace(_ledger_entry(), entry_digest="1" * 64)


def test_audit_ledger_entry_is_immutable() -> None:
    def mutate(entry: object) -> None:
        delattr(entry, "sequence")

    with pytest.raises(FrozenInstanceError):
        mutate(_ledger_entry())


def test_audit_ledger_entry_serialization() -> None:
    assert set(_ledger_entry().to_dict(_safe_policy())) == {
        "ledger_id",
        "sequence",
        "record",
        "appended_at",
        "previous_digest",
        "record_digest",
        "entry_digest",
    }


def test_audit_ledger_entry_round_trip_without_redaction() -> None:
    entry = _ledger_entry()
    assert AuditLedgerEntry.from_dict(entry.to_dict(_safe_policy())) == entry


def test_audit_ledger_entry_serialization_is_independent() -> None:
    entry = _ledger_entry(record=replace(_record(), attributes={"nested": [1]}))
    rendered = entry.to_dict(_safe_policy())
    cast(dict[str, object], cast(dict[str, object], rendered["record"])["attributes"])[
        "nested"
    ] = []
    assert entry.record.attributes["nested"] == (1,)


def test_audit_ledger_entry_rendering_redacts_record() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    rendered = _ledger_entry(record=replace(_record(), attributes={"password": secret})).to_dict()
    assert secret not in str(rendered)


def test_redacted_audit_ledger_entry_is_display_only() -> None:
    entry = _ledger_entry(record=replace(_record(), attributes={"password": "secret"}))
    with pytest.raises(AuditSerializationError):
        AuditLedgerEntry.from_dict(entry.to_dict())


def test_audit_ledger_entry_from_dict_uses_strict_types() -> None:
    value = _ledger_entry().to_dict(_safe_policy())
    value["sequence"] = True
    with pytest.raises(AuditSerializationError):
        AuditLedgerEntry.from_dict(value)


def test_audit_ledger_verification_failure_exact_values() -> None:
    assert [item.value for item in AuditLedgerVerificationFailure] == [
        "ledger_id_mismatch",
        "genesis_mismatch",
        "sequence_mismatch",
        "previous_digest_mismatch",
        "record_digest_mismatch",
        "entry_digest_mismatch",
        "appended_time_regression",
        "duplicate_record_id",
        "head_digest_mismatch",
        "entry_count_mismatch",
    ]


def test_audit_ledger_verification_failure_parsing() -> None:
    assert (
        AuditLedgerVerificationFailure.parse("genesis_mismatch")
        is AuditLedgerVerificationFailure.GENESIS_MISMATCH
    )


def test_audit_ledger_verification_failure_rejects_unknown() -> None:
    with pytest.raises(AuditValidationError):
        AuditLedgerVerificationFailure.parse("unknown")


def test_audit_ledger_verification_failure_rejects_non_string() -> None:
    with pytest.raises(AuditValidationError):
        AuditLedgerVerificationFailure.parse(cast(Any, 1))


def test_audit_ledger_verification_result_empty_valid() -> None:
    result = AuditLedgerVerificationResult(True, 0, AuditLedgerId(), None, None, None)
    assert result.valid and result.checked_entry_count == 0


def test_audit_ledger_verification_result_non_empty_valid() -> None:
    result = AuditLedgerVerificationResult(True, 1, AuditLedgerId(), 1, 1, "1" * 64)
    assert result.head_digest == "1" * 64


def test_audit_ledger_verification_result_invalid() -> None:
    result = AuditLedgerVerificationResult(
        False,
        1,
        AuditLedgerId(),
        1,
        1,
        "1" * 64,
        AuditLedgerVerificationFailure.ENTRY_DIGEST_MISMATCH,
        1,
    )
    assert not result.valid and result.failure_sequence == 1


def test_audit_ledger_verification_result_rejects_invalid_consistency() -> None:
    with pytest.raises(AuditLedgerError):
        AuditLedgerVerificationResult(True, 1, AuditLedgerId(), 1, 1, None)


def test_audit_ledger_verification_result_serialization() -> None:
    result = AuditLedgerVerificationResult(True, 0, AuditLedgerId(), None, None, None)
    assert result.to_dict()["failure"] is None


def test_audit_ledger_verification_result_round_trip() -> None:
    result = AuditLedgerVerificationResult(True, 1, AuditLedgerId(), 1, 1, "1" * 64)
    assert AuditLedgerVerificationResult.from_dict(result.to_dict()) == result


def test_audit_ledger_verification_result_require_valid() -> None:
    result = AuditLedgerVerificationResult(True, 0, AuditLedgerId(), None, None, None)
    result.require_valid()
    assert result.valid


def test_audit_ledger_verification_result_require_valid_raises() -> None:
    result = AuditLedgerVerificationResult(
        False,
        0,
        AuditLedgerId(),
        None,
        None,
        None,
        AuditLedgerVerificationFailure.ENTRY_COUNT_MISMATCH,
    )
    with pytest.raises(AuditLedgerVerificationError):
        result.require_valid()


def test_audit_ledger_status_empty() -> None:
    status = AuditLedgerStatus(
        AuditLedgerId(), 0, 10, 1, None, False, datetime(2026, 1, 1, tzinfo=UTC), None
    )
    assert status.entry_count == 0 and status.head_digest is None


def test_audit_ledger_status_populated() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    status = AuditLedgerStatus(AuditLedgerId(), 1, 10, 2, "1" * 64, False, instant, instant)
    assert status.next_sequence == 2


def test_audit_ledger_status_rejects_boolean_count() -> None:
    with pytest.raises(AuditLedgerError):
        AuditLedgerStatus(
            AuditLedgerId(), True, 10, 2, "1" * 64, False, datetime.now(UTC), datetime.now(UTC)
        )


def test_audit_ledger_status_rejects_count_over_capacity() -> None:
    with pytest.raises(AuditLedgerError):
        AuditLedgerStatus(
            AuditLedgerId(), 2, 1, 3, "1" * 64, False, datetime.now(UTC), datetime.now(UTC)
        )


def test_audit_ledger_status_rejects_wrong_next_sequence() -> None:
    with pytest.raises(AuditLedgerError):
        AuditLedgerStatus(AuditLedgerId(), 0, 1, 2, None, False, datetime.now(UTC), None)


def test_audit_ledger_status_rejects_empty_with_head() -> None:
    with pytest.raises(AuditLedgerError):
        AuditLedgerStatus(AuditLedgerId(), 0, 1, 1, "1" * 64, False, datetime.now(UTC), None)


def test_audit_ledger_status_rejects_non_empty_without_head() -> None:
    with pytest.raises(AuditLedgerError):
        AuditLedgerStatus(
            AuditLedgerId(), 1, 1, 2, None, False, datetime.now(UTC), datetime.now(UTC)
        )


def test_audit_ledger_status_rejects_naive_creation_time() -> None:
    with pytest.raises(AuditLedgerError):
        AuditLedgerStatus(AuditLedgerId(), 0, 1, 1, None, False, datetime.now(), None)


def test_audit_ledger_status_rejects_last_append_before_creation() -> None:
    created = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(AuditLedgerError):
        AuditLedgerStatus(
            AuditLedgerId(),
            1,
            1,
            2,
            "1" * 64,
            False,
            created,
            created - timedelta(seconds=1),
        )


def test_audit_ledger_status_serialization() -> None:
    status = AuditLedgerStatus(
        AuditLedgerId(), 0, 10, 1, None, False, datetime(2026, 1, 1, tzinfo=UTC), None
    )
    assert status.to_dict()["next_sequence"] == 1


def test_audit_ledger_status_round_trip() -> None:
    status = AuditLedgerStatus(
        AuditLedgerId(), 0, 10, 1, None, False, datetime(2026, 1, 1, tzinfo=UTC), None
    )
    assert AuditLedgerStatus.from_dict(status.to_dict()) == status


@pytest.mark.asyncio
async def test_in_memory_audit_ledger_default_construction() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    assert (await ledger.status()).entry_count == 0


@pytest.mark.asyncio
async def test_in_memory_audit_ledger_explicit_id() -> None:
    identifier = AuditLedgerId()
    ledger = InMemoryAuditLedger(ledger_id=identifier, clock=lambda: datetime.now(UTC))
    assert (await ledger.status()).ledger_id is identifier


def test_in_memory_audit_ledger_rejects_wrong_id() -> None:
    with pytest.raises(AuditLedgerError):
        InMemoryAuditLedger(ledger_id=cast(Any, AuditRecordId()))


def test_in_memory_audit_ledger_rejects_zero_capacity() -> None:
    with pytest.raises(AuditLedgerError):
        InMemoryAuditLedger(maximum_entries=0)


def test_in_memory_audit_ledger_rejects_boolean_capacity() -> None:
    with pytest.raises(AuditLedgerError):
        InMemoryAuditLedger(maximum_entries=True)


def test_in_memory_audit_ledger_rejects_excessive_capacity() -> None:
    with pytest.raises(AuditLedgerCapacityError):
        InMemoryAuditLedger(maximum_entries=1_000_001)


def test_in_memory_audit_ledger_rejects_naive_constructor_clock() -> None:
    with pytest.raises(AuditLedgerError):
        InMemoryAuditLedger(clock=datetime.now)


@pytest.mark.asyncio
async def test_in_memory_audit_ledgers_are_isolated() -> None:
    first = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    second = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await first.append(_record())
    assert await second.entries() == ()


@pytest.mark.asyncio
async def test_audit_ledger_append_first_entry() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = InMemoryAuditLedger(clock=lambda: instant)
    entry = await ledger.append(_record())
    assert entry.sequence == 1 and entry.previous_digest == "0" * 64


@pytest.mark.asyncio
async def test_audit_ledger_append_sequence_is_contiguous() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entries = (
        await ledger.append(_record()),
        await ledger.append(replace(_record(), record_id=AuditRecordId())),
    )
    assert tuple(item.sequence for item in entries) == (1, 2)


@pytest.mark.asyncio
async def test_audit_ledger_append_links_previous_digest() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    first = await ledger.append(_record())
    second = await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert second.previous_digest == first.entry_digest


@pytest.mark.asyncio
async def test_audit_ledger_append_preserves_record_identity() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    assert (await ledger.append(record)).record is record


@pytest.mark.asyncio
async def test_audit_ledger_append_allows_older_occurrence_time() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime(2026, 2, 1, tzinfo=UTC))
    assert (await ledger.append(_record())).sequence == 1


@pytest.mark.asyncio
async def test_audit_ledger_append_allows_equal_append_time() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = InMemoryAuditLedger(clock=lambda: instant)
    await ledger.append(_record())
    assert (
        await ledger.append(replace(_record(), record_id=AuditRecordId()))
    ).appended_at == instant


@pytest.mark.asyncio
async def test_audit_ledger_append_rejects_backward_clock() -> None:
    later, earlier = datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    ledger = InMemoryAuditLedger(clock=_LedgerClock(later, earlier))
    with pytest.raises(AuditLedgerError):
        await ledger.append(_record())


@pytest.mark.asyncio
async def test_audit_ledger_append_rejects_duplicate_record_id() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    await ledger.append(record)
    with pytest.raises(ResourceConflictError):
        await ledger.append(record)


@pytest.mark.asyncio
async def test_audit_ledger_append_rejects_wrong_record_type() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    with pytest.raises(AuditLedgerError):
        await ledger.append(cast(Any, object()))


@pytest.mark.asyncio
async def test_audit_ledger_capacity_limit() -> None:
    ledger = InMemoryAuditLedger(maximum_entries=1, clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    with pytest.raises(AuditLedgerCapacityError):
        await ledger.append(replace(_record(), record_id=AuditRecordId()))


@pytest.mark.asyncio
async def test_audit_ledger_failed_append_does_not_consume_sequence() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    record = _record()
    await ledger.append(record)
    with pytest.raises(ResourceConflictError):
        await ledger.append(record)
    assert (await ledger.append(replace(record, record_id=AuditRecordId()))).sequence == 2


@pytest.mark.asyncio
async def test_audit_ledger_failed_append_does_not_change_head() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    await ledger.append(record)
    before = (await ledger.status()).head_digest
    with pytest.raises(ResourceConflictError):
        await ledger.append(record)
    assert (await ledger.status()).head_digest == before


@pytest.mark.asyncio
async def test_audit_ledger_failed_append_does_not_change_status() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    await ledger.append(record)
    before = await ledger.status()
    with pytest.raises(ResourceConflictError):
        await ledger.append(record)
    assert await ledger.status() == before


@pytest.mark.asyncio
async def test_audit_ledger_concurrent_final_slot() -> None:
    ledger = InMemoryAuditLedger(maximum_entries=1, clock=lambda: datetime.now(UTC))
    results = await asyncio.gather(
        ledger.append(_record()),
        ledger.append(replace(_record(), record_id=AuditRecordId())),
        return_exceptions=True,
    )
    assert sum(isinstance(item, AuditLedgerEntry) for item in results) == 1


@pytest.mark.asyncio
async def test_audit_ledger_concurrent_duplicate_record_id() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    results = await asyncio.gather(
        ledger.append(record), ledger.append(record), return_exceptions=True
    )
    assert sum(isinstance(item, ResourceConflictError) for item in results) == 1


@pytest.mark.asyncio
async def test_audit_ledger_get() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    assert await ledger.get(1) is entry


@pytest.mark.asyncio
async def test_audit_ledger_get_optional_existing() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    assert await ledger.get_optional(1) is entry


@pytest.mark.asyncio
async def test_audit_ledger_get_optional_missing() -> None:
    assert await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).get_optional(1) is None


@pytest.mark.asyncio
async def test_audit_ledger_get_missing() -> None:
    with pytest.raises(ResourceNotFoundError):
        await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).get(1)


@pytest.mark.asyncio
async def test_audit_ledger_get_rejects_boolean_sequence() -> None:
    with pytest.raises(AuditLedgerError):
        await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).get(True)


@pytest.mark.asyncio
async def test_audit_ledger_get_rejects_zero_sequence() -> None:
    with pytest.raises(AuditLedgerError):
        await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).get(0)


@pytest.mark.asyncio
async def test_audit_ledger_get_rejects_negative_sequence() -> None:
    with pytest.raises(AuditLedgerError):
        await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).get(-1)


@pytest.mark.asyncio
async def test_audit_ledger_get_rejects_float_sequence() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    with pytest.raises(AuditLedgerError):
        await ledger.get(cast(Any, 1.0))


@pytest.mark.asyncio
async def test_audit_ledger_get_optional_rejects_numeric_string() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    with pytest.raises(AuditLedgerError):
        await ledger.get_optional(cast(Any, "1"))


@pytest.mark.asyncio
async def test_audit_ledger_get_by_record_id() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    entry = await ledger.append(record)
    assert await ledger.get_by_record_id(record.record_id) is entry


@pytest.mark.asyncio
async def test_audit_ledger_get_optional_by_record_id_existing() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    entry = await ledger.append(record)
    assert await ledger.get_optional_by_record_id(record.record_id) is entry


@pytest.mark.asyncio
async def test_audit_ledger_get_optional_by_record_id_missing() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    assert await ledger.get_optional_by_record_id(AuditRecordId()) is None


@pytest.mark.asyncio
async def test_audit_ledger_get_by_record_id_missing() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    with pytest.raises(ResourceNotFoundError):
        await ledger.get_by_record_id(AuditRecordId())


@pytest.mark.asyncio
async def test_audit_ledger_get_by_record_id_rejects_wrong_identifier() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    with pytest.raises(AuditLedgerError):
        await ledger.get_by_record_id(cast(Any, AuditLedgerId()))


@pytest.mark.asyncio
async def test_audit_ledger_entries_empty() -> None:
    assert await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).entries() == ()


@pytest.mark.asyncio
async def test_audit_ledger_entries_sequence_order() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert tuple(item.sequence for item in await ledger.entries()) == (1, 2)


@pytest.mark.asyncio
async def test_audit_ledger_entries_returns_immutable_tuple() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert isinstance(await ledger.entries(), tuple)


@pytest.mark.asyncio
async def test_audit_ledger_entries_snapshot_is_independent() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    snapshot = await ledger.entries()
    await ledger.append(_record())
    assert snapshot == ()


@pytest.mark.asyncio
async def test_audit_ledger_verify_empty() -> None:
    result = await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).verify()
    assert result.valid and result.checked_entry_count == 0


@pytest.mark.asyncio
async def test_audit_ledger_verify_single_entry() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert (await ledger.verify()).valid


@pytest.mark.asyncio
async def test_audit_ledger_verify_multiple_entries() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert (await ledger.verify()).checked_entry_count == 2


@pytest.mark.asyncio
async def test_audit_ledger_verify_is_deterministic() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.verify() == await ledger.verify()


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_ledger_id_mismatch() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    object.__setattr__(entry, "ledger_id", AuditLedgerId())
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.LEDGER_ID_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_sequence_mismatch() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    object.__setattr__(entry, "sequence", 2)
    result = await ledger.verify()
    assert result.failure is AuditLedgerVerificationFailure.SEQUENCE_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_genesis_mismatch() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    object.__setattr__(entry, "previous_digest", "1" * 64)
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.GENESIS_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_previous_digest_mismatch() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    second = await ledger.append(replace(_record(), record_id=AuditRecordId()))
    object.__setattr__(second, "previous_digest", "1" * 64)
    result = await ledger.verify()
    assert result.failure is AuditLedgerVerificationFailure.PREVIOUS_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_duplicate_record_id() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    first = await ledger.append(_record())
    second = await ledger.append(replace(_record(), record_id=AuditRecordId()))
    object.__setattr__(second, "record", first.record)
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.DUPLICATE_RECORD_ID


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_record_digest_mismatch() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    object.__setattr__(entry, "record_digest", "1" * 64)
    result = await ledger.verify()
    assert result.failure is AuditLedgerVerificationFailure.RECORD_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_entry_digest_mismatch() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    object.__setattr__(entry, "entry_digest", "1" * 64)
    result = await ledger.verify()
    assert result.failure is AuditLedgerVerificationFailure.ENTRY_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_append_time_regression() -> None:
    created = datetime(2026, 1, 2, tzinfo=UTC)
    ledger = InMemoryAuditLedger(clock=lambda: created)
    first = await ledger.append(_record())
    second_record = replace(_record(), record_id=AuditRecordId())
    second = AuditLedgerEntry.create(
        ledger_id=first.ledger_id,
        sequence=2,
        record=second_record,
        appended_at=created - timedelta(seconds=1),
        previous_digest=first.entry_digest,
    )
    ledger._entries.append(second)
    ledger._entry_count = 2
    ledger._head_digest = second.entry_digest
    assert (
        await ledger.verify()
    ).failure is AuditLedgerVerificationFailure.APPENDED_TIME_REGRESSION


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_head_digest_mismatch() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    ledger._head_digest = "1" * 64
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.HEAD_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verify_detects_entry_count_mismatch() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    ledger._entry_count = 2
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.ENTRY_COUNT_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verification_error_omits_record_values() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(replace(_record(), summary=secret))
    object.__setattr__(entry, "entry_digest", "1" * 64)
    result = await ledger.verify()
    with pytest.raises(AuditLedgerVerificationError) as captured:
        result.require_valid()
    assert secret not in str(captured.value.to_dict())


@pytest.mark.asyncio
async def test_audit_ledger_initial_status() -> None:
    status = await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).status()
    assert status.next_sequence == 1 and not status.closed


@pytest.mark.asyncio
async def test_audit_ledger_status_after_append() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    status = await ledger.status()
    assert status.entry_count == 1 and status.head_digest == entry.entry_digest


@pytest.mark.asyncio
async def test_audit_ledger_old_status_unchanged_after_append() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    old = await ledger.status()
    await ledger.append(_record())
    assert old.entry_count == 0


@pytest.mark.asyncio
async def test_audit_ledger_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    assert (await ledger.status()).closed


@pytest.mark.asyncio
async def test_audit_ledger_repeated_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    await ledger.close()
    assert (await ledger.status()).closed


@pytest.mark.asyncio
async def test_audit_ledger_append_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    with pytest.raises(AuditLedgerClosedError):
        await ledger.append(_record())


@pytest.mark.asyncio
async def test_audit_ledger_get_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    await ledger.close()
    assert await ledger.get(1) is entry


@pytest.mark.asyncio
async def test_audit_ledger_entries_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.close()
    assert len(await ledger.entries()) == 1


@pytest.mark.asyncio
async def test_audit_ledger_verify_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    assert (await ledger.verify()).valid


@pytest.mark.asyncio
async def test_audit_ledger_status_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    before = await ledger.status()
    await ledger.close()
    after = await ledger.status()
    assert after.closed and after.created_at == before.created_at


@pytest.mark.asyncio
async def test_audit_ledger_close_does_not_clear_entries() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.close()
    assert (await ledger.status()).entry_count == 1


@pytest.mark.asyncio
async def test_audit_ledger_concurrent_append_and_close_is_consistent() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    results = await asyncio.gather(ledger.append(_record()), ledger.close(), return_exceptions=True)
    assert (await ledger.status()).closed and len(await ledger.entries()) in (0, 1)
    assert not any(isinstance(item, BaseException) for item in results)


@pytest.mark.asyncio
async def test_audit_ledger_duplicate_error_omits_secret() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _secret_ledger_record()
    await ledger.append(record)
    with pytest.raises(ResourceConflictError) as captured:
        await ledger.append(record)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


@pytest.mark.asyncio
async def test_audit_ledger_capacity_error_omits_secret() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    ledger = InMemoryAuditLedger(maximum_entries=1, clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    with pytest.raises(AuditLedgerCapacityError) as captured:
        await ledger.append(_secret_ledger_record())
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


@pytest.mark.asyncio
async def test_audit_ledger_closed_error_omits_secret() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    with pytest.raises(AuditLedgerClosedError) as captured:
        await ledger.append(_secret_ledger_record())
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


@pytest.mark.asyncio
async def test_audit_ledger_missing_sequence_error_omits_secret() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_secret_ledger_record())
    with pytest.raises(ResourceNotFoundError) as captured:
        await ledger.get(2)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


@pytest.mark.asyncio
async def test_audit_ledger_missing_record_error_omits_secret() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_secret_ledger_record())
    with pytest.raises(ResourceNotFoundError) as captured:
        await ledger.get_by_record_id(AuditRecordId())
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_ledger_malformed_entry_error_omits_secret() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    entry = _ledger_entry(record=_secret_ledger_record())
    value = entry.to_dict(_safe_policy())
    cast(dict[str, object], value["record"])["summary"] = secret
    value["entry_digest"] = "invalid"
    with pytest.raises(AuditSerializationError) as captured:
        AuditLedgerEntry.from_dict(value)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_ledger_clock_failure_error_omits_secret() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"

    def fail() -> datetime:
        raise RuntimeError(secret)

    with pytest.raises(AuditLedgerError) as captured:
        InMemoryAuditLedger(clock=fail)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_filter_defaults() -> None:
    value = AuditFilter()
    assert value.sequences is None and value.limit is None and value.offset == 0


def test_audit_filter_empty_collections_normalize_to_none() -> None:
    assert AuditFilter(tags_all=frozenset(), record_ids=frozenset()).tags_all is None


def test_audit_filter_sequences() -> None:
    assert AuditFilter(sequences=cast(Any, [2, 1])).sequences == frozenset({1, 2})


def test_audit_filter_rejects_zero_sequence() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(sequences=cast(Any, [0]))


def test_audit_filter_rejects_boolean_sequence() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(sequences=cast(Any, [True]))


def test_audit_filter_rejects_duplicate_sequence_list() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(sequences=cast(Any, [1, 1]))


def test_audit_filter_record_ids() -> None:
    identifier = AuditRecordId()
    assert AuditFilter(record_ids=frozenset({identifier})).record_ids == frozenset({identifier})


def test_audit_filter_rejects_wrong_record_id() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(record_ids=cast(Any, [AuditLedgerId()]))


def test_audit_filter_categories() -> None:
    assert AuditFilter(categories=frozenset({AuditCategory.SECURITY})).categories


def test_audit_filter_severities() -> None:
    assert AuditFilter(severities=frozenset({AuditSeverity.NOTICE})).severities


def test_audit_filter_outcomes() -> None:
    assert AuditFilter(outcomes=frozenset({AuditOutcome.SUCCESS})).outcomes


def test_audit_filter_actor_kinds() -> None:
    assert AuditFilter(actor_kinds=frozenset({AuditActorKind.USER})).actor_kinds


def test_audit_filter_rejects_wrong_category() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(categories=cast(Any, ["security"]))


def test_audit_filter_rejects_wrong_severity() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(severities=cast(Any, [AuditOutcome.SUCCESS]))


def test_audit_filter_rejects_wrong_outcome() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(outcomes=cast(Any, [AuditSeverity.INFO]))


def test_audit_filter_rejects_wrong_actor_kind() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(actor_kinds=cast(Any, [AuditCategory.SYSTEM]))


def test_audit_filter_collections_are_immutable() -> None:
    assert isinstance(AuditFilter(action_names=cast(Any, ["read"])).action_names, frozenset)


def test_audit_filter_caller_collections_are_isolated() -> None:
    values = ["read"]
    filter = AuditFilter(action_names=cast(Any, values))
    values.append("write")
    assert filter.action_names == frozenset({"read"})


def test_audit_filter_actor_identifiers() -> None:
    assert AuditFilter(actor_identifiers=cast(Any, ["Alice"])).actor_identifiers == frozenset(
        {"Alice"}
    )


def test_audit_filter_actor_tenant_ids() -> None:
    identifier = TenantId()
    assert AuditFilter(actor_tenant_ids=cast(Any, [identifier])).actor_tenant_ids == frozenset(
        {identifier}
    )


def test_audit_filter_actor_user_ids() -> None:
    identifier = UserId()
    assert AuditFilter(actor_user_ids=cast(Any, [identifier])).actor_user_ids == frozenset(
        {identifier}
    )


def test_audit_filter_action_names() -> None:
    assert AuditFilter(action_names=cast(Any, ["document.read"])).action_names


def test_audit_filter_source_components() -> None:
    assert AuditFilter(source_components=cast(Any, ["api"])).source_components


def test_audit_filter_source_modules() -> None:
    assert AuditFilter(source_modules=cast(Any, ["documents"])).source_modules


def test_audit_filter_source_operations() -> None:
    assert AuditFilter(source_operations=cast(Any, ["read"])).source_operations


def test_audit_filter_target_kinds() -> None:
    assert AuditFilter(target_kinds=cast(Any, ["document"])).target_kinds


def test_audit_filter_target_identifiers() -> None:
    assert AuditFilter(target_identifiers=cast(Any, ["doc-1"])).target_identifiers


def test_audit_filter_has_target_true() -> None:
    assert AuditFilter(has_target=True).has_target is True


def test_audit_filter_has_target_false() -> None:
    assert AuditFilter(has_target=False).has_target is False


def test_audit_filter_rejects_non_boolean_has_target() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(has_target=cast(Any, 1))


def test_audit_filter_correlation_ids() -> None:
    identifier = CorrelationId()
    assert AuditFilter(correlation_ids=cast(Any, [identifier])).correlation_ids


def test_audit_filter_run_ids() -> None:
    identifier = RunId()
    assert AuditFilter(run_ids=cast(Any, [identifier])).run_ids


def test_audit_filter_tenant_ids() -> None:
    identifier = TenantId()
    assert AuditFilter(tenant_ids=cast(Any, [identifier])).tenant_ids


def test_audit_filter_user_ids() -> None:
    identifier = UserId()
    assert AuditFilter(user_ids=cast(Any, [identifier])).user_ids


def test_audit_filter_trace_ids() -> None:
    identifier = TraceId()
    assert AuditFilter(trace_ids=cast(Any, [identifier])).trace_ids


def test_audit_filter_span_ids() -> None:
    identifier = SpanId()
    assert AuditFilter(span_ids=cast(Any, [identifier])).span_ids


def test_audit_filter_parent_span_ids() -> None:
    identifier = SpanId()
    assert AuditFilter(parent_span_ids=cast(Any, [identifier])).parent_span_ids


def test_audit_filter_rejects_wrong_context_identifier() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(trace_ids=cast(Any, [SpanId()]))


def test_audit_filter_reason_codes() -> None:
    assert AuditFilter(reason_codes=cast(Any, ["allowed"])).reason_codes


def test_audit_filter_tags_all() -> None:
    assert AuditFilter(tags_all=cast(Any, ["security"])).tags_all


def test_audit_filter_tags_any() -> None:
    assert AuditFilter(tags_any=cast(Any, ["access"])).tags_any


def test_audit_filter_rejects_invalid_reason_code() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(reason_codes=cast(Any, ["bad reason"]))


def test_audit_filter_rejects_invalid_tag() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(tags_all=cast(Any, ["bad tag"]))


def test_audit_filter_rejects_duplicate_tag_sequence() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(tags_any=cast(Any, ["access", "access"]))


def test_audit_filter_combines_tags_all_and_any() -> None:
    filter = AuditFilter(tags_all=cast(Any, ["security"]), tags_any=cast(Any, ["access"]))
    assert filter.matches(_filter_entry())


def test_audit_filter_record_attributes_are_immutable() -> None:
    with pytest.raises(TypeError):
        cast(
            dict[str, object], AuditFilter(record_attribute_equals={"x": 1}).record_attribute_equals
        )["x"] = 2


def test_audit_filter_actor_attributes_are_immutable() -> None:
    assert isinstance(AuditFilter(actor_attribute_equals={"x": 1}).actor_attribute_equals, Mapping)


def test_audit_filter_action_attributes_are_immutable() -> None:
    assert AuditFilter(action_attribute_equals={"nested": [1]}).action_attribute_equals[
        "nested"
    ] == (1,)


def test_audit_filter_target_attributes_are_immutable() -> None:
    with pytest.raises(TypeError):
        cast(dict[str, object], AuditFilter(target_attribute_equals={}).target_attribute_equals)[
            "x"
        ] = 1


def test_audit_filter_context_metadata_is_immutable() -> None:
    assert (
        type(AuditFilter(context_metadata_equals={}).context_metadata_equals).__name__
        == "mappingproxy"
    )


def test_audit_filter_rejects_non_mapping_record_attributes() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(record_attribute_equals=cast(Any, []))


def test_audit_filter_rejects_non_mapping_actor_attributes() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(actor_attribute_equals=cast(Any, False))


def test_audit_filter_rejects_non_mapping_action_attributes() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(action_attribute_equals=cast(Any, ""))


def test_audit_filter_rejects_non_mapping_target_attributes() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(target_attribute_equals=cast(Any, ()))


def test_audit_filter_rejects_non_mapping_context_metadata() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(context_metadata_equals=cast(Any, 0))


def test_audit_filter_attribute_caller_mapping_isolated() -> None:
    values = {"nested": [1]}
    filter = AuditFilter(record_attribute_equals=values)
    values["nested"].append(2)
    assert filter.record_attribute_equals["nested"] == (1,)


def test_audit_filter_rejects_unsupported_attribute_value() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(record_attribute_equals={"invalid": object()})


def test_audit_filter_attribute_error_omits_secret() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"

    class Secret(Enum):
        VALUE = secret

    with pytest.raises(AuditFilterError) as captured:
        AuditFilter(record_attribute_equals={"nested": [Secret.VALUE]})
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_filter_occurred_range() -> None:
    after, before = datetime(2025, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    assert AuditFilter(occurred_after=after, occurred_before=before).occurred_after == after


def test_audit_filter_appended_range() -> None:
    after, before = datetime(2025, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    assert AuditFilter(appended_after=after, appended_before=before).appended_before == before


def test_audit_filter_rejects_naive_occurred_after() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(occurred_after=datetime.now())


def test_audit_filter_rejects_naive_appended_before() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(appended_before=datetime.now())


def test_audit_filter_rejects_inverted_occurred_range() -> None:
    instant = datetime.now(UTC)
    with pytest.raises(AuditFilterError):
        AuditFilter(occurred_after=instant, occurred_before=instant)


def test_audit_filter_rejects_inverted_appended_range() -> None:
    instant = datetime.now(UTC)
    with pytest.raises(AuditFilterError):
        AuditFilter(appended_after=instant, appended_before=instant)


def test_audit_filter_minimum_sequence() -> None:
    assert AuditFilter(minimum_sequence=2).minimum_sequence == 2


def test_audit_filter_maximum_sequence() -> None:
    assert AuditFilter(maximum_sequence=3).maximum_sequence == 3


def test_audit_filter_rejects_inverted_sequence_range() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(minimum_sequence=3, maximum_sequence=2)


def test_audit_filter_accepts_limit_zero() -> None:
    assert AuditFilter(limit=0).limit == 0


def test_audit_filter_rejects_negative_limit() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(limit=-1)


def test_audit_filter_rejects_boolean_limit() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(limit=cast(Any, False))


def test_audit_filter_rejects_negative_offset() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(offset=-1)


def test_audit_filter_rejects_boolean_offset() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter(offset=cast(Any, True))


def test_audit_filter_matches_sequence() -> None:
    assert AuditFilter(sequences=frozenset({1})).matches(_filter_entry())


def test_audit_filter_matches_record_id() -> None:
    entry = _filter_entry()
    assert AuditFilter(record_ids=frozenset({entry.record.record_id})).matches(entry)


def test_audit_filter_matches_category() -> None:
    assert AuditFilter(categories=frozenset({AuditCategory.SECURITY})).matches(_filter_entry())


def test_audit_filter_matches_severity() -> None:
    assert AuditFilter(severities=frozenset({AuditSeverity.NOTICE})).matches(_filter_entry())


def test_audit_filter_matches_outcome() -> None:
    assert AuditFilter(outcomes=frozenset({AuditOutcome.SUCCESS})).matches(_filter_entry())


def test_audit_filter_matches_actor_kind() -> None:
    assert AuditFilter(actor_kinds=frozenset({AuditActorKind.USER})).matches(_filter_entry())


def test_audit_filter_matches_actor_identifier() -> None:
    assert AuditFilter(actor_identifiers=frozenset({"Alice"})).matches(_filter_entry())


def test_audit_filter_matches_actor_scope() -> None:
    entry = _filter_entry()
    filter = AuditFilter(
        actor_tenant_ids=frozenset({cast(TenantId, entry.record.actor.tenant_id)}),
        actor_user_ids=frozenset({cast(UserId, entry.record.actor.user_id)}),
    )
    assert filter.matches(entry)


def test_audit_filter_matches_action_name() -> None:
    assert AuditFilter(action_names=frozenset({"document.read"})).matches(_filter_entry())


def test_audit_filter_matches_source() -> None:
    filter = AuditFilter(
        source_components=frozenset({"api"}),
        source_modules=frozenset({"documents"}),
        source_operations=frozenset({"read"}),
    )
    assert filter.matches(_filter_entry())


def test_audit_filter_matches_target() -> None:
    filter = AuditFilter(
        target_kinds=frozenset({"document"}), target_identifiers=frozenset({"doc-1"})
    )
    assert filter.matches(_filter_entry())


def test_audit_filter_matches_has_target() -> None:
    assert AuditFilter(has_target=True).matches(_filter_entry())


def test_audit_filter_matches_context_scope() -> None:
    entry = _filter_entry()
    assert AuditFilter(run_ids=frozenset({cast(RunId, entry.record.context.run_id)})).matches(entry)


def test_audit_filter_matches_reason_code() -> None:
    assert AuditFilter(reason_codes=frozenset({"allowed"})).matches(_filter_entry())


def test_audit_filter_matches_tags_all() -> None:
    assert AuditFilter(tags_all=frozenset({"security", "access"})).matches(_filter_entry())


def test_audit_filter_matches_tags_any() -> None:
    assert AuditFilter(tags_any=frozenset({"access", "other"})).matches(_filter_entry())


def test_audit_filter_matches_record_attribute_subset() -> None:
    assert AuditFilter(record_attribute_equals={"region": "eu"}).matches(_filter_entry())


def test_audit_filter_matches_actor_attribute_subset() -> None:
    assert AuditFilter(actor_attribute_equals={"role": "admin"}).matches(_filter_entry())


def test_audit_filter_matches_action_attribute_subset() -> None:
    assert AuditFilter(action_attribute_equals={"mode": "safe"}).matches(_filter_entry())


def test_audit_filter_matches_target_attribute_subset() -> None:
    assert AuditFilter(target_attribute_equals={"class": "internal"}).matches(_filter_entry())


def test_audit_filter_matches_context_metadata_subset() -> None:
    assert AuditFilter(context_metadata_equals={"environment": "test"}).matches(_filter_entry())


def test_audit_filter_matches_occurred_time() -> None:
    assert AuditFilter(
        occurred_after=datetime(2025, 1, 1, tzinfo=UTC),
        occurred_before=datetime(2027, 1, 1, tzinfo=UTC),
    ).matches(_filter_entry())


def test_audit_filter_matches_appended_time() -> None:
    assert AuditFilter(
        appended_after=datetime(2025, 1, 1, tzinfo=UTC),
        appended_before=datetime(2027, 1, 1, tzinfo=UTC),
    ).matches(_filter_entry())


def test_audit_filter_matches_sequence_range() -> None:
    assert AuditFilter(minimum_sequence=1, maximum_sequence=1).matches(_filter_entry())


def test_audit_filter_combines_all_predicates() -> None:
    entry = _filter_entry()
    filter = AuditFilter(
        sequences=frozenset({1}),
        categories=frozenset({AuditCategory.SECURITY}),
        actor_identifiers=frozenset({"Alice"}),
        action_names=frozenset({"document.read"}),
        has_target=True,
        tags_all=frozenset({"security"}),
        record_attribute_equals={"region": "eu"},
    )
    assert filter.matches(entry)


def test_audit_filter_rejects_wrong_entry_type() -> None:
    with pytest.raises(AuditFilterError):
        AuditFilter().matches(cast(Any, _record()))


def test_audit_filter_pagination_does_not_affect_matches() -> None:
    assert AuditFilter(limit=0, offset=99).matches(_filter_entry())


def test_audit_filter_serialization() -> None:
    rendered = AuditFilter(categories=frozenset({AuditCategory.SECURITY})).to_dict(_safe_policy())
    assert set(rendered) == {
        "sequences",
        "record_ids",
        "categories",
        "severities",
        "outcomes",
        "actor_kinds",
        "actor_identifiers",
        "actor_tenant_ids",
        "actor_user_ids",
        "action_names",
        "source_components",
        "source_modules",
        "source_operations",
        "target_kinds",
        "target_identifiers",
        "has_target",
        "correlation_ids",
        "run_ids",
        "tenant_ids",
        "user_ids",
        "trace_ids",
        "span_ids",
        "parent_span_ids",
        "reason_codes",
        "tags_all",
        "tags_any",
        "record_attribute_equals",
        "actor_attribute_equals",
        "action_attribute_equals",
        "target_attribute_equals",
        "context_metadata_equals",
        "occurred_after",
        "occurred_before",
        "appended_after",
        "appended_before",
        "minimum_sequence",
        "maximum_sequence",
        "limit",
        "offset",
    }


def test_audit_filter_round_trip() -> None:
    filter = AuditFilter(
        sequences=frozenset({2, 1}),
        categories=frozenset({AuditCategory.SECURITY}),
        actor_identifiers=frozenset({"Alice"}),
        record_attribute_equals={"x": [1]},
        limit=5,
        offset=1,
    )
    assert AuditFilter.from_dict(filter.to_dict(_safe_policy())) == filter


def test_audit_filter_serialization_is_deterministic() -> None:
    first = AuditFilter(tags_any=frozenset({"b", "a"})).to_dict(_safe_policy())
    second = AuditFilter(tags_any=frozenset({"a", "b"})).to_dict(_safe_policy())
    assert first == second and first["tags_any"] == ["a", "b"]


def test_audit_filter_serialization_is_independent() -> None:
    filter = AuditFilter(record_attribute_equals={"nested": [1]})
    rendered = filter.to_dict(_safe_policy())
    cast(dict[str, object], rendered["record_attribute_equals"])["nested"] = []
    assert filter.record_attribute_equals["nested"] == (1,)


def test_audit_filter_rendering_redacts_actor_identifiers() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    policy = RedactionPolicy(
        rules=(RedactionRule("query-secret", value_patterns=(secret,)),),
        include_default_rules=False,
    )
    assert secret not in str(AuditFilter(actor_identifiers=frozenset({secret})).to_dict(policy))


def test_audit_filter_rendering_redacts_target_identifiers() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    policy = RedactionPolicy(
        rules=(RedactionRule("query-secret", value_patterns=(secret,)),),
        include_default_rules=False,
    )
    assert secret not in str(AuditFilter(target_identifiers=frozenset({secret})).to_dict(policy))


def test_audit_filter_rendering_redacts_attribute_predicates() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    rendered = AuditFilter(record_attribute_equals={"password": secret}).to_dict()
    assert secret not in str(rendered)


def test_audit_filter_from_dict_uses_strict_types() -> None:
    value = AuditFilter().to_dict(_safe_policy())
    value["sequences"] = "1"
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_filter_from_dict_rejects_duplicate_sequences() -> None:
    value = AuditFilter().to_dict(_safe_policy())
    value["sequences"] = [1, 1]
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_filter_from_dict_rejects_duplicate_identifiers() -> None:
    identifier = AuditRecordId()
    value = AuditFilter().to_dict(_safe_policy())
    value["record_ids"] = [str(identifier), str(identifier)]
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_filter_from_dict_rejects_duplicate_enums() -> None:
    value = AuditFilter().to_dict(_safe_policy())
    value["categories"] = ["security", "security"]
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_filter_from_dict_rejects_invalid_identifier() -> None:
    value = AuditFilter().to_dict(_safe_policy())
    value["record_ids"] = ["invalid"]
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_filter_from_dict_rejects_unknown_enum() -> None:
    value = AuditFilter().to_dict(_safe_policy())
    value["categories"] = ["unknown"]
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_filter_from_dict_rejects_non_boolean_flag() -> None:
    value = AuditFilter().to_dict(_safe_policy())
    value["has_target"] = 1
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_filter_from_dict_rejects_naive_time() -> None:
    value = AuditFilter().to_dict(_safe_policy())
    value["occurred_after"] = "2026-01-01T00:00:00"
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_filter_from_dict_rejects_boolean_pagination() -> None:
    value = AuditFilter().to_dict(_safe_policy())
    value["limit"] = False
    with pytest.raises(AuditSerializationError):
        AuditFilter.from_dict(value)


def test_audit_ledger_snapshot_empty() -> None:
    assert _empty_snapshot().entries == ()


def test_audit_ledger_snapshot_unfiltered() -> None:
    snapshot = _populated_snapshot()
    assert not snapshot.filtered and snapshot.matching_entry_count == 1


def test_audit_ledger_snapshot_filtered() -> None:
    snapshot = _empty_snapshot(filtered=True)
    assert snapshot.filtered and snapshot.matching_entry_count == 0


def test_audit_ledger_snapshot_rejects_wrong_status() -> None:
    with pytest.raises(AuditSnapshotError):
        AuditLedgerSnapshot(
            cast(Any, object()), (), datetime.now(UTC), False, 0, cast(Any, object())
        )


def test_audit_ledger_snapshot_rejects_wrong_entry() -> None:
    snapshot = _empty_snapshot(filtered=True)
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, entries=cast(Any, (_record(),)), matching_entry_count=1)


def test_audit_ledger_snapshot_rejects_unsorted_entries() -> None:
    ledger_id, instant = AuditLedgerId(), datetime(2026, 1, 1, tzinfo=UTC)
    first = _ledger_entry(ledger_id=ledger_id, appended_at=instant)
    second = _ledger_entry(
        ledger_id=ledger_id,
        sequence=2,
        record=replace(_record(), record_id=AuditRecordId()),
        appended_at=instant,
        previous_digest=first.entry_digest,
    )
    snapshot = _empty_snapshot(filtered=True)
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, entries=(second, first), matching_entry_count=2)


def test_audit_ledger_snapshot_rejects_duplicate_sequence() -> None:
    first = _ledger_entry()
    second = _ledger_entry(
        ledger_id=first.ledger_id, record=replace(_record(), record_id=AuditRecordId())
    )
    snapshot = _empty_snapshot(filtered=True)
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, entries=(first, second), matching_entry_count=2)


def test_audit_ledger_snapshot_rejects_duplicate_record_id() -> None:
    ledger_id, instant, record = AuditLedgerId(), datetime(2026, 1, 1, tzinfo=UTC), _record()
    first = _ledger_entry(ledger_id=ledger_id, record=record, appended_at=instant)
    second = _ledger_entry(
        ledger_id=ledger_id,
        sequence=2,
        record=record,
        appended_at=instant,
        previous_digest=first.entry_digest,
    )
    snapshot = _empty_snapshot(filtered=True)
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, entries=(first, second), matching_entry_count=2)


def test_audit_ledger_snapshot_rejects_entry_from_other_ledger() -> None:
    snapshot = _empty_snapshot(filtered=True)
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, entries=(_ledger_entry(),), matching_entry_count=1)


def test_audit_ledger_snapshot_rejects_naive_collection_time() -> None:
    with pytest.raises(AuditSnapshotError):
        replace(_empty_snapshot(), collected_at=datetime.now())


def test_audit_ledger_snapshot_rejects_collection_before_creation() -> None:
    snapshot = _empty_snapshot()
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, collected_at=snapshot.status.created_at - timedelta(seconds=1))


def test_audit_ledger_snapshot_rejects_collection_before_last_append() -> None:
    snapshot = _populated_snapshot()
    with pytest.raises(AuditSnapshotError):
        replace(
            snapshot,
            collected_at=cast(datetime, snapshot.status.last_appended_at) - timedelta(seconds=1),
        )


def test_audit_ledger_snapshot_rejects_boolean_matching_count() -> None:
    with pytest.raises(AuditSnapshotError):
        replace(_empty_snapshot(filtered=True), matching_entry_count=True)


def test_audit_ledger_snapshot_rejects_matching_count_over_status() -> None:
    with pytest.raises(AuditSnapshotError):
        replace(_empty_snapshot(filtered=True), matching_entry_count=1)


def test_unfiltered_audit_snapshot_requires_all_entries() -> None:
    snapshot = _populated_snapshot()
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, entries=())


def test_filtered_audit_snapshot_allows_paginated_entries() -> None:
    snapshot = _populated_snapshot(filtered=True)
    assert replace(snapshot, entries=()).matching_entry_count == 1


def test_audit_ledger_snapshot_rejects_wrong_verification_ledger() -> None:
    snapshot = _empty_snapshot()
    other = AuditLedgerVerificationResult(True, 0, AuditLedgerId(), None, None, None)
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, verification=other)


def test_audit_ledger_snapshot_valid_verification_matches_status() -> None:
    snapshot = _populated_snapshot()
    wrong = AuditLedgerVerificationResult(True, 0, snapshot.status.ledger_id, None, None, None)
    with pytest.raises(AuditSnapshotError):
        replace(snapshot, verification=wrong)


def test_audit_ledger_snapshot_preserves_invalid_verification() -> None:
    snapshot = _populated_snapshot()
    invalid = AuditLedgerVerificationResult(
        False,
        1,
        snapshot.status.ledger_id,
        1,
        1,
        snapshot.status.head_digest,
        AuditLedgerVerificationFailure.ENTRY_DIGEST_MISMATCH,
        1,
    )
    assert replace(snapshot, verification=invalid).verification is invalid


def test_audit_ledger_snapshot_require_valid() -> None:
    snapshot = _empty_snapshot()
    snapshot.require_valid()
    assert snapshot.verification.valid


def test_audit_ledger_snapshot_require_valid_raises() -> None:
    snapshot = _populated_snapshot()
    invalid = AuditLedgerVerificationResult(
        False,
        1,
        snapshot.status.ledger_id,
        1,
        1,
        snapshot.status.head_digest,
        AuditLedgerVerificationFailure.RECORD_DIGEST_MISMATCH,
        1,
    )
    with pytest.raises(AuditLedgerVerificationError):
        replace(snapshot, verification=invalid).require_valid()


def test_audit_ledger_snapshot_serialization() -> None:
    assert set(_empty_snapshot().to_dict(_safe_policy())) == {
        "status",
        "entries",
        "collected_at",
        "filtered",
        "matching_entry_count",
        "verification",
    }


def test_audit_ledger_snapshot_round_trip_without_redaction() -> None:
    snapshot = _populated_snapshot()
    assert AuditLedgerSnapshot.from_dict(snapshot.to_dict(_safe_policy())) == snapshot


def test_audit_ledger_snapshot_serialization_is_independent() -> None:
    snapshot = _populated_snapshot()
    rendered = snapshot.to_dict(_safe_policy())
    cast(dict[str, object], rendered["status"])["entry_count"] = 0
    assert snapshot.status.entry_count == 1


def test_audit_ledger_snapshot_rendering_redacts_entries() -> None:
    snapshot = _populated_snapshot()
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    entry = _ledger_entry(
        ledger_id=snapshot.status.ledger_id,
        record=replace(_record(), attributes={"password": secret}),
    )
    assert secret not in str(replace(snapshot, entries=(entry,)).to_dict())


def test_redacted_audit_ledger_snapshot_is_display_only() -> None:
    snapshot = _populated_snapshot()
    entry = snapshot.entries[0]
    object.__setattr__(entry.record, "attributes", MappingProxyType({"password": "secret"}))
    with pytest.raises(AuditSerializationError):
        AuditLedgerSnapshot.from_dict(snapshot.to_dict())


def test_audit_ledger_snapshot_from_dict_uses_strict_types() -> None:
    value = _empty_snapshot().to_dict(_safe_policy())
    value["filtered"] = 0
    with pytest.raises(AuditSerializationError):
        AuditLedgerSnapshot.from_dict(value)


@pytest.mark.asyncio
async def test_audit_ledger_list_all() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.list() == await ledger.entries()


@pytest.mark.asyncio
async def test_audit_ledger_list_with_filter() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert len(await ledger.list(AuditFilter(categories=frozenset({AuditCategory.SECURITY})))) == 1


@pytest.mark.asyncio
async def test_audit_ledger_list_sequence_order() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert tuple(entry.sequence for entry in await ledger.list()) == (1, 2)


@pytest.mark.asyncio
async def test_audit_ledger_list_applies_offset() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    second = await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert await ledger.list(AuditFilter(offset=1)) == (second,)


@pytest.mark.asyncio
async def test_audit_ledger_list_applies_limit() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    first = await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert await ledger.list(AuditFilter(limit=1)) == (first,)


@pytest.mark.asyncio
async def test_audit_ledger_list_applies_offset_before_limit() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    second = await ledger.append(replace(_record(), record_id=AuditRecordId()))
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert await ledger.list(AuditFilter(offset=1, limit=1)) == (second,)


@pytest.mark.asyncio
async def test_audit_ledger_list_limit_zero() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.list(AuditFilter(limit=0)) == ()


@pytest.mark.asyncio
async def test_audit_ledger_list_offset_beyond_end() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.list(AuditFilter(offset=2)) == ()


@pytest.mark.asyncio
async def test_audit_ledger_list_rejects_wrong_filter() -> None:
    with pytest.raises(AuditFilterError):
        await InMemoryAuditLedger(clock=lambda: datetime.now(UTC)).list(cast(Any, {}))


@pytest.mark.asyncio
async def test_audit_ledger_list_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    assert await ledger.list() == ()


@pytest.mark.asyncio
async def test_audit_ledger_count_all() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.count() == 1


@pytest.mark.asyncio
async def test_audit_ledger_count_with_filter() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.count(AuditFilter(outcomes=frozenset({AuditOutcome.FAILURE}))) == 0


@pytest.mark.asyncio
async def test_audit_ledger_count_ignores_limit() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.count(AuditFilter(limit=0)) == 1


@pytest.mark.asyncio
async def test_audit_ledger_count_ignores_offset() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.count(AuditFilter(offset=99)) == 1


@pytest.mark.asyncio
async def test_audit_ledger_count_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    assert await ledger.count() == 0


@pytest.mark.asyncio
async def test_audit_ledger_query_result_is_immutable() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert isinstance(await ledger.list(), tuple)


@pytest.mark.asyncio
async def test_audit_ledger_query_snapshot_is_point_in_time() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    result = await ledger.list()
    await ledger.append(_record())
    assert result == ()


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_api_unfiltered() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = InMemoryAuditLedger(clock=lambda: instant)
    snapshot = await ledger.snapshot()
    assert not snapshot.filtered and snapshot.status.entry_count == 0


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_api_filtered() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = await InMemoryAuditLedger(clock=lambda: instant).snapshot(AuditFilter())
    assert snapshot.filtered


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_matching_count_before_pagination() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    snapshot = await ledger.snapshot(AuditFilter(limit=0))
    assert snapshot.matching_entry_count == 1 and snapshot.entries == ()


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_uses_injected_clock() -> None:
    created, collected = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    clock = _LedgerClock(created, collected)
    snapshot = await InMemoryAuditLedger(clock=clock).snapshot()
    assert snapshot.collected_at == collected


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_rejects_naive_clock() -> None:
    clock = _LedgerClock(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1))
    ledger = InMemoryAuditLedger(clock=clock)
    with pytest.raises(AuditSnapshotError):
        await ledger.snapshot()


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_rejects_clock_before_creation() -> None:
    later, earlier = datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)
    ledger = InMemoryAuditLedger(clock=_LedgerClock(later, earlier))
    with pytest.raises(AuditSnapshotError):
        await ledger.snapshot()


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_rejects_clock_before_last_append() -> None:
    created, appended, collected = (
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 3, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    ledger = InMemoryAuditLedger(clock=_LedgerClock(created, appended, collected))
    await ledger.append(_record())
    with pytest.raises(AuditSnapshotError):
        await ledger.snapshot()


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_after_close() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = InMemoryAuditLedger(clock=lambda: instant)
    await ledger.close()
    assert (await ledger.snapshot()).status.closed


@pytest.mark.asyncio
async def test_old_audit_ledger_snapshot_unchanged_after_append() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    old = await ledger.snapshot()
    await ledger.append(_record())
    assert old.status.entry_count == 0 and old.entries == ()


@pytest.mark.asyncio
async def test_audit_ledger_snapshot_verifies_complete_chain() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    snapshot = await ledger.snapshot(AuditFilter(limit=0))
    assert snapshot.verification.checked_entry_count == 1


@pytest.mark.asyncio
async def test_filtered_snapshot_detects_corruption_outside_selection() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    object.__setattr__(entry, "entry_digest", "1" * 64)
    snapshot = await ledger.snapshot(AuditFilter(categories=frozenset({AuditCategory.SYSTEM})))
    assert snapshot.entries == () and not snapshot.verification.valid


@pytest.mark.asyncio
async def test_empty_audit_ledger_verify_detects_stored_head() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    ledger._head_digest = "1" * 64
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.HEAD_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_empty_audit_ledger_verify_detects_stored_count() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    ledger._entry_count = 1
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.ENTRY_COUNT_MISMATCH


@pytest.mark.asyncio
async def test_empty_audit_ledger_head_failure_precedes_count_failure() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    ledger._head_digest, ledger._entry_count = "1" * 64, 1
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.HEAD_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verification_uses_constant_time_digest_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    original = cast(Callable[[str, str], bool], hmac.compare_digest)

    def compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(hmac, "compare_digest", compare)
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.verify()
    assert len(calls) >= 4


def test_audit_filter_invalid_identifier_error_omits_secret() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    value = AuditFilter().to_dict(_safe_policy())
    value["record_ids"] = [secret]
    with pytest.raises(AuditSerializationError) as captured:
        AuditFilter.from_dict(value)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_filter_invalid_text_error_omits_secret() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    with pytest.raises(AuditFilterError) as captured:
        AuditFilter(action_names=cast(Any, [secret + " invalid"]))
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


@pytest.mark.asyncio
async def test_audit_filter_wrong_runtime_error_omits_secret() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"

    class InvalidFilter:
        def __repr__(self) -> str:
            return secret

    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    with pytest.raises(AuditFilterError) as captured:
        await ledger.list(cast(Any, InvalidFilter()))
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_filter_malformed_serialization_error_omits_secret() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    value = AuditFilter().to_dict(_safe_policy())
    value["has_target"] = secret
    with pytest.raises(AuditSerializationError) as captured:
        AuditFilter.from_dict(value)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_snapshot_consistency_error_omits_secret() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    snapshot = _populated_snapshot(filtered=True)
    secret_entry = _ledger_entry(
        ledger_id=snapshot.status.ledger_id,
        record=replace(_record(), summary=secret),
    )
    with pytest.raises(AuditSnapshotError) as captured:
        replace(snapshot, entries=(secret_entry,), matching_entry_count=0)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())
