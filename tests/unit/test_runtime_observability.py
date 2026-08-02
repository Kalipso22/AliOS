import asyncio
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from alios_core import (
    RuntimeObservabilityClosedError,
    RuntimeObservabilityConfigurationError,
    RuntimeObservabilityContextError,
    RuntimeObservabilityEmissionError,
    RuntimeObservabilityError,
    RuntimeObservabilityLifecycleError,
)
from alios_core.ids import CorrelationId, RunId, TaskId, TenantId, UserId
from alios_core.types import ExecutionMode
from alios_observability import (
    DefaultTracer,
    InMemoryAuditLedger,
    InMemoryLogSink,
    InMemoryMetricRegistry,
    MetricDescriptor,
    MetricKind,
    StructuredLogger,
    TraceContext,
    TraceSource,
    current_audit_context,
    current_log_context,
    current_trace_context,
)
from alios_runtime import (
    DefaultRuntimeObservability,
    ExecutionContext,
    RuntimeMetricInstruments,
    RuntimeObservabilityContext,
    RuntimeObservabilityFailureMode,
    RuntimeObservabilityOptions,
    RuntimeObservabilityStatus,
    audit_context_from_execution_context,
    bind_runtime_observability_context,
    current_execution_context,
    current_runtime_observability_context,
    execution_context_with_trace_context,
    log_context_from_execution_context,
    require_runtime_observability_context,
    trace_context_from_execution_context,
)


def _execution_context(**overrides: Any) -> ExecutionContext:
    values: dict[str, Any] = {
        "run_id": RunId(),
        "correlation_id": CorrelationId(),
        "tenant_id": TenantId(),
        "user_id": UserId(),
        "current_task_id": TaskId(),
        "parent_run_id": RunId(),
        "execution_mode": ExecutionMode.SCHEDULED,
        "deadline": datetime(2026, 1, 2, tzinfo=UTC),
        "metadata": {"secret": "metadata"},
        "policy_context": {"secret": "policy"},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "attempt_number": 2,
    }
    values.update(overrides)
    return ExecutionContext(**values)


def _trace(context: ExecutionContext, **overrides: Any) -> TraceContext:
    values: dict[str, Any] = {
        "correlation_id": context.correlation_id,
        "run_id": context.run_id,
        "tenant_id": context.tenant_id,
        "user_id": context.user_id,
        "baggage": {"secret": "baggage"},
    }
    values.update(overrides)
    return TraceContext.create_root(**values)


def _bundle(*, trace: bool = True) -> RuntimeObservabilityContext:
    execution = _execution_context()
    return RuntimeObservabilityContext.create(
        execution,
        trace_context=_trace(execution) if trace else None,
        log_attributes={"explicit_log": "value"},
        audit_metadata={"explicit_audit": "value"},
    )


def _logger() -> StructuredLogger:
    return StructuredLogger(component="runtime", sink=InMemoryLogSink())


async def _read_current_bundle() -> RuntimeObservabilityContext | None:
    return current_runtime_observability_context()


def test_runtime_observability_error_codes() -> None:
    assert (
        RuntimeObservabilityError.code,
        RuntimeObservabilityConfigurationError.code,
        RuntimeObservabilityContextError.code,
        RuntimeObservabilityLifecycleError.code,
        RuntimeObservabilityClosedError.code,
        RuntimeObservabilityEmissionError.code,
    ) == (
        "runtime_observability_error",
        "runtime_observability_configuration_error",
        "runtime_observability_context_error",
        "runtime_observability_lifecycle_error",
        "runtime_observability_closed",
        "runtime_observability_emission_error",
    )


def test_runtime_observability_errors_are_safe() -> None:
    secret = "RUNTIME-OBSERVABILITY-SECRET"
    error = RuntimeObservabilityContextError("safe", details={"field_name": "metadata"})
    assert (
        secret not in str(error)
        and secret not in repr(error)
        and secret not in str(error.to_dict())
    )


def test_runtime_observability_failure_mode_exact_values() -> None:
    assert [item.value for item in RuntimeObservabilityFailureMode] == ["fail_open", "fail_closed"]


def test_runtime_observability_failure_mode_parsing() -> None:
    assert (
        RuntimeObservabilityFailureMode.parse("fail_open")
        is RuntimeObservabilityFailureMode.FAIL_OPEN
    )


def test_runtime_observability_failure_mode_rejects_unknown() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        RuntimeObservabilityFailureMode.parse("other")


def test_runtime_observability_failure_mode_rejects_non_string() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        RuntimeObservabilityFailureMode.parse(cast(Any, 1))


def test_runtime_observability_options_defaults() -> None:
    options = RuntimeObservabilityOptions()
    assert options.failure_mode is RuntimeObservabilityFailureMode.FAIL_OPEN


def test_runtime_observability_options_rejects_wrong_failure_mode() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        RuntimeObservabilityOptions(failure_mode=cast(Any, "fail_open"))


def test_runtime_observability_options_rejects_non_boolean_parent_flag() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        RuntimeObservabilityOptions(include_parent_run_attribute=cast(Any, 1))


def test_runtime_observability_options_rejects_non_boolean_deadline_flag() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        RuntimeObservabilityOptions(include_deadline_attribute=cast(Any, 0))


