from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from alios_core.errors import (
    RuntimeObservabilityConfigurationError,
    RuntimeObservabilityContextError,
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
    current_execution_context,
    current_runtime_observability_context,
    execution_context_with_trace_context,
    trace_context_from_execution_context,
)


def _execution(**changes: Any) -> ExecutionContext:
    values: dict[str, Any] = {
        "run_id": RunId(),
        "correlation_id": CorrelationId(),
        "tenant_id": TenantId(),
        "user_id": UserId(),
        "current_task_id": TaskId(),
        "execution_mode": ExecutionMode.INTERACTIVE,
        "metadata": {"private": "execution"},
        "policy_context": {"private": "policy"},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(changes)
    return ExecutionContext(**values)


def _trace(execution: ExecutionContext) -> TraceContext:
    return TraceContext.create_root(
        correlation_id=execution.correlation_id,
        run_id=execution.run_id,
        tenant_id=execution.tenant_id,
        user_id=execution.user_id,
        baggage={"private": "baggage"},
    )


async def _started(**components: Any) -> DefaultRuntimeObservability:
    bridge = DefaultRuntimeObservability(**components)
    await bridge.start()
    return bridge


async def _read_current_bundle() -> RuntimeObservabilityContext | None:
    return current_runtime_observability_context()


class _BrokenRegistry:
    def __init__(self) -> None:
        self.real = InMemoryMetricRegistry()

    async def register_counter(self, descriptor: MetricDescriptor) -> Any:
        raise RuntimeError("private registry detail")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


class _OrderedResource:
    def __init__(self, name: str, order: list[str], fail: bool = False) -> None:
        self.name, self.order, self.fail = name, order, fail

    def __getattr__(self, _: str) -> Any:
        async def operation(*__: object, **___: object) -> None:
            pass

        return operation

    async def close(self) -> None:
        self.order.append(self.name)
        if self.fail:
            raise RuntimeError("private close detail")


@pytest.mark.asyncio
async def test_runtime_context_bridge_binds_execution_log_trace_and_audit() -> None:
    execution = _execution()
    trace = _trace(execution)
    bridge = await _started()
    bundle = bridge.create_context(execution, trace_context=trace)
    async with bridge.bind(bundle):
        assert current_execution_context() is bundle.execution_context
        assert current_log_context() is bundle.log_context
        assert current_trace_context() is trace
        assert current_audit_context() is bundle.audit_context


@pytest.mark.asyncio
async def test_runtime_context_bridge_without_trace() -> None:
    bridge = await _started()
    bundle = bridge.create_context(_execution())
    async with bridge.bind(bundle):
        assert current_trace_context() is None


def test_runtime_context_bridge_trace_round_trip() -> None:
    execution = _execution()
    trace = _trace(execution)
    assert (
        trace_context_from_execution_context(execution_context_with_trace_context(execution, trace))
        == trace
    )


def test_runtime_context_bridge_preserves_trace_baggage_only_in_trace_context() -> None:
    execution = _execution()
    bundle = RuntimeObservabilityContext.create(execution, trace_context=_trace(execution))
    assert bundle.trace_context is not None
    assert (
        "private" in bundle.trace_context.baggage and "private" not in bundle.log_context.attributes
    )


def test_runtime_context_bridge_does_not_copy_execution_metadata() -> None:
    bundle = RuntimeObservabilityContext.create(_execution())
    assert "private" not in bundle.log_context.attributes


def test_runtime_context_bridge_does_not_copy_policy_context() -> None:
    bundle = RuntimeObservabilityContext.create(_execution())
    assert "private" not in bundle.audit_context.metadata


@pytest.mark.asyncio
async def test_nested_runtime_context_bridges_restore_lineage() -> None:
    bridge = await _started()
    parent = bridge.create_context(_execution())
    child = bridge.create_context(_execution())
    async with bridge.bind(parent):
        async with bridge.bind(child):
            assert current_runtime_observability_context() is child
        assert current_runtime_observability_context() is parent


@pytest.mark.asyncio
async def test_concurrent_runtime_context_bridges_are_isolated() -> None:
    bridge = await _started()
    first = bridge.create_context(_execution())
    second = bridge.create_context(_execution())

    async def read(bundle: RuntimeObservabilityContext) -> RuntimeObservabilityContext | None:
        async with bridge.bind(bundle):
            return current_runtime_observability_context()

    assert tuple(await asyncio.gather(read(first), read(second))) == (first, second)


@pytest.mark.asyncio
async def test_child_task_inherits_complete_runtime_observability_context() -> None:
    bridge = await _started()
    bundle = bridge.create_context(_execution())
    async with bridge.bind(bundle):
        assert await asyncio.create_task(_read_current_bundle()) is bundle


@pytest.mark.asyncio
async def test_sibling_tasks_inherit_same_parent_snapshot() -> None:
    bridge = await _started()
    bundle = bridge.create_context(_execution())
    async with bridge.bind(bundle):
        assert await asyncio.gather(
            *[asyncio.create_task(_read_current_bundle()) for _ in range(2)]
        ) == [bundle, bundle]


@pytest.mark.asyncio
async def test_runtime_context_bridge_restores_after_child_failure() -> None:
    bridge = await _started()
    bundle = bridge.create_context(_execution())
    with pytest.raises(ValueError):
        async with bridge.bind(bundle):
            raise ValueError
    assert current_runtime_observability_context() is None


@pytest.mark.asyncio
async def test_runtime_context_bridge_restores_after_child_cancellation() -> None:
    bridge = await _started()
    bundle = bridge.create_context(_execution())
    with pytest.raises(asyncio.CancelledError):
        async with bridge.bind(bundle):
            raise asyncio.CancelledError
    assert current_runtime_observability_context() is None


@pytest.mark.asyncio
async def test_runtime_metric_registration_with_real_registry() -> None:
    assert (
        await RuntimeMetricInstruments.register(InMemoryMetricRegistry())
    ).execution_started is not None


@pytest.mark.asyncio
async def test_two_bridges_share_exact_runtime_metric_descriptors() -> None:
    registry = InMemoryMetricRegistry()
    first = await _started(metric_registry=registry)
    second = await _started(metric_registry=registry)
    assert first.metrics is not None and second.metrics is not None
    assert first.metrics.execution_started is second.metrics.execution_started


@pytest.mark.asyncio
async def test_runtime_metric_registration_preserves_existing_samples() -> None:
    registry = InMemoryMetricRegistry()
    metrics = await RuntimeMetricInstruments.register(registry)
    await metrics.execution_started.add(labels={"execution_mode": "interactive"})
    await RuntimeMetricInstruments.register(registry)
    assert cast(Any, (await metrics.execution_started.collect())[0]).value == 1


@pytest.mark.asyncio
async def test_runtime_metric_registration_rejects_conflicting_descriptor() -> None:
    registry = InMemoryMetricRegistry()
    await registry.register_counter(
        MetricDescriptor(
            "alios.runtime.execution.started_total",
            MetricKind.COUNTER,
            "conflict",
            "executions",
            ("execution_mode",),
        )
    )
    with pytest.raises(RuntimeObservabilityConfigurationError):
        await RuntimeMetricInstruments.register(registry)


@pytest.mark.asyncio
async def test_runtime_metric_registry_remains_open_when_unowned() -> None:
    registry = InMemoryMetricRegistry()
    bridge = await _started(metric_registry=registry)
    await bridge.close()
    assert not (await registry.status()).closed


@pytest.mark.asyncio
async def test_runtime_metric_registry_closes_when_owned() -> None:
    registry = InMemoryMetricRegistry()
    bridge = await _started(metric_registry=registry, owns_metric_registry=True)
    await bridge.close()
    assert (await registry.status()).closed


@pytest.mark.asyncio
async def test_runtime_observability_with_real_logger() -> None:
    logger = StructuredLogger(component="runtime", sink=InMemoryLogSink())
    assert (await _started(logger=logger)).logger is logger


@pytest.mark.asyncio
async def test_runtime_observability_with_real_tracer() -> None:
    tracer = DefaultTracer(TraceSource("runtime"))
    assert (await _started(tracer=tracer)).tracer is tracer


@pytest.mark.asyncio
async def test_runtime_observability_with_real_audit_ledger() -> None:
    ledger = InMemoryAuditLedger()
    assert (await _started(audit_ledger=ledger)).audit_ledger is ledger


@pytest.mark.asyncio
async def test_runtime_observability_with_all_real_components() -> None:
    bridge = await _started(
        logger=StructuredLogger(component="runtime", sink=InMemoryLogSink()),
        tracer=DefaultTracer(TraceSource("runtime")),
        metric_registry=InMemoryMetricRegistry(),
        audit_ledger=InMemoryAuditLedger(),
    )
    assert (await bridge.status()).metrics_registered


@pytest.mark.asyncio
async def test_runtime_observability_empty_bridge_still_binds_contexts() -> None:
    bridge = await _started()
    bundle = bridge.create_context(_execution())
    async with bridge.bind(bundle):
        assert current_runtime_observability_context() is bundle


@pytest.mark.asyncio
async def test_runtime_observability_fail_open_metric_registration_failure() -> None:
    bridge = await _started(metric_registry=cast(Any, _BrokenRegistry()))
    assert (await bridge.status()).failure_count == 1


@pytest.mark.asyncio
async def test_runtime_observability_fail_closed_metric_registration_failure() -> None:
    options = RuntimeObservabilityOptions(RuntimeObservabilityFailureMode.FAIL_CLOSED)
    bridge = DefaultRuntimeObservability(
        metric_registry=cast(Any, _BrokenRegistry()), options=options
    )
    with pytest.raises(RuntimeObservabilityLifecycleError):
        await bridge.start()


@pytest.mark.asyncio
async def test_runtime_observability_owned_resources_close_in_order() -> None:
    order: list[str] = []
    resources = [_OrderedResource(name, order) for name in ("tracer", "metrics", "audit")]
    bridge = DefaultRuntimeObservability(
        tracer=cast(Any, resources[0]),
        metric_registry=cast(Any, resources[1]),
        audit_ledger=cast(Any, resources[2]),
        owns_tracer=True,
        owns_metric_registry=True,
        owns_audit_ledger=True,
    )
    await bridge.close()
    assert order == ["tracer", "metrics", "audit"]


@pytest.mark.asyncio
async def test_runtime_observability_close_attempts_remaining_resources_after_failure() -> None:
    order: list[str] = []
    tracer = _OrderedResource("tracer", order, True)
    metrics = _OrderedResource("metrics", order)
    bridge = DefaultRuntimeObservability(
        tracer=cast(Any, tracer),
        metric_registry=cast(Any, metrics),
        owns_tracer=True,
        owns_metric_registry=True,
    )
    await bridge.close()
    assert order == ["tracer", "metrics"]


@pytest.mark.asyncio
async def test_runtime_observability_two_instances_are_isolated() -> None:
    first, second = await _started(), await _started()
    await first.close()
    assert (await first.status()).closed and not (await second.status()).closed


def test_runtime_observability_public_exports_preserve_class_identity() -> None:
    from alios_runtime.observability import DefaultRuntimeObservability as implementation

    assert DefaultRuntimeObservability is implementation


@pytest.mark.asyncio
async def test_runtime_observability_import_creates_no_global_tasks() -> None:
    assert len(asyncio.all_tasks()) == 1


def test_observability_package_does_not_import_runtime() -> None:
    import ast
    from pathlib import Path

    root = Path("packages/observability/alios_observability")
    assert all(
        "alios_runtime" not in ast.dump(ast.parse(path.read_text())) for path in root.glob("*.py")
    )


def test_runtime_observability_context_errors_do_not_expose_secret_values() -> None:
    execution = _execution()
    with pytest.raises(RuntimeObservabilityContextError) as caught:
        RuntimeObservabilityContext.create(
            execution, log_attributes={"execution_mode": "private-secret"}
        )
    assert "private-secret" not in str(caught.value)
