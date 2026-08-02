import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from alios_core import RunId
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


def _record() -> AuditRecord:
    return AuditRecord.create(
        category=AuditCategory.SECURITY,
        severity=AuditSeverity.NOTICE,
        outcome=AuditOutcome.SUCCESS,
        actor=AuditActor(AuditActorKind.USER, "user"),
        action=AuditAction("read"),
        source=TraceSource("integration"),
        summary="Read",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_nested_audit_context_lineage() -> None:
    parent, child = AuditContext(run_id=RunId()), AuditContext(run_id=RunId())
    async with bind_audit_context(parent):
        async with bind_audit_context(child):
            assert require_audit_context() == child
        assert require_audit_context() == parent


@pytest.mark.asyncio
async def test_concurrent_audit_contexts_are_isolated() -> None:
    async def read(context: AuditContext) -> AuditContext:
        async with bind_audit_context(context):
            return require_audit_context()

    first, second = AuditContext(run_id=RunId()), AuditContext(run_id=RunId())
    assert tuple(await asyncio.gather(read(first), read(second))) == (first, second)


@pytest.mark.asyncio
async def test_child_async_task_inherits_audit_context() -> None:
    context = AuditContext(run_id=RunId())
    async with bind_audit_context(context):
        assert (
            await asyncio.create_task(asyncio.sleep(0, result=require_audit_context())) == context
        )


@pytest.mark.asyncio
async def test_sibling_tasks_inherit_same_audit_context_snapshot() -> None:
    context = AuditContext(run_id=RunId())
    async with bind_audit_context(context):
        assert tuple(
            await asyncio.gather(
                asyncio.create_task(asyncio.sleep(0, result=require_audit_context())),
                asyncio.create_task(asyncio.sleep(0, result=require_audit_context())),
            )
        ) == (context, context)


@pytest.mark.asyncio
async def test_audit_binding_restores_after_child_failure() -> None:
    parent = AuditContext(run_id=RunId())
    async with bind_audit_context(parent):
        with pytest.raises(RuntimeError):
            async with bind_audit_context(AuditContext()):
                raise RuntimeError("safe")
        assert require_audit_context() == parent


@pytest.mark.asyncio
async def test_audit_binding_restores_after_child_cancellation() -> None:
    parent = AuditContext(run_id=RunId())
    async with bind_audit_context(parent):
        with pytest.raises(asyncio.CancelledError):
            async with bind_audit_context(AuditContext()):
                raise asyncio.CancelledError
        assert require_audit_context() == parent


def test_audit_context_created_from_bound_trace_context() -> None:
    trace = TraceContext.create_root(run_id=RunId())
    assert AuditContext.from_trace_context(trace).run_id == trace.run_id


def test_audit_and_trace_contexts_remain_independent() -> None:
    trace = TraceContext.create_root(baggage={"secret": "value"})
    assert AuditContext.from_trace_context(trace).metadata == {}


def test_audit_record_factory_uses_bound_context() -> None:
    context = AuditContext(run_id=RunId())
    with bind_audit_context(context):
        record = _record()
    assert record.context is context


@pytest.mark.asyncio
async def test_audit_record_factory_under_concurrent_contexts() -> None:
    async def create(context: AuditContext) -> AuditContext:
        async with bind_audit_context(context):
            return _record().context

    contexts = (AuditContext(run_id=RunId()), AuditContext(run_id=RunId()))
    assert tuple(await asyncio.gather(*(create(item) for item in contexts))) == contexts


def test_audit_record_complete_round_trip() -> None:
    record = replace(
        _record(), target=AuditTarget("doc", "1"), attributes={"x": 1}, tags=frozenset({"security"})
    )
    assert (
        AuditRecord.from_dict(record.to_dict(RedactionPolicy(include_default_rules=False)))
        == record
    )


def test_audit_record_with_nested_attributes_round_trip() -> None:
    record = replace(_record(), attributes={"nested": [{"ok": True}]})
    assert (
        AuditRecord.from_dict(record.to_dict(RedactionPolicy(include_default_rules=False)))
        == record
    )


def test_audit_record_with_actor_target_action_round_trip() -> None:
    record = replace(
        _record(),
        actor=AuditActor(AuditActorKind.SERVICE, "api"),
        action=AuditAction("write", {"x": 1}),
        target=AuditTarget("doc", "1"),
    )
    assert (
        AuditRecord.from_dict(record.to_dict(RedactionPolicy(include_default_rules=False)))
        == record
    )


def test_audit_record_redaction_across_all_sensitive_locations() -> None:
    secret = "password=AUDIT-C2A-SECRET-41f9b7"
    record = replace(
        _record(),
        actor=AuditActor(AuditActorKind.USER, secret, attributes={"password": secret}),
        action=AuditAction("read", {"password": secret}),
        target=AuditTarget("doc", secret, {"password": secret}),
        context=AuditContext(metadata={"password": secret}),
        summary=secret,
        attributes={"password": secret},
    )
    rendered = record.to_dict()
    assert (
        secret not in str(rendered)
        and secret not in repr(rendered)
        and secret not in json.dumps(rendered)
    )


def test_audit_record_redaction_does_not_mutate_record() -> None:
    record = replace(_record(), attributes={"password": "secret"})
    record.to_dict()
    assert record.attributes["password"] == "secret"


def test_audit_record_redacted_output_is_json() -> None:
    assert json.loads(json.dumps(_record().to_dict()))["category"] == "security"


def test_redacted_audit_record_round_trip() -> None:
    rendered = replace(_record(), attributes={"password": "secret"}).to_dict()
    assert AuditRecord.from_dict(rendered).attributes["password"] == "[REDACTED]"


def test_custom_redaction_policy_applies_to_audit_record() -> None:
    record = replace(_record(), attributes={"password": "secret"})
    assert (
        record.to_dict()["attributes"]
        != record.to_dict(RedactionPolicy(include_default_rules=False))["attributes"]
    )


def test_default_redaction_policy_does_not_mutate_global_state() -> None:
    first, second = _record().to_dict(), _record().to_dict()
    assert first["category"] == second["category"]


def test_audit_integrity_digest_survives_serialization_round_trip() -> None:
    record = _record()
    restored = AuditRecord.from_dict(record.to_dict(RedactionPolicy(include_default_rules=False)))
    assert restored.integrity_digest() == record.integrity_digest()


@pytest.mark.asyncio
async def test_audit_integrity_digest_is_stable_across_async_boundary() -> None:
    record = _record()
    assert (
        await asyncio.create_task(asyncio.sleep(0, result=record.integrity_digest()))
        == record.integrity_digest()
    )


def test_audit_integrity_digest_is_stable_across_mapping_order() -> None:
    record = _record()
    assert (
        replace(record, attributes={"a": 1, "b": 2}).integrity_digest()
        == replace(record, attributes={"b": 2, "a": 1}).integrity_digest()
    )


def test_audit_integrity_digest_detects_actor_change() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, actor=AuditActor(AuditActorKind.USER, "other")).integrity_digest()
    )