def test_runtime_observability_options_serialization() -> None:
    assert RuntimeObservabilityOptions().to_dict()["failure_mode"] == "fail_open"


def test_runtime_observability_options_round_trip() -> None:
    options = RuntimeObservabilityOptions(RuntimeObservabilityFailureMode.FAIL_CLOSED, False, False)
    assert RuntimeObservabilityOptions.from_dict(options.to_dict()) == options


def test_runtime_observability_options_from_dict_uses_strict_types() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        RuntimeObservabilityOptions.from_dict(
            {
                "failure_mode": "fail_open",
                "include_parent_run_attribute": 1,
                "include_deadline_attribute": True,
            }
        )


def test_execution_trace_context_empty_returns_none() -> None:
    assert trace_context_from_execution_context(_execution_context()) is None


def test_execution_trace_context_round_trip() -> None:
    execution = _execution_context()
    trace = _trace(execution)
    assert (
        trace_context_from_execution_context(execution_context_with_trace_context(execution, trace))
        == trace
    )


def test_execution_trace_context_rejects_malformed_payload() -> None:
    with pytest.raises(RuntimeObservabilityContextError):
        trace_context_from_execution_context(_execution_context(trace_context={"bad": "value"}))


def test_execution_trace_context_rejects_run_mismatch() -> None:
    execution = _execution_context()
    with pytest.raises(RuntimeObservabilityContextError):
        execution_context_with_trace_context(execution, _trace(execution, run_id=RunId()))


def test_execution_trace_context_rejects_correlation_mismatch() -> None:
    execution = _execution_context()
    with pytest.raises(RuntimeObservabilityContextError):
        execution_context_with_trace_context(
            execution, _trace(execution, correlation_id=CorrelationId())
        )


def test_execution_trace_context_rejects_tenant_mismatch() -> None:
    execution = _execution_context()
    with pytest.raises(RuntimeObservabilityContextError):
        execution_context_with_trace_context(execution, _trace(execution, tenant_id=TenantId()))


def test_execution_trace_context_rejects_user_mismatch() -> None:
    execution = _execution_context()
    with pytest.raises(RuntimeObservabilityContextError):
        execution_context_with_trace_context(execution, _trace(execution, user_id=UserId()))


@pytest.mark.asyncio
async def test_execution_trace_context_does_not_capture_current_trace() -> None:
    execution = _execution_context()
    from alios_observability import bind_trace_context

    async with bind_trace_context(_trace(execution)):
        assert trace_context_from_execution_context(execution) is None


def test_execution_trace_context_preserves_baggage() -> None:
    execution = _execution_context()
    trace = _trace(execution, baggage={"password": "exact-secret"})
    restored = trace_context_from_execution_context(
        execution_context_with_trace_context(execution, trace)
    )
    assert restored is not None and restored.baggage["password"] == "exact-secret"


def test_execution_trace_context_does_not_mutate_execution_context() -> None:
    execution = _execution_context()
    execution_context_with_trace_context(execution, _trace(execution))
    assert execution.trace_context == {}


def test_execution_context_with_trace_context() -> None:
    execution = _execution_context()
    assert execution_context_with_trace_context(execution, _trace(execution)).trace_context


def test_execution_context_with_trace_context_none() -> None:
    execution = _execution_context()
    assert execution_context_with_trace_context(execution, None).trace_context == {}


def test_execution_context_with_trace_context_rejects_wrong_type() -> None:
    with pytest.raises(RuntimeObservabilityContextError):
        execution_context_with_trace_context(_execution_context(), cast(Any, {}))


def test_execution_context_with_trace_context_rejects_scope_mismatch() -> None:
    execution = _execution_context()
    with pytest.raises(RuntimeObservabilityContextError):
        execution_context_with_trace_context(execution, TraceContext.create_root())


def test_execution_context_with_trace_context_is_immutable() -> None:
    execution = _execution_context()
    updated = execution_context_with_trace_context(execution, _trace(execution))
    assert updated is not execution and execution.trace_context == {}


def test_execution_context_with_trace_context_uses_exact_serialization() -> None:
    execution = _execution_context()
    updated = execution_context_with_trace_context(
        execution, _trace(execution, baggage={"password": "secret"})
    )
    assert cast(Mapping[str, object], updated.trace_context)["baggage"] == {"password": "secret"}


def test_runtime_log_context_maps_execution_identifiers() -> None:
    execution = _execution_context()
    log = log_context_from_execution_context(execution)
    assert (log.run_id, log.correlation_id, log.tenant_id, log.user_id) == (
        execution.run_id,
        execution.correlation_id,
        execution.tenant_id,
        execution.user_id,
    )


def test_runtime_log_context_maps_task_id() -> None:
    execution = _execution_context()
    assert log_context_from_execution_context(execution).task_id == execution.current_task_id


def test_runtime_log_context_includes_execution_mode() -> None:
    assert (
        log_context_from_execution_context(_execution_context()).attributes["execution_mode"]
        == "scheduled"
    )


def test_runtime_log_context_includes_attempt_number() -> None:
    assert (
        log_context_from_execution_context(_execution_context()).attributes["attempt_number"] == 2
    )


def test_runtime_log_context_includes_parent_run() -> None:
    execution = _execution_context()
    assert log_context_from_execution_context(execution).attributes["parent_run_id"] == str(
        execution.parent_run_id
    )


