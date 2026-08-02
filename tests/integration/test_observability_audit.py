import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from alios_core import AuditFilterError, AuditRecordId, RunId
from alios_core.errors import (
    AuditContextError,
    AuditLedgerCapacityError,
    AuditLedgerClosedError,
    AuditLedgerError,
    AuditLedgerVerificationError,
    AuditSerializationError,
    AuditValidationError,
    ResourceConflictError,
)
from alios_observability import (
    AuditAction,
    AuditActor,
    AuditActorKind,
    AuditCategory,
    AuditContext,
    AuditFilter,
    AuditLedgerEntry,
    AuditLedgerSnapshot,
    AuditLedgerVerificationFailure,
    AuditOutcome,
    AuditRecord,
    AuditSeverity,
    AuditTarget,
    InMemoryAuditLedger,
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


def test_trace_to_audit_context_rejects_falsey_non_mapping_metadata() -> None:
    trace = TraceContext.create_root()
    invalid_values: tuple[object, ...] = ([], (), "", 0, False)
    for invalid in invalid_values:
        with pytest.raises(AuditContextError):
            AuditContext.from_trace_context(trace, metadata=invalid)  # type: ignore[arg-type]


def test_trace_to_audit_context_failure_does_not_change_bound_context() -> None:
    bound = AuditContext(run_id=RunId())
    with bind_audit_context(bound):
        with pytest.raises(AuditContextError):
            AuditContext.from_trace_context(TraceContext.create_root(), metadata=False)  # type: ignore[arg-type]
        assert current_audit_context() is bound


def test_audit_record_factory_rejects_falsey_invalid_attributes() -> None:
    invalid_values: tuple[object, ...] = ([], (), "", 0, False)
    for invalid in invalid_values:
        with pytest.raises(AuditValidationError):
            _record_with(attributes=invalid)


def test_audit_record_factory_invalid_id_does_not_change_bound_context() -> None:
    bound = AuditContext(run_id=RunId())
    with bind_audit_context(bound):
        with pytest.raises(AuditValidationError):
            _record_with(record_id=False)
        assert current_audit_context() is bound


def test_audit_record_duplicate_serialized_tags_rejected_after_round_trip_mutation() -> None:
    serialized = _record().to_dict(RedactionPolicy(include_default_rules=False))
    serialized["tags"] = ["security", "security"]
    with pytest.raises(AuditSerializationError) as captured:
        AuditRecord.from_dict(serialized)
    assert isinstance(captured.value.__cause__, AuditValidationError)


def test_audit_record_unique_serialized_tags_round_trip() -> None:
    record = replace(_record(), tags=frozenset({"security", "access"}))
    serialized = record.to_dict(RedactionPolicy(include_default_rules=False))
    assert serialized["tags"] == ["access", "security"]
    assert AuditRecord.from_dict(serialized) == record


def test_audit_record_strict_factory_failures_create_no_global_state() -> None:
    assert current_audit_context() is None
    with pytest.raises(AuditValidationError):
        _record_with(attributes=[])
    assert current_audit_context() is None


def test_audit_record_strict_factory_failures_do_not_affect_integrity_digest() -> None:
    record = _record()
    digest = record.integrity_digest()
    with pytest.raises(AuditValidationError):
        _record_with(record_id=0)
    assert record.integrity_digest() == digest


def _record_with(**overrides: object) -> AuditRecord:
    values: dict[str, object] = {
        "category": AuditCategory.SECURITY,
        "severity": AuditSeverity.NOTICE,
        "outcome": AuditOutcome.SUCCESS,
        "actor": AuditActor(AuditActorKind.USER, "user"),
        "action": AuditAction("read"),
        "source": TraceSource("integration"),
        "summary": "Read",
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return AuditRecord.create(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_audit_ledger_append_record_end_to_end() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    record = _record()
    entry = await ledger.append(record)
    assert entry.record is record and await ledger.get(1) is entry


@pytest.mark.asyncio
async def test_audit_ledger_multiple_records_form_valid_chain() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert (await ledger.verify()).valid


@pytest.mark.asyncio
async def test_audit_ledger_sequence_follows_append_order() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    later = replace(_record(), occurred_at=datetime(2026, 2, 1, tzinfo=UTC))
    earlier = replace(
        _record(), record_id=AuditRecordId(), occurred_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert (await ledger.append(later)).sequence < (await ledger.append(earlier)).sequence


@pytest.mark.asyncio
async def test_audit_ledger_occurrence_time_does_not_control_sequence() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    first = replace(_record(), occurred_at=datetime(2027, 1, 1, tzinfo=UTC))
    second = replace(
        _record(), record_id=AuditRecordId(), occurred_at=datetime(2024, 1, 1, tzinfo=UTC)
    )
    await ledger.append(first)
    await ledger.append(second)
    assert tuple(item.record for item in await ledger.entries()) == (first, second)


@pytest.mark.asyncio
async def test_audit_ledger_entry_round_trip_preserves_chain() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    first = await ledger.append(_record())
    second = await ledger.append(replace(_record(), record_id=AuditRecordId()))
    restored = AuditLedgerEntry.from_dict(
        second.to_dict(RedactionPolicy(include_default_rules=False))
    )
    assert restored.previous_digest == first.entry_digest


@pytest.mark.asyncio
async def test_audit_ledger_default_rendering_redacts_sensitive_record() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(replace(_record(), attributes={"password": secret}))
    assert secret not in str(entry.to_dict())


@pytest.mark.asyncio
async def test_audit_ledger_exact_rendering_round_trip() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    assert (
        AuditLedgerEntry.from_dict(entry.to_dict(RedactionPolicy(include_default_rules=False)))
        == entry
    )


@pytest.mark.asyncio
async def test_audit_ledger_integrity_survives_record_serialization_round_trip() -> None:
    record = _record()
    restored = AuditRecord.from_dict(record.to_dict(RedactionPolicy(include_default_rules=False)))
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    assert (await ledger.append(restored)).record_digest == record.integrity_digest()


@pytest.mark.asyncio
async def test_audit_ledger_concurrent_appends_are_contiguous() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    gate = asyncio.Event()

    async def append(record: AuditRecord) -> AuditLedgerEntry:
        await gate.wait()
        return await ledger.append(record)

    tasks = [
        asyncio.create_task(append(replace(_record(), record_id=AuditRecordId()))) for _ in "abcd"
    ]
    gate.set()
    entries = await asyncio.gather(*tasks)
    assert sorted(item.sequence for item in entries) == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_audit_ledger_concurrent_appends_form_valid_chain() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    gate = asyncio.Event()

    async def append(record: AuditRecord) -> None:
        await gate.wait()
        await ledger.append(record)

    tasks = [
        asyncio.create_task(append(replace(_record(), record_id=AuditRecordId()))) for _ in "abc"
    ]
    gate.set()
    await asyncio.gather(*tasks)
    assert (await ledger.verify()).valid


@pytest.mark.asyncio
async def test_audit_ledger_concurrent_duplicate_record_id_has_one_winner() -> None:
    ledger, record, gate = (
        InMemoryAuditLedger(clock=lambda: datetime.now(UTC)),
        _record(),
        asyncio.Event(),
    )

    async def append() -> AuditLedgerEntry:
        await gate.wait()
        return await ledger.append(record)

    tasks = [asyncio.create_task(append()) for _ in "abc"]
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(item, AuditLedgerEntry) for item in results) == 1
    assert sum(isinstance(item, ResourceConflictError) for item in results) == 2


@pytest.mark.asyncio
async def test_audit_ledger_concurrent_final_capacity_slot_has_one_winner() -> None:
    ledger, gate = (
        InMemoryAuditLedger(maximum_entries=1, clock=lambda: datetime.now(UTC)),
        asyncio.Event(),
    )

    async def append(record: AuditRecord) -> AuditLedgerEntry:
        await gate.wait()
        return await ledger.append(record)

    tasks = [
        asyncio.create_task(append(replace(_record(), record_id=AuditRecordId()))) for _ in "abc"
    ]
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(item, AuditLedgerEntry) for item in results) == 1
    assert sum(isinstance(item, AuditLedgerCapacityError) for item in results) == 2


@pytest.mark.asyncio
async def test_audit_ledger_concurrent_reads_during_append_are_consistent() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    gate = asyncio.Event()

    async def append() -> None:
        await gate.wait()
        await ledger.append(_record())

    task = asyncio.create_task(append())
    before = await ledger.entries()
    gate.set()
    await task
    after = await ledger.entries()
    assert before == () and len(after) == 1


@pytest.mark.asyncio
async def test_audit_ledger_lookup_by_sequence_and_record_id() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    entry = await ledger.append(record)
    assert await ledger.get(1) is await ledger.get_by_record_id(record.record_id) is entry


@pytest.mark.asyncio
async def test_audit_ledger_entries_preserve_immutable_records() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    await ledger.append(record)
    assert (await ledger.entries())[0].record is record


@pytest.mark.asyncio
async def test_audit_ledger_status_tracks_chain_head() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    assert (await ledger.status()).head_digest == entry.entry_digest


@pytest.mark.asyncio
async def test_audit_ledger_failed_append_leaves_chain_valid() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    await ledger.append(record)
    with pytest.raises(ResourceConflictError):
        await ledger.append(record)
    assert (await ledger.verify()).valid


@pytest.mark.asyncio
async def test_audit_ledger_capacity_failure_leaves_chain_valid() -> None:
    ledger = InMemoryAuditLedger(maximum_entries=1, clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    with pytest.raises(AuditLedgerCapacityError):
        await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert (await ledger.verify()).valid


@pytest.mark.asyncio
async def test_audit_ledger_duplicate_failure_leaves_chain_valid() -> None:
    ledger, record = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), _record()
    await ledger.append(record)
    with pytest.raises(ResourceConflictError):
        await ledger.append(record)
    assert (await ledger.status()).entry_count == 1


@pytest.mark.asyncio
async def test_audit_ledger_backward_clock_failure_leaves_chain_valid() -> None:
    created = datetime(2026, 1, 2, tzinfo=UTC)
    values = iter((created, created - timedelta(seconds=1)))
    ledger = InMemoryAuditLedger(clock=lambda: next(values))
    with pytest.raises(AuditLedgerError):
        await ledger.append(_record())
    assert (await ledger.verify()).valid


@pytest.mark.asyncio
async def test_audit_ledger_verification_detects_reordered_entries() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    ledger._entries.reverse()
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.SEQUENCE_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verification_detects_removed_middle_entry() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    for _ in "abc":
        await ledger.append(replace(_record(), record_id=AuditRecordId()))
    del ledger._entries[1]
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.SEQUENCE_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verification_detects_changed_record() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    object.__setattr__(entry, "record", replace(entry.record, summary="changed"))
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.RECORD_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verification_detects_changed_head() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    ledger._head_digest = "1" * 64
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.HEAD_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_audit_ledger_verification_result_contains_no_record_data() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(replace(_record(), summary=secret))
    object.__setattr__(entry, "entry_digest", "1" * 64)
    assert secret not in str((await ledger.verify()).to_dict())


@pytest.mark.asyncio
async def test_audit_ledger_reads_remain_available_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(_record())
    await ledger.close()
    assert await ledger.get(1) is entry


@pytest.mark.asyncio
async def test_audit_ledger_verification_remains_available_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    assert (await ledger.verify()).valid


@pytest.mark.asyncio
async def test_audit_ledger_close_blocks_new_append() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    with pytest.raises(AuditLedgerClosedError):
        await ledger.append(_record())


@pytest.mark.asyncio
async def test_audit_ledger_close_racing_with_append_is_consistent() -> None:
    ledger, gate = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), asyncio.Event()

    async def append() -> AuditLedgerEntry:
        await gate.wait()
        return await ledger.append(_record())

    task = asyncio.create_task(append())
    await ledger.close()
    gate.set()
    result = await asyncio.gather(task, return_exceptions=True)
    assert (
        isinstance(result[0], AuditLedgerClosedError) and (await ledger.status()).entry_count == 0
    )


@pytest.mark.asyncio
async def test_audit_ledger_errors_do_not_expose_secret_values() -> None:
    secret = "AUDIT-C2B1-LEDGER-SECRET-7ad291"
    record = replace(
        _record(),
        actor=AuditActor(AuditActorKind.USER, secret),
        target=AuditTarget("doc", secret),
        summary=secret,
        context=AuditContext(metadata={"password": secret}),
        attributes={"password": secret},
    )
    ledger = InMemoryAuditLedger(maximum_entries=1, clock=lambda: datetime.now(UTC))
    await ledger.append(record)
    with pytest.raises(ResourceConflictError) as captured:
        await ledger.append(record)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_public_audit_ledger_exports_preserve_class_identity() -> None:
    import alios_observability
    import alios_observability.audit as audit

    assert alios_observability.InMemoryAuditLedger is audit.InMemoryAuditLedger


@pytest.mark.asyncio
async def test_audit_ledger_import_creates_no_global_tasks() -> None:
    before = asyncio.all_tasks()
    import alios_observability.audit as audit

    await asyncio.sleep(0)
    assert asyncio.all_tasks() == before
    assert not any(isinstance(value, InMemoryAuditLedger) for value in audit.__dict__.values())


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_category() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert len(await ledger.list(AuditFilter(categories=frozenset({AuditCategory.SECURITY})))) == 1


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_actor() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert len(await ledger.list(AuditFilter(actor_identifiers=frozenset({"user"})))) == 1


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_action() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.count(AuditFilter(action_names=frozenset({"read"}))) == 1


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_target() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(replace(_record(), target=AuditTarget("document", "doc-1")))
    assert len(await ledger.list(AuditFilter(target_kinds=frozenset({"document"})))) == 1


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_context() -> None:
    run_id = RunId()
    trace = TraceContext.create_root(run_id=run_id)
    context = AuditContext.from_trace_context(trace)
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(replace(_record(), context=context))
    assert await ledger.count(AuditFilter(run_ids=frozenset({run_id}))) == 1


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_tags() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(replace(_record(), tags=frozenset({"security", "access"})))
    assert await ledger.count(AuditFilter(tags_all=frozenset({"security"}))) == 1


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_attributes() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(replace(_record(), attributes={"region": "eu", "active": True}))
    assert await ledger.count(AuditFilter(record_attribute_equals={"region": "eu"})) == 1


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_times() -> None:
    instant = datetime(2026, 1, 2, tzinfo=UTC)
    ledger = InMemoryAuditLedger(clock=lambda: instant)
    await ledger.append(_record())
    filter = AuditFilter(
        appended_after=instant - timedelta(seconds=1),
        appended_before=instant + timedelta(seconds=1),
    )
    assert await ledger.count(filter) == 1


@pytest.mark.asyncio
async def test_audit_query_filters_end_to_end_by_sequence_range() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    result = await ledger.list(AuditFilter(minimum_sequence=2, maximum_sequence=2))
    assert tuple(entry.sequence for entry in result) == (2,)


@pytest.mark.asyncio
async def test_audit_query_combined_predicates() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(replace(_record(), tags=frozenset({"security"}), attributes={"x": 1}))
    filter = AuditFilter(
        categories=frozenset({AuditCategory.SECURITY}),
        actor_identifiers=frozenset({"user"}),
        tags_any=frozenset({"security"}),
        record_attribute_equals={"x": 1},
    )
    assert await ledger.count(filter) == 1


@pytest.mark.asyncio
async def test_audit_query_pagination_is_deterministic() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    for _ in "abc":
        await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert tuple(entry.sequence for entry in await ledger.list(AuditFilter(offset=1, limit=1))) == (
        2,
    )


@pytest.mark.asyncio
async def test_audit_query_count_ignores_pagination() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    assert await ledger.count(AuditFilter(offset=10, limit=0)) == 1


@pytest.mark.asyncio
async def test_audit_query_preserves_append_order_not_occurrence_order() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    later = replace(_record(), occurred_at=datetime(2027, 1, 1, tzinfo=UTC))
    earlier = replace(
        _record(), record_id=AuditRecordId(), occurred_at=datetime(2025, 1, 1, tzinfo=UTC)
    )
    await ledger.append(later)
    await ledger.append(earlier)
    assert tuple(entry.record for entry in await ledger.list()) == (later, earlier)


@pytest.mark.asyncio
async def test_audit_query_after_close() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.close()
    assert len(await ledger.list()) == await ledger.count() == 1


@pytest.mark.asyncio
async def test_audit_query_during_concurrent_appends_is_consistent() -> None:
    ledger, gate = InMemoryAuditLedger(clock=lambda: datetime.now(UTC)), asyncio.Event()

    async def append() -> None:
        await gate.wait()
        await ledger.append(_record())

    task = asyncio.create_task(append())
    before = await ledger.list()
    gate.set()
    await task
    after = await ledger.list()
    assert before == () and len(after) == 1


@pytest.mark.asyncio
async def test_two_audit_ledgers_query_independently() -> None:
    first = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    second = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await first.append(_record())
    assert await first.count() == 1 and await second.count() == 0


@pytest.mark.asyncio
async def test_audit_snapshot_complete_chain() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    snapshot = await ledger.snapshot()
    assert snapshot.verification.valid and len(snapshot.entries) == 1


@pytest.mark.asyncio
async def test_audit_snapshot_filtered_entries() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    snapshot = await ledger.snapshot(AuditFilter(categories=frozenset({AuditCategory.SYSTEM})))
    assert snapshot.filtered and snapshot.entries == ()


@pytest.mark.asyncio
async def test_audit_snapshot_paginated_entries() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    assert tuple(
        entry.sequence for entry in (await ledger.snapshot(AuditFilter(offset=1))).entries
    ) == (2,)


@pytest.mark.asyncio
async def test_audit_snapshot_matching_count_before_pagination() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    snapshot = await ledger.snapshot(AuditFilter(limit=0))
    assert snapshot.matching_entry_count == 1 and snapshot.entries == ()


@pytest.mark.asyncio
async def test_audit_snapshot_round_trip_without_redaction() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    snapshot = await ledger.snapshot()
    assert (
        AuditLedgerSnapshot.from_dict(
            snapshot.to_dict(RedactionPolicy(include_default_rules=False))
        )
        == snapshot
    )


@pytest.mark.asyncio
async def test_audit_snapshot_default_rendering_redacts_sensitive_records() -> None:
    secret = "password=AUDIT-C2B2-QUERY-SECRET-913acf"
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(replace(_record(), attributes={"password": secret}))
    assert secret not in str((await ledger.snapshot()).to_dict())


@pytest.mark.asyncio
async def test_audit_snapshot_old_instance_unchanged_after_append() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    old = await ledger.snapshot()
    await ledger.append(_record())
    assert old.status.entry_count == 0


@pytest.mark.asyncio
async def test_audit_snapshot_after_close_preserves_closed_status() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.close()
    assert (await ledger.snapshot()).status.closed


@pytest.mark.asyncio
async def test_audit_snapshot_uses_one_point_in_time_state() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    snapshot = await ledger.snapshot()
    assert (
        snapshot.status.entry_count
        == len(snapshot.entries)
        == snapshot.verification.checked_entry_count
    )


@pytest.mark.asyncio
async def test_filtered_audit_snapshot_verifies_full_chain() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    snapshot = await ledger.snapshot(AuditFilter(limit=0))
    assert snapshot.entries == () and snapshot.verification.checked_entry_count == 1


@pytest.mark.asyncio
async def test_filtered_audit_snapshot_detects_corruption_before_selection() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    first = await ledger.append(_record())
    await ledger.append(
        replace(_record(), record_id=AuditRecordId(), category=AuditCategory.SYSTEM)
    )
    object.__setattr__(first, "entry_digest", "1" * 64)
    snapshot = await ledger.snapshot(AuditFilter(categories=frozenset({AuditCategory.SYSTEM})))
    assert not snapshot.verification.valid and len(snapshot.entries) == 1


@pytest.mark.asyncio
async def test_filtered_audit_snapshot_detects_corruption_after_selection() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    await ledger.append(_record())
    second = await ledger.append(
        replace(_record(), record_id=AuditRecordId(), category=AuditCategory.SYSTEM)
    )
    object.__setattr__(second, "record_digest", "1" * 64)
    snapshot = await ledger.snapshot(AuditFilter(categories=frozenset({AuditCategory.SECURITY})))
    assert not snapshot.verification.valid and snapshot.entries[0].sequence == 1


@pytest.mark.asyncio
async def test_filtered_audit_snapshot_does_not_rewrite_previous_digest() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    first = await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    snapshot = await ledger.snapshot(AuditFilter(minimum_sequence=2))
    assert snapshot.entries[0].previous_digest == first.entry_digest


@pytest.mark.asyncio
async def test_filtered_audit_snapshot_does_not_claim_subset_genesis() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    first = await ledger.append(_record())
    await ledger.append(replace(_record(), record_id=AuditRecordId()))
    snapshot = await ledger.snapshot(AuditFilter(sequences=frozenset({2})))
    assert snapshot.entries[0].previous_digest != "0" * 64
    assert snapshot.entries[0].previous_digest == first.entry_digest


@pytest.mark.asyncio
async def test_audit_snapshot_require_valid_reports_safe_failure() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    entry = await ledger.append(replace(_record(), summary=secret))
    object.__setattr__(entry, "entry_digest", "1" * 64)
    snapshot = await ledger.snapshot()
    with pytest.raises(AuditLedgerVerificationError) as captured:
        snapshot.require_valid()
    assert secret not in str(captured.value.to_dict())


@pytest.mark.asyncio
async def test_empty_ledger_head_corruption_detected() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    ledger._head_digest = "1" * 64
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.HEAD_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_empty_ledger_count_corruption_detected() -> None:
    ledger = InMemoryAuditLedger(clock=lambda: datetime.now(UTC))
    ledger._entry_count = 1
    assert (await ledger.verify()).failure is AuditLedgerVerificationFailure.ENTRY_COUNT_MISMATCH


def test_audit_query_errors_do_not_expose_secret_values() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    with pytest.raises(AuditFilterError) as captured:
        AuditFilter(record_attribute_equals={"nested": [object(), secret]})
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_audit_snapshot_errors_do_not_expose_secret_values() -> None:
    secret = "AUDIT-C2B2-QUERY-SECRET-913acf"
    value: dict[str, object] = {"secret": secret}
    with pytest.raises(AuditSerializationError) as captured:
        AuditLedgerSnapshot.from_dict(value)
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in str(captured.value.to_dict())


def test_public_audit_query_exports_preserve_class_identity() -> None:
    import alios_observability
    import alios_observability.audit as audit

    assert alios_observability.AuditFilter is audit.AuditFilter
    assert alios_observability.AuditLedgerSnapshot is audit.AuditLedgerSnapshot


@pytest.mark.asyncio
async def test_audit_query_import_creates_no_global_tasks() -> None:
    before = asyncio.all_tasks()
    import alios_observability.audit as audit

    await asyncio.sleep(0)
    assert asyncio.all_tasks() == before
    assert not any(
        isinstance(value, (AuditFilter, AuditLedgerSnapshot)) for value in audit.__dict__.values()
    )