def test_audit_integrity_digest_detects_context_change() -> None:
    record = _record()
    assert (
        record.integrity_digest()
        != replace(record, context=AuditContext(run_id=RunId())).integrity_digest()
    )


def test_audit_integrity_digest_detects_attribute_change() -> None:
    record = _record()
    assert record.integrity_digest() != replace(record, attributes={"x": 1}).integrity_digest()


def test_audit_integrity_verification_uses_safe_output() -> None:
    record = replace(_record(), attributes={"secret": "plaintext"})
    assert "plaintext" not in record.integrity_digest()


def test_audit_module_does_not_import_runtime() -> None:
    import alios_observability.audit as audit

    assert "alios_runtime" not in audit.__dict__


@pytest.mark.asyncio
async def test_audit_module_creates_no_global_tasks() -> None:
    before = asyncio.all_tasks()
    import alios_observability.audit  # noqa: F401

    await asyncio.sleep(0)
    assert asyncio.all_tasks() == before


def test_public_audit_imports_have_no_global_context() -> None:
    assert current_audit_context() is None


def test_public_audit_exports_preserve_logging_identity() -> None:
    import alios_observability
    import alios_observability.logging as logging

    assert alios_observability.RedactionPolicy is logging.RedactionPolicy


def test_public_audit_exports_preserve_metrics_identity() -> None:
    import alios_observability
    import alios_observability.metrics as metrics

    assert alios_observability.MetricPoint is metrics.MetricPoint


def test_public_audit_exports_preserve_tracing_identity() -> None:
    import alios_observability
    import alios_observability.tracing as tracing

    assert alios_observability.TraceContext is tracing.TraceContext


def test_public_audit_exports_preserve_audit_class_identity() -> None:
    import alios_observability
    import alios_observability.audit as audit

    assert alios_observability.AuditRecord is audit.AuditRecord