def test_runtime_log_context_can_omit_parent_run() -> None:
    options = RuntimeObservabilityOptions(include_parent_run_attribute=False)
    assert (
        "parent_run_id"
        not in log_context_from_execution_context(_execution_context(), options=options).attributes
    )


def test_runtime_log_context_includes_deadline() -> None:
    execution = _execution_context()
    assert execution.deadline is not None
    assert (
        log_context_from_execution_context(execution).attributes["deadline"]
        == execution.deadline.isoformat()
    )


def test_runtime_log_context_can_omit_deadline() -> None:
    options = RuntimeObservabilityOptions(include_deadline_attribute=False)
    assert (
        "deadline"
        not in log_context_from_execution_context(_execution_context(), options=options).attributes
    )


def test_runtime_log_context_includes_trace_identifiers() -> None:
    execution = _execution_context()
    trace = _trace(execution)
    assert log_context_from_execution_context(execution, trace_context=trace).attributes[
        "trace_id"
    ] == str(trace.trace_id)


def test_runtime_log_context_does_not_copy_baggage() -> None:
    execution = _execution_context()
    log = log_context_from_execution_context(execution, trace_context=_trace(execution))
    assert "secret" not in log.attributes


def test_runtime_log_context_does_not_copy_execution_metadata() -> None:
    assert "secret" not in log_context_from_execution_context(_execution_context()).attributes


def test_runtime_log_context_accepts_explicit_attributes() -> None:
    assert (
        log_context_from_execution_context(
            _execution_context(), attributes={"region": "eu"}
        ).attributes["region"]
        == "eu"
    )


def test_runtime_log_context_rejects_non_mapping_attributes() -> None:
    with pytest.raises(RuntimeObservabilityContextError):
        log_context_from_execution_context(_execution_context(), attributes=cast(Any, []))


def test_runtime_log_context_rejects_structural_collision() -> None:
    with pytest.raises(RuntimeObservabilityContextError):
        log_context_from_execution_context(
            _execution_context(), attributes={"execution_mode": "other"}
        )


def test_runtime_log_context_caller_attributes_are_unchanged() -> None:
    values = {"nested": [1]}
    log_context_from_execution_context(_execution_context(), attributes=values)
    assert values == {"nested": [1]}


def test_runtime_log_context_attributes_are_immutable() -> None:
    log = log_context_from_execution_context(_execution_context(), attributes={"x": 1})
    with pytest.raises(TypeError):
        cast(dict[str, object], log.attributes)["x"] = 2


def test_runtime_audit_context_maps_execution_identifiers() -> None:
    execution = _execution_context()
    audit = audit_context_from_execution_context(execution)
    assert (audit.run_id, audit.correlation_id, audit.tenant_id, audit.user_id) == (
        execution.run_id,
        execution.correlation_id,
        execution.tenant_id,
        execution.user_id,
    )


def test_runtime_audit_context_maps_trace_identifiers() -> None:
    execution = _execution_context()
    trace = _trace(execution)
    audit = audit_context_from_execution_context(execution, trace_context=trace)
    assert (audit.trace_id, audit.span_id) == (trace.trace_id, trace.span_id)


def test_runtime_audit_context_does_not_copy_baggage() -> None:
    execution = _execution_context()
    assert (
        "secret"
        not in audit_context_from_execution_context(
            execution, trace_context=_trace(execution)
        ).metadata
    )


def test_runtime_audit_context_does_not_copy_execution_metadata() -> None:
    assert "secret" not in audit_context_from_execution_context(_execution_context()).metadata


def test_runtime_audit_context_does_not_copy_policy_context() -> None:
    assert "secret" not in audit_context_from_execution_context(_execution_context()).metadata


def test_runtime_audit_context_accepts_explicit_metadata() -> None:
    assert (
        audit_context_from_execution_context(
            _execution_context(), metadata={"region": "eu"}
        ).metadata["region"]
        == "eu"
    )


def test_runtime_audit_context_rejects_non_mapping_metadata() -> None:
    with pytest.raises(RuntimeObservabilityContextError):
        audit_context_from_execution_context(_execution_context(), metadata=cast(Any, False))


def test_runtime_audit_context_rejects_structural_collision() -> None:
    with pytest.raises(RuntimeObservabilityContextError):
        audit_context_from_execution_context(_execution_context(), metadata={"attempt_number": 10})


def test_runtime_audit_context_caller_metadata_is_unchanged() -> None:
    values = {"nested": [1]}
    audit_context_from_execution_context(_execution_context(), metadata=values)
    assert values == {"nested": [1]}


def test_runtime_audit_context_metadata_is_immutable() -> None:
    audit = audit_context_from_execution_context(_execution_context())
    with pytest.raises(TypeError):
        cast(dict[str, object], audit.metadata)["x"] = 1


def test_runtime_observability_context_create_without_trace() -> None:
    bundle = RuntimeObservabilityContext.create(_execution_context())
    assert bundle.trace_context is None and bundle.execution_context.trace_context == {}


def test_runtime_observability_context_create_with_explicit_trace() -> None:
    execution = _execution_context()
    trace = _trace(execution)
    assert RuntimeObservabilityContext.create(execution, trace_context=trace).trace_context is trace


def test_runtime_observability_context_uses_stored_trace() -> None:
    execution = _execution_context()
    trace = _trace(execution)
    stored = execution_context_with_trace_context(execution, trace)
    assert RuntimeObservabilityContext.create(stored).trace_context == trace


def test_runtime_observability_context_rejects_explicit_stored_trace_mismatch() -> None:
    execution = _execution_context()
    stored = execution_context_with_trace_context(execution, _trace(execution))
    with pytest.raises(RuntimeObservabilityContextError):
        RuntimeObservabilityContext.create(stored, trace_context=_trace(execution))


def test_runtime_observability_context_rejects_wrong_execution_context() -> None:
    bundle = _bundle()
    with pytest.raises(RuntimeObservabilityContextError):
        replace(bundle, execution_context=cast(Any, object()))


def test_runtime_observability_context_rejects_wrong_log_context() -> None:
    bundle = _bundle()
    with pytest.raises(RuntimeObservabilityContextError):
        replace(bundle, log_context=cast(Any, object()))


def test_runtime_observability_context_rejects_wrong_trace_context() -> None:
    bundle = _bundle()
    with pytest.raises(RuntimeObservabilityContextError):
        replace(bundle, trace_context=cast(Any, object()))


def test_runtime_observability_context_rejects_wrong_audit_context() -> None:
    bundle = _bundle()
    with pytest.raises(RuntimeObservabilityContextError):
        replace(bundle, audit_context=cast(Any, object()))


def test_runtime_observability_context_rejects_log_scope_mismatch() -> None:
    bundle = _bundle()
    with pytest.raises(RuntimeObservabilityContextError):
        replace(bundle, log_context=replace(bundle.log_context, run_id=RunId()))


def test_runtime_observability_context_rejects_audit_scope_mismatch() -> None:
    bundle = _bundle()
    with pytest.raises(RuntimeObservabilityContextError):
        replace(bundle, audit_context=replace(bundle.audit_context, run_id=RunId()))


def test_runtime_observability_context_is_immutable() -> None:
    def mutate(value: object) -> None:
        delattr(value, "trace_context")

    with pytest.raises(FrozenInstanceError):
        mutate(_bundle())


def test_runtime_observability_context_with_trace_context() -> None:
    bundle = _bundle(trace=False)
    trace = _trace(bundle.execution_context)
    assert bundle.with_trace_context(trace).trace_context is trace


def test_runtime_observability_context_with_trace_context_none() -> None:
    updated = _bundle().with_trace_context(None)
    assert updated.trace_context is None and updated.audit_context.trace_id is None


def test_runtime_observability_context_with_trace_preserves_non_trace_attributes() -> None:
    bundle = _bundle(trace=False)
    updated = bundle.with_trace_context(_trace(bundle.execution_context))
    assert updated.log_context.attributes["explicit_log"] == "value"
    assert updated.audit_context.metadata["explicit_audit"] == "value"


def test_runtime_observability_context_with_trace_does_not_mutate_original() -> None:
    bundle = _bundle(trace=False)
    bundle.with_trace_context(_trace(bundle.execution_context))
    assert bundle.trace_context is None


def test_runtime_observability_context_is_unbound_by_default() -> None:
    assert current_runtime_observability_context() is None


def test_require_runtime_observability_context_raises_when_unbound() -> None:
    with pytest.raises(RuntimeObservabilityContextError):
        require_runtime_observability_context()


@pytest.mark.asyncio
async def test_runtime_observability_binding_binds_all_contexts() -> None:
    bundle = _bundle()
    async with bind_runtime_observability_context(bundle):
        assert current_execution_context() is bundle.execution_context
        assert current_log_context() is bundle.log_context
        assert current_trace_context() is bundle.trace_context
        assert current_audit_context() is bundle.audit_context
        assert current_runtime_observability_context() is bundle


@pytest.mark.asyncio
async def test_runtime_observability_binding_without_trace() -> None:
    bundle = _bundle(trace=False)
    async with bind_runtime_observability_context(bundle):
        assert (
            current_trace_context() is None
            and current_execution_context() is bundle.execution_context
        )


@pytest.mark.asyncio
async def test_runtime_observability_binding_restores_all_contexts() -> None:
    async with bind_runtime_observability_context(_bundle()):
        assert current_runtime_observability_context() is not None
    assert current_execution_context() is None
    assert current_log_context() is None
    assert current_trace_context() is None
    assert current_audit_context() is None


@pytest.mark.asyncio
async def test_nested_runtime_observability_binding_restores_parent() -> None:
    parent, child = _bundle(), _bundle()
    async with bind_runtime_observability_context(parent):
        async with bind_runtime_observability_context(child):
            assert current_runtime_observability_context() is child
        assert current_runtime_observability_context() is parent


@pytest.mark.asyncio
async def test_runtime_observability_binding_restores_after_exception() -> None:
    with pytest.raises(RuntimeError):
        async with bind_runtime_observability_context(_bundle()):
            raise RuntimeError("safe")
    assert current_runtime_observability_context() is None


@pytest.mark.asyncio
async def test_runtime_observability_binding_restores_after_cancellation() -> None:
    with pytest.raises(asyncio.CancelledError):
        async with bind_runtime_observability_context(_bundle()):
            raise asyncio.CancelledError
    assert current_runtime_observability_context() is None


def test_runtime_observability_binding_rejects_wrong_context() -> None:
    with pytest.raises(RuntimeObservabilityContextError):
        bind_runtime_observability_context(cast(Any, object()))


@pytest.mark.asyncio
async def test_runtime_observability_binding_rejects_active_reentry() -> None:
    binding = bind_runtime_observability_context(_bundle())
    async with binding:
        with pytest.raises(RuntimeObservabilityContextError):
            await binding.__aenter__()


@pytest.mark.asyncio
async def test_runtime_observability_binding_resets_once() -> None:
    binding = bind_runtime_observability_context(_bundle())
    await binding.__aenter__()
    await binding.__aexit__(None, None, None)
    await binding.__aexit__(None, None, None)
    assert current_runtime_observability_context() is None


@pytest.mark.asyncio
async def test_runtime_observability_binding_partial_enter_failure_restores_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import alios_runtime.observability as observability

    def fail(_: object) -> Any:
        raise RuntimeObservabilityContextError("safe")

    monkeypatch.setattr(observability, "bind_log_context", fail)
    with pytest.raises(RuntimeObservabilityContextError):
        async with bind_runtime_observability_context(_bundle()):
            pass
    assert current_execution_context() is None


@pytest.mark.asyncio
async def test_runtime_observability_binding_child_task_inherits_snapshot() -> None:
    bundle = _bundle()
    async with bind_runtime_observability_context(bundle):
        task = asyncio.create_task(_read_current_bundle())
        assert await task is bundle


@pytest.mark.asyncio
async def test_runtime_observability_binding_sibling_tasks_are_isolated() -> None:
    async def read(bundle: RuntimeObservabilityContext) -> RuntimeObservabilityContext | None:
        async with bind_runtime_observability_context(bundle):
            return current_runtime_observability_context()

    first, second = _bundle(), _bundle()
    assert tuple(await asyncio.gather(read(first), read(second))) == (first, second)


@pytest.mark.asyncio
async def test_runtime_metric_descriptor_names() -> None:
    metrics = await RuntimeMetricInstruments.register(InMemoryMetricRegistry())
    assert [
        metrics.execution_started.descriptor.name,
        metrics.execution_completed.descriptor.name,
        metrics.execution_active.descriptor.name,
        metrics.execution_duration.descriptor.name,
        metrics.observability_failures.descriptor.name,
    ] == [
        "alios.runtime.execution.started_total",
        "alios.runtime.execution.completed_total",
        "alios.runtime.execution.active",
        "alios.runtime.execution.duration_seconds",
        "alios.runtime.observability.failure_total",
    ]


@pytest.mark.asyncio
async def test_runtime_metric_descriptor_kinds() -> None:
    metrics = await RuntimeMetricInstruments.register(InMemoryMetricRegistry())
    assert (
        metrics.execution_started.descriptor.kind,
        metrics.execution_active.descriptor.kind,
        metrics.execution_duration.descriptor.kind,
    ) == (MetricKind.COUNTER, MetricKind.GAUGE, MetricKind.HISTOGRAM)


@pytest.mark.asyncio
async def test_runtime_metric_descriptor_units() -> None:
    metrics = await RuntimeMetricInstruments.register(InMemoryMetricRegistry())
    assert metrics.execution_started.descriptor.unit == "executions"
    assert metrics.execution_duration.descriptor.unit == "s"
    assert metrics.observability_failures.descriptor.unit == "failures"


@pytest.mark.asyncio
async def test_runtime_metric_descriptor_label_schemas() -> None:
    metrics = await RuntimeMetricInstruments.register(InMemoryMetricRegistry())
    assert metrics.execution_completed.descriptor.label_names == ("execution_mode", "outcome")
    assert metrics.observability_failures.descriptor.label_names == ("operation", "signal")


@pytest.mark.asyncio
async def test_runtime_duration_histogram_boundaries() -> None:
    metrics = await RuntimeMetricInstruments.register(InMemoryMetricRegistry())
    assert metrics.execution_duration.descriptor.histogram_boundaries == (
        0.001,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
        300.0,
    )


@pytest.mark.asyncio
async def test_runtime_metrics_have_no_high_cardinality_labels() -> None:
    metrics = await RuntimeMetricInstruments.register(InMemoryMetricRegistry())
    names = set().union(
        metrics.execution_started.descriptor.label_names,
        metrics.execution_completed.descriptor.label_names,
        metrics.execution_active.descriptor.label_names,
        metrics.execution_duration.descriptor.label_names,
        metrics.observability_failures.descriptor.label_names,
    )
    assert names.isdisjoint(
        {"run_id", "correlation_id", "tenant_id", "user_id", "task_id", "exception_type"}
    )


@pytest.mark.asyncio
async def test_runtime_metric_registration() -> None:
    registry = InMemoryMetricRegistry()
    metrics = await RuntimeMetricInstruments.register(registry)
    assert (
        await registry.get_counter(metrics.execution_started.descriptor.name)
        is metrics.execution_started
    )


@pytest.mark.asyncio
async def test_runtime_metric_registration_reuses_exact_descriptors() -> None:
    registry = InMemoryMetricRegistry()
    first = await RuntimeMetricInstruments.register(registry)
    second = await RuntimeMetricInstruments.register(registry)
    assert second.execution_started is first.execution_started


@pytest.mark.asyncio
async def test_runtime_metric_registration_rejects_descriptor_mismatch() -> None:
    registry = InMemoryMetricRegistry()
    await registry.register_counter(
        MetricDescriptor(
            "alios.runtime.execution.started_total",
            MetricKind.COUNTER,
            "wrong",
            "executions",
            ("execution_mode",),
        )
    )
    with pytest.raises(RuntimeObservabilityConfigurationError):
        await RuntimeMetricInstruments.register(registry)


@pytest.mark.asyncio
async def test_runtime_metric_registration_handles_concurrent_exact_registration() -> None:
    registry = InMemoryMetricRegistry()
    first, second = await asyncio.gather(
        RuntimeMetricInstruments.register(registry), RuntimeMetricInstruments.register(registry)
    )
    assert first.execution_duration is second.execution_duration


@pytest.mark.asyncio
async def test_runtime_metric_registration_does_not_reset_samples() -> None:
    registry = InMemoryMetricRegistry()
    first = await RuntimeMetricInstruments.register(registry)
    await first.execution_started.add(labels={"execution_mode": "batch"})
    await RuntimeMetricInstruments.register(registry)
    assert cast(Any, (await first.execution_started.collect())[0]).value == 1


@pytest.mark.asyncio
async def test_runtime_metric_registration_does_not_close_registry() -> None:
    registry = InMemoryMetricRegistry()
    await RuntimeMetricInstruments.register(registry)
    assert not (await registry.status()).closed


@pytest.mark.asyncio
async def test_runtime_metric_instruments_are_immutable() -> None:
    metrics = await RuntimeMetricInstruments.register(InMemoryMetricRegistry())

    def mutate(value: object) -> None:
        delattr(value, "execution_started")

    with pytest.raises(FrozenInstanceError):
        mutate(metrics)


def _status(**overrides: Any) -> RuntimeObservabilityStatus:
    values: dict[str, Any] = {
        "started": False,
        "closed": False,
        "logging_available": False,
        "tracing_available": False,
        "metrics_available": False,
        "audit_available": False,
        "metrics_registered": False,
        "failure_count": 0,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return RuntimeObservabilityStatus(**values)


def test_runtime_observability_status_initial() -> None:
    status = _status()
    assert not status.started and not status.closed


def test_runtime_observability_status_started() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    assert _status(started=True, started_at=instant).started


def test_runtime_observability_status_closed() -> None:
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    assert _status(closed=True, closed_at=instant).closed


def test_runtime_observability_status_rejects_boolean_failure_count() -> None:
    with pytest.raises(RuntimeObservabilityLifecycleError):
        _status(failure_count=True)


def test_runtime_observability_status_rejects_naive_timestamp() -> None:
    with pytest.raises(RuntimeObservabilityLifecycleError):
        _status(created_at=datetime.now())


def test_runtime_observability_status_rejects_invalid_time_order() -> None:
    with pytest.raises(RuntimeObservabilityLifecycleError):
        _status(started=True, started_at=datetime(2025, 1, 1, tzinfo=UTC))


def test_runtime_observability_status_rejects_registered_metrics_without_start() -> None:
    with pytest.raises(RuntimeObservabilityLifecycleError):
        _status(metrics_available=True, metrics_registered=True)


def test_runtime_observability_status_serialization() -> None:
    assert _status().to_dict()["failure_count"] == 0


def test_runtime_observability_status_round_trip() -> None:
    status = _status()
    assert RuntimeObservabilityStatus.from_dict(status.to_dict()) == status


class _FailOnceRegistry:
    def __init__(self) -> None:
        self.registry = InMemoryMetricRegistry()
        self.failed = False

    async def register_counter(self, descriptor: MetricDescriptor) -> Any:
        if not self.failed:
            self.failed = True
            raise RuntimeError("secret failure")
        return await self.registry.register_counter(descriptor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)


class _BlockingRegistry:
    def __init__(self) -> None:
        self.registry = InMemoryMetricRegistry()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def register_counter(self, descriptor: MetricDescriptor) -> Any:
        self.entered.set()
        await self.release.wait()
        return await self.registry.register_counter(descriptor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.registry, name)


class _CloseResource:
    def __init__(self, name: str, order: list[str], *, fail: bool = False) -> None:
        self.name, self.order, self.fail = name, order, fail
        self.close_count = 0

    def __getattr__(self, _: str) -> Any:
        async def operation(*__: object, **___: object) -> None:
            return None

        return operation

    async def close(self) -> None:
        self.close_count += 1
        self.order.append(self.name)
        if self.fail:
            raise RuntimeError("secret close failure")


def test_default_runtime_observability_empty_configuration() -> None:
    bridge = DefaultRuntimeObservability(clock=lambda: datetime.now(UTC))
    assert bridge.logger is None
    assert bridge.tracer is None
    assert bridge.metric_registry is None
    assert bridge.audit_ledger is None


def test_default_runtime_observability_with_logger() -> None:
    logger = _logger()
    assert DefaultRuntimeObservability(logger=logger).logger is logger


def test_default_runtime_observability_with_tracer() -> None:
    tracer = DefaultTracer(TraceSource("runtime"))
    assert DefaultRuntimeObservability(tracer=tracer).tracer is tracer


def test_default_runtime_observability_with_metric_registry() -> None:
    registry = InMemoryMetricRegistry()
    assert DefaultRuntimeObservability(metric_registry=registry).metric_registry is registry


def test_default_runtime_observability_with_audit_ledger() -> None:
    ledger = InMemoryAuditLedger()
    assert DefaultRuntimeObservability(audit_ledger=ledger).audit_ledger is ledger


def test_default_runtime_observability_rejects_wrong_logger() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        DefaultRuntimeObservability(logger=cast(Any, object()))


def test_default_runtime_observability_rejects_wrong_tracer() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        DefaultRuntimeObservability(tracer=cast(Any, object()))


def test_default_runtime_observability_rejects_wrong_metric_registry() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        DefaultRuntimeObservability(metric_registry=cast(Any, object()))


def test_default_runtime_observability_rejects_wrong_audit_ledger() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        DefaultRuntimeObservability(audit_ledger=cast(Any, object()))


def test_default_runtime_observability_rejects_non_boolean_ownership() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        DefaultRuntimeObservability(owns_tracer=cast(Any, 1))


def test_default_runtime_observability_rejects_naive_clock() -> None:
    with pytest.raises(RuntimeObservabilityConfigurationError):
        DefaultRuntimeObservability(clock=datetime.now)


@pytest.mark.asyncio
async def test_runtime_observability_start() -> None:
    bridge = DefaultRuntimeObservability()
    await bridge.start()
    assert (await bridge.status()).started


@pytest.mark.asyncio
async def test_runtime_observability_repeated_start() -> None:
    bridge = DefaultRuntimeObservability()
    await bridge.start()
    await bridge.start()
    assert (await bridge.status()).failure_count == 0


@pytest.mark.asyncio
async def test_runtime_observability_concurrent_start() -> None:
    bridge = DefaultRuntimeObservability(metric_registry=InMemoryMetricRegistry())
    await asyncio.gather(bridge.start(), bridge.start())
    assert (await bridge.status()).metrics_registered


@pytest.mark.asyncio
async def test_runtime_observability_close_prevents_in_flight_start_completion() -> None:
    registry = _BlockingRegistry()
    bridge = DefaultRuntimeObservability(metric_registry=cast(Any, registry))
    bundle = bridge.create_context(_execution_context())
    start_task = asyncio.create_task(bridge.start())
    await registry.entered.wait()
    close_scheduled = asyncio.Event()

    async def mark_close_scheduled() -> None:
        close_scheduled.set()

    close_task = asyncio.create_task(bridge.close())
    marker_task = asyncio.create_task(mark_close_scheduled())
    await close_scheduled.wait()
    await marker_task
    with pytest.raises(RuntimeObservabilityClosedError):
        bridge.bind(bundle)
    registry.release.set()
    with pytest.raises(RuntimeObservabilityClosedError):
        await start_task
    await close_task
    status = await bridge.status()
    assert status.closed and not status.started and not status.metrics_registered


@pytest.mark.asyncio
async def test_runtime_observability_start_registers_metrics() -> None:
    bridge = DefaultRuntimeObservability(metric_registry=InMemoryMetricRegistry())
    await bridge.start()
    assert bridge.metrics is not None


@pytest.mark.asyncio
async def test_runtime_observability_fail_closed_start_failure() -> None:
    options = RuntimeObservabilityOptions(RuntimeObservabilityFailureMode.FAIL_CLOSED)
    bridge = DefaultRuntimeObservability(
        metric_registry=cast(Any, _FailOnceRegistry()), options=options
    )
    with pytest.raises(RuntimeObservabilityLifecycleError):
        await bridge.start()
    assert not (await bridge.status()).started


@pytest.mark.asyncio
async def test_runtime_observability_fail_open_start_failure() -> None:
    options = RuntimeObservabilityOptions(RuntimeObservabilityFailureMode.FAIL_OPEN)
    bridge = DefaultRuntimeObservability(
        metric_registry=cast(Any, _FailOnceRegistry()), options=options
    )
    await bridge.start()
    status = await bridge.status()
    assert status.started and not status.metrics_registered and status.failure_count == 1


@pytest.mark.asyncio
async def test_runtime_observability_start_failure_can_retry() -> None:
    options = RuntimeObservabilityOptions(RuntimeObservabilityFailureMode.FAIL_CLOSED)
    bridge = DefaultRuntimeObservability(
        metric_registry=cast(Any, _FailOnceRegistry()), options=options
    )
    with pytest.raises(RuntimeObservabilityLifecycleError):
        await bridge.start()
    await bridge.start()
    assert (await bridge.status()).metrics_registered


@pytest.mark.asyncio
async def test_runtime_observability_create_context_before_start() -> None:
    bridge = DefaultRuntimeObservability()
    execution = _execution_context()
    assert bridge.create_context(execution).execution_context == execution


@pytest.mark.asyncio
async def test_runtime_observability_create_context_after_start() -> None:
    bridge = DefaultRuntimeObservability()
    await bridge.start()
    assert bridge.create_context(_execution_context()).trace_context is None


@pytest.mark.asyncio
async def test_runtime_observability_create_context_after_close() -> None:
    bridge = DefaultRuntimeObservability()
    await bridge.close()
    with pytest.raises(RuntimeObservabilityClosedError):
        bridge.create_context(_execution_context())


def test_runtime_observability_bind_before_start() -> None:
    bridge = DefaultRuntimeObservability()
    with pytest.raises(RuntimeObservabilityLifecycleError):
        bridge.bind(_bundle())


@pytest.mark.asyncio
async def test_runtime_observability_bind_after_start() -> None:
    bridge = DefaultRuntimeObservability()
    await bridge.start()
    bundle = bridge.create_context(_execution_context())
    async with bridge.bind(bundle):
        assert current_runtime_observability_context() is bundle


@pytest.mark.asyncio
async def test_runtime_observability_bind_after_close() -> None:
    bridge = DefaultRuntimeObservability()
    bundle = bridge.create_context(_execution_context())
    await bridge.start()
    await bridge.close()
    with pytest.raises(RuntimeObservabilityClosedError):
        bridge.bind(bundle)


@pytest.mark.asyncio
async def test_runtime_observability_close() -> None:
    bridge = DefaultRuntimeObservability()
    await bridge.close()
    assert (await bridge.status()).closed


@pytest.mark.asyncio
async def test_runtime_observability_repeated_close() -> None:
    resource = _CloseResource("tracer", [])
    bridge = DefaultRuntimeObservability(tracer=cast(Any, resource), owns_tracer=True)
    await bridge.close()
    await bridge.close()
    assert resource.close_count == 1


@pytest.mark.asyncio
async def test_runtime_observability_close_owned_tracer() -> None:
    resource = _CloseResource("tracer", [])
    await DefaultRuntimeObservability(tracer=cast(Any, resource), owns_tracer=True).close()
    assert resource.close_count == 1


@pytest.mark.asyncio
async def test_runtime_observability_close_owned_metric_registry() -> None:
    resource = _CloseResource("metrics", [])
    await DefaultRuntimeObservability(
        metric_registry=cast(Any, resource), owns_metric_registry=True
    ).close()
    assert resource.close_count == 1


@pytest.mark.asyncio
async def test_runtime_observability_close_owned_audit_ledger() -> None:
    resource = _CloseResource("audit", [])
    await DefaultRuntimeObservability(
        audit_ledger=cast(Any, resource), owns_audit_ledger=True
    ).close()
    assert resource.close_count == 1


@pytest.mark.asyncio
async def test_runtime_observability_does_not_close_unowned_tracer() -> None:
    resource = _CloseResource("tracer", [])
    await DefaultRuntimeObservability(tracer=cast(Any, resource)).close()
    assert resource.close_count == 0


@pytest.mark.asyncio
async def test_runtime_observability_does_not_close_unowned_metric_registry() -> None:
    resource = _CloseResource("metrics", [])
    await DefaultRuntimeObservability(metric_registry=cast(Any, resource)).close()
    assert resource.close_count == 0


@pytest.mark.asyncio
async def test_runtime_observability_does_not_close_unowned_audit_ledger() -> None:
    resource = _CloseResource("audit", [])
    await DefaultRuntimeObservability(audit_ledger=cast(Any, resource)).close()
    assert resource.close_count == 0


@pytest.mark.asyncio
async def test_runtime_observability_close_attempts_all_owned_resources() -> None:
    order: list[str] = []
    resources = [
        _CloseResource("tracer", order, fail=True),
        _CloseResource("metrics", order),
        _CloseResource("audit", order),
    ]
    options = RuntimeObservabilityOptions(RuntimeObservabilityFailureMode.FAIL_OPEN)
    bridge = DefaultRuntimeObservability(
        tracer=cast(Any, resources[0]),
        metric_registry=cast(Any, resources[1]),
        audit_ledger=cast(Any, resources[2]),
        options=options,
        owns_tracer=True,
        owns_metric_registry=True,
        owns_audit_ledger=True,
    )
    await bridge.close()
    assert order == ["tracer", "metrics", "audit"]


@pytest.mark.asyncio
async def test_runtime_observability_fail_open_close_failure() -> None:
    resource = _CloseResource("tracer", [], fail=True)
    options = RuntimeObservabilityOptions(RuntimeObservabilityFailureMode.FAIL_OPEN)
    bridge = DefaultRuntimeObservability(
        tracer=cast(Any, resource), options=options, owns_tracer=True
    )
    await bridge.close()
    assert (await bridge.status()).failure_count == 1


@pytest.mark.asyncio
async def test_runtime_observability_fail_closed_close_failure() -> None:
    resource = _CloseResource("tracer", [], fail=True)
    options = RuntimeObservabilityOptions(RuntimeObservabilityFailureMode.FAIL_CLOSED)
    bridge = DefaultRuntimeObservability(
        tracer=cast(Any, resource), options=options, owns_tracer=True
    )
    with pytest.raises(RuntimeObservabilityLifecycleError) as caught:
        await bridge.close()
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_runtime_observability_status_available_after_close() -> None:
    bridge = DefaultRuntimeObservability()
    await bridge.close()
    assert (await bridge.status()).closed_at is not None


@pytest.mark.asyncio
async def test_runtime_observability_creates_no_background_task() -> None:
    before = set(asyncio.all_tasks())
    bridge = DefaultRuntimeObservability()
    await bridge.start()
    await bridge.close()
    assert set(asyncio.all_tasks()) == before
