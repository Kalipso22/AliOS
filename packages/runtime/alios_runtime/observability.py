"""Provider-neutral runtime observability bridge foundations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self, cast

from alios_core.errors import (
    ResourceConflictError,
    RuntimeObservabilityClosedError,
    RuntimeObservabilityConfigurationError,
    RuntimeObservabilityContextError,
    RuntimeObservabilityLifecycleError,
)
from alios_core.types import JsonValue, utc_now
from alios_observability import (
    AuditContext,
    AuditLedger,
    Counter,
    Gauge,
    Histogram,
    LogContext,
    MetricDescriptor,
    MetricKind,
    MetricRegistry,
    RedactionPolicy,
    StructuredLogger,
    TraceContext,
    Tracer,
    bind_audit_context,
    bind_log_context,
    bind_trace_context,
)

from .execution_context import ExecutionContext, bind_execution_context

_CURRENT: ContextVar[RuntimeObservabilityContext | None] = ContextVar(
    "alios_runtime_observability_context", default=None
)
_TRACE_ATTRIBUTE_NAMES = frozenset({"trace_id", "span_id", "parent_span_id", "trace_sampled"})
_RUNTIME_ATTRIBUTE_NAMES = frozenset(
    {"execution_mode", "attempt_number", "parent_run_id", "deadline"}
)
_DURATION_BOUNDARIES = (
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


class RuntimeObservabilityFailureMode(StrEnum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"

    @classmethod
    def parse(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise RuntimeObservabilityConfigurationError(
                "Invalid runtime observability failure mode"
            )
        try:
            return cls(value)
        except ValueError as error:
            raise RuntimeObservabilityConfigurationError(
                "Invalid runtime observability failure mode"
            ) from error


@dataclass(frozen=True, slots=True)
class RuntimeObservabilityOptions:
    failure_mode: RuntimeObservabilityFailureMode = RuntimeObservabilityFailureMode.FAIL_OPEN
    include_parent_run_attribute: bool = True
    include_deadline_attribute: bool = True

    def __post_init__(self) -> None:
        if type(self.failure_mode) is not RuntimeObservabilityFailureMode or any(
            type(value) is not bool
            for value in (
                self.include_parent_run_attribute,
                self.include_deadline_attribute,
            )
        ):
            raise RuntimeObservabilityConfigurationError("Invalid runtime observability options")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "failure_mode": self.failure_mode.value,
            "include_parent_run_attribute": self.include_parent_run_attribute,
            "include_deadline_attribute": self.include_deadline_attribute,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("failure_mode"), str)
                or type(value.get("include_parent_run_attribute")) is not bool
                or type(value.get("include_deadline_attribute")) is not bool
            ):
                raise ValueError
            return cls(
                RuntimeObservabilityFailureMode.parse(cast(str, value["failure_mode"])),
                cast(bool, value["include_parent_run_attribute"]),
                cast(bool, value["include_deadline_attribute"]),
            )
        except (KeyError, TypeError, ValueError, RuntimeObservabilityConfigurationError) as error:
            raise RuntimeObservabilityConfigurationError(
                "Invalid serialized runtime observability options"
            ) from error


def _no_redaction_policy() -> RedactionPolicy:
    return RedactionPolicy(include_default_rules=False)


def _execution(value: object) -> ExecutionContext:
    if type(value) is not ExecutionContext:
        raise RuntimeObservabilityContextError("Invalid execution context")
    return value


def _trace_scope(context: ExecutionContext, trace: TraceContext) -> None:
    if (
        trace.run_id != context.run_id
        or trace.correlation_id != context.correlation_id
        or trace.tenant_id != context.tenant_id
        or trace.user_id != context.user_id
    ):
        raise RuntimeObservabilityContextError("Runtime observability trace scope mismatch")


def trace_context_from_execution_context(context: ExecutionContext) -> TraceContext | None:
    execution = _execution(context)
    if not execution.trace_context:
        return None
    try:
        trace = TraceContext.from_dict(execution.trace_context)
    except Exception as error:
        raise RuntimeObservabilityContextError(
            "Invalid serialized runtime trace context"
        ) from error
    _trace_scope(execution, trace)
    return trace


def execution_context_with_trace_context(
    context: ExecutionContext, trace_context: TraceContext | None
) -> ExecutionContext:
    execution = _execution(context)
    if trace_context is None:
        return execution.with_trace_context({})
    if type(trace_context) is not TraceContext:
        raise RuntimeObservabilityContextError("Invalid runtime trace context")
    _trace_scope(execution, trace_context)
    return execution.with_trace_context(trace_context.to_dict(_no_redaction_policy()))


def _options(value: RuntimeObservabilityOptions | None) -> RuntimeObservabilityOptions:
    if value is None:
        return RuntimeObservabilityOptions()
    if type(value) is not RuntimeObservabilityOptions:
        raise RuntimeObservabilityContextError("Invalid runtime observability options")
    return value


def _structural_attributes(
    context: ExecutionContext, options: RuntimeObservabilityOptions
) -> dict[str, object]:
    result: dict[str, object] = {
        "execution_mode": context.execution_mode.value,
        "attempt_number": context.attempt_number,
    }
    if context.parent_run_id is not None and options.include_parent_run_attribute:
        result["parent_run_id"] = str(context.parent_run_id)
    if context.deadline is not None and options.include_deadline_attribute:
        result["deadline"] = context.deadline.isoformat()
    return result


def _explicit_mapping(value: object, field_name: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RuntimeObservabilityContextError(
            "Invalid runtime observability mapping", details={"field_name": field_name}
        )
    if not all(isinstance(key, str) for key in value):
        raise RuntimeObservabilityContextError(
            "Invalid runtime observability mapping", details={"field_name": field_name}
        )
    return dict(value)


def _merge_attributes(
    structural: Mapping[str, object], explicit: Mapping[str, object], field_name: str
) -> dict[str, object]:
    collisions = set(structural).intersection(explicit)
    if collisions:
        raise RuntimeObservabilityContextError(
            "Runtime observability structural attribute collision",
            details={"field_name": field_name},
        )
    return {**structural, **explicit}


def _resolved_trace(
    context: ExecutionContext, trace_context: TraceContext | None
) -> TraceContext | None:
    if trace_context is not None and type(trace_context) is not TraceContext:
        raise RuntimeObservabilityContextError("Invalid runtime trace context")
    if trace_context is not None:
        _trace_scope(context, trace_context)
    return trace_context


def log_context_from_execution_context(
    context: ExecutionContext,
    *,
    trace_context: TraceContext | None = None,
    attributes: Mapping[str, object] | None = None,
    options: RuntimeObservabilityOptions | None = None,
) -> LogContext:
    execution = _execution(context)
    trace = _resolved_trace(execution, trace_context)
    structural = _structural_attributes(execution, _options(options))
    if trace is not None:
        structural.update(
            {
                "trace_id": str(trace.trace_id),
                "span_id": str(trace.span_id),
                "parent_span_id": str(trace.parent_span_id)
                if trace.parent_span_id is not None
                else None,
                "trace_sampled": trace.sampled,
            }
        )
    explicit = _explicit_mapping(attributes, "attributes")
    try:
        return LogContext(
            correlation_id=execution.correlation_id,
            run_id=execution.run_id,
            tenant_id=execution.tenant_id,
            user_id=execution.user_id,
            task_id=execution.current_task_id,
            attributes=_merge_attributes(structural, explicit, "attributes"),
        )
    except Exception as error:
        if isinstance(error, RuntimeObservabilityContextError):
            raise
        raise RuntimeObservabilityContextError("Invalid runtime log context") from error


def audit_context_from_execution_context(
    context: ExecutionContext,
    *,
    trace_context: TraceContext | None = None,
    metadata: Mapping[str, object] | None = None,
    options: RuntimeObservabilityOptions | None = None,
) -> AuditContext:
    execution = _execution(context)
    trace = _resolved_trace(execution, trace_context)
    structural = _structural_attributes(execution, _options(options))
    explicit = _explicit_mapping(metadata, "metadata")
    try:
        return AuditContext(
            correlation_id=execution.correlation_id,
            run_id=execution.run_id,
            tenant_id=execution.tenant_id,
            user_id=execution.user_id,
            trace_id=trace.trace_id if trace is not None else None,
            span_id=trace.span_id if trace is not None else None,
            parent_span_id=trace.parent_span_id if trace is not None else None,
            metadata=_merge_attributes(structural, explicit, "metadata"),
        )
    except Exception as error:
        if isinstance(error, RuntimeObservabilityContextError):
            raise
        raise RuntimeObservabilityContextError("Invalid runtime audit context") from error


@dataclass(frozen=True, slots=True)
class RuntimeObservabilityContext:
    execution_context: ExecutionContext
    log_context: LogContext
    trace_context: TraceContext | None
    audit_context: AuditContext
    _options: RuntimeObservabilityOptions = field(
        default_factory=RuntimeObservabilityOptions, repr=False
    )

    def __post_init__(self) -> None:
        execution = _execution(self.execution_context)
        if type(self.log_context) is not LogContext:
            raise RuntimeObservabilityContextError("Invalid runtime log context")
        if self.trace_context is not None and type(self.trace_context) is not TraceContext:
            raise RuntimeObservabilityContextError("Invalid runtime trace context")
        if type(self.audit_context) is not AuditContext:
            raise RuntimeObservabilityContextError("Invalid runtime audit context")
        if type(self._options) is not RuntimeObservabilityOptions:
            raise RuntimeObservabilityContextError("Invalid runtime observability options")
        log = self.log_context
        audit = self.audit_context
        execution_scope = (
            execution.correlation_id,
            execution.run_id,
            execution.tenant_id,
            execution.user_id,
        )
        if (log.correlation_id, log.run_id, log.tenant_id, log.user_id) != execution_scope:
            raise RuntimeObservabilityContextError("Runtime log scope mismatch")
        if (audit.correlation_id, audit.run_id, audit.tenant_id, audit.user_id) != execution_scope:
            raise RuntimeObservabilityContextError("Runtime audit scope mismatch")
        stored = trace_context_from_execution_context(execution)
        if self.trace_context is None:
            if stored is not None or any(
                item is not None for item in (audit.trace_id, audit.span_id, audit.parent_span_id)
            ):
                raise RuntimeObservabilityContextError("Unexpected runtime trace state")
            if any(name in log.attributes for name in _TRACE_ATTRIBUTE_NAMES):
                raise RuntimeObservabilityContextError("Unexpected runtime log trace state")
        else:
            trace = self.trace_context
            if stored != trace:
                raise RuntimeObservabilityContextError("Runtime serialized trace mismatch")
            if (audit.trace_id, audit.span_id, audit.parent_span_id) != (
                trace.trace_id,
                trace.span_id,
                trace.parent_span_id,
            ):
                raise RuntimeObservabilityContextError("Runtime audit trace mismatch")
            expected_log = {
                "trace_id": str(trace.trace_id),
                "span_id": str(trace.span_id),
                "parent_span_id": str(trace.parent_span_id)
                if trace.parent_span_id is not None
                else None,
                "trace_sampled": trace.sampled,
            }
            if any(log.attributes.get(name) != value for name, value in expected_log.items()):
                raise RuntimeObservabilityContextError("Runtime log trace mismatch")

    @classmethod
    def create(
        cls,
        execution_context: ExecutionContext,
        *,
        trace_context: TraceContext | None = None,
        log_attributes: Mapping[str, object] | None = None,
        audit_metadata: Mapping[str, object] | None = None,
        options: RuntimeObservabilityOptions | None = None,
    ) -> Self:
        execution = _execution(execution_context)
        configured = _options(options)
        stored = trace_context_from_execution_context(execution)
        explicit = _resolved_trace(execution, trace_context)
        if explicit is not None and stored is not None and explicit != stored:
            raise RuntimeObservabilityContextError("Explicit and stored trace contexts differ")
        resolved = explicit if explicit is not None else stored
        updated = execution_context_with_trace_context(execution, resolved)
        return cls(
            updated,
            log_context_from_execution_context(
                updated,
                trace_context=resolved,
                attributes=log_attributes,
                options=configured,
            ),
            resolved,
            audit_context_from_execution_context(
                updated,
                trace_context=resolved,
                metadata=audit_metadata,
                options=configured,
            ),
            configured,
        )

    def with_trace_context(self, trace_context: TraceContext | None) -> Self:
        non_trace_log = {
            key: value
            for key, value in self.log_context.attributes.items()
            if key not in _TRACE_ATTRIBUTE_NAMES and key not in _RUNTIME_ATTRIBUTE_NAMES
        }
        non_trace_audit = {
            key: value
            for key, value in self.audit_context.metadata.items()
            if key not in _RUNTIME_ATTRIBUTE_NAMES
        }
        execution = execution_context_with_trace_context(self.execution_context, trace_context)
        return type(self).create(
            execution,
            trace_context=trace_context,
            log_attributes=non_trace_log,
            audit_metadata=non_trace_audit,
            options=self._options,
        )


def current_runtime_observability_context() -> RuntimeObservabilityContext | None:
    return _CURRENT.get()


def require_runtime_observability_context() -> RuntimeObservabilityContext:
    context = current_runtime_observability_context()
    if context is None:
        raise RuntimeObservabilityContextError("Runtime observability context is not bound")
    return context


class _RuntimeObservabilityBinding(AbstractAsyncContextManager[None]):
    def __init__(self, context: RuntimeObservabilityContext) -> None:
        self._context = context
        self._stack: AsyncExitStack | None = None
        self._token: Token[RuntimeObservabilityContext | None] | None = None

    async def __aenter__(self) -> None:
        if self._stack is not None:
            raise RuntimeObservabilityContextError(
                "Runtime observability binding is already active"
            )
        stack = AsyncExitStack()
        self._stack = stack
        try:
            await stack.enter_async_context(bind_execution_context(self._context.execution_context))
            await stack.enter_async_context(bind_log_context(self._context.log_context))
            if self._context.trace_context is not None:
                await stack.enter_async_context(bind_trace_context(self._context.trace_context))
            await stack.enter_async_context(bind_audit_context(self._context.audit_context))
            self._token = _CURRENT.set(self._context)
        except BaseException:
            self._stack = None
            await stack.aclose()
            raise

    async def __aexit__(self, *_: object) -> None:
        token, stack = self._token, self._stack
        self._token = None
        self._stack = None
        if token is not None:
            _CURRENT.reset(token)
        if stack is not None:
            await stack.aclose()


def bind_runtime_observability_context(
    context: RuntimeObservabilityContext,
) -> AbstractAsyncContextManager[None]:
    if type(context) is not RuntimeObservabilityContext:
        raise RuntimeObservabilityContextError("Invalid runtime observability context")
    return _RuntimeObservabilityBinding(context)


@dataclass(frozen=True, slots=True)
class RuntimeMetricInstruments:
    execution_started: Counter
    execution_completed: Counter
    execution_active: Gauge
    execution_duration: Histogram
    observability_failures: Counter

    def __post_init__(self) -> None:
        expected = _runtime_metric_descriptors()
        values = (
            self.execution_started,
            self.execution_completed,
            self.execution_active,
            self.execution_duration,
            self.observability_failures,
        )
        if any(
            not hasattr(value, "descriptor") or value.descriptor != descriptor
            for value, descriptor in zip(values, expected, strict=True)
        ):
            raise RuntimeObservabilityConfigurationError("Invalid runtime metric instruments")

    @classmethod
    async def register(cls, registry: MetricRegistry) -> Self:
        _validate_registry(registry)
        descriptors = _runtime_metric_descriptors()
        started = await _register_metric(registry, descriptors[0])
        completed = await _register_metric(registry, descriptors[1])
        active = await _register_metric(registry, descriptors[2])
        duration = await _register_metric(registry, descriptors[3])
        failures = await _register_metric(registry, descriptors[4])
        return cls(
            cast(Counter, started),
            cast(Counter, completed),
            cast(Gauge, active),
            cast(Histogram, duration),
            cast(Counter, failures),
        )


def _runtime_metric_descriptors() -> tuple[MetricDescriptor, ...]:
    return (
        MetricDescriptor(
            "alios.runtime.execution.started_total",
            MetricKind.COUNTER,
            "Runtime executions started",
            "executions",
            ("execution_mode",),
        ),
        MetricDescriptor(
            "alios.runtime.execution.completed_total",
            MetricKind.COUNTER,
            "Runtime executions completed",
            "executions",
            ("execution_mode", "outcome"),
        ),
        MetricDescriptor(
            "alios.runtime.execution.active",
            MetricKind.GAUGE,
            "Active runtime executions",
            "executions",
            ("execution_mode",),
        ),
        MetricDescriptor(
            "alios.runtime.execution.duration_seconds",
            MetricKind.HISTOGRAM,
            "Runtime execution duration",
            "s",
            ("execution_mode", "outcome"),
            histogram_boundaries=_DURATION_BOUNDARIES,
        ),
        MetricDescriptor(
            "alios.runtime.observability.failure_total",
            MetricKind.COUNTER,
            "Runtime observability failures",
            "failures",
            ("operation", "signal"),
        ),
    )


def _has_methods(value: object, names: tuple[str, ...]) -> bool:
    return all(callable(getattr(value, name, None)) for name in names)


def _validate_registry(registry: object) -> None:
    if not _has_methods(
        registry,
        (
            "register_counter",
            "register_gauge",
            "register_histogram",
            "get",
            "get_optional",
            "get_counter",
            "get_gauge",
            "get_histogram",
            "close",
        ),
    ):
        raise RuntimeObservabilityConfigurationError("Invalid metric registry")


async def _register_metric(
    registry: MetricRegistry, descriptor: MetricDescriptor
) -> Counter | Gauge | Histogram:
    existing = await registry.get_optional(descriptor.name)
    if existing is None:
        try:
            if descriptor.kind is MetricKind.COUNTER:
                await registry.register_counter(descriptor)
            elif descriptor.kind is MetricKind.GAUGE:
                await registry.register_gauge(descriptor)
            else:
                await registry.register_histogram(descriptor)
        except ResourceConflictError:
            pass
        existing = await registry.get_optional(descriptor.name)
    if existing is None or existing.descriptor != descriptor:
        raise RuntimeObservabilityConfigurationError(
            "Runtime metric descriptor conflict", details={"metric_name": descriptor.name}
        )
    try:
        if descriptor.kind is MetricKind.COUNTER:
            return await registry.get_counter(descriptor.name)
        if descriptor.kind is MetricKind.GAUGE:
            return await registry.get_gauge(descriptor.name)
        return await registry.get_histogram(descriptor.name)
    except Exception as error:
        raise RuntimeObservabilityConfigurationError(
            "Runtime metric kind conflict", details={"metric_name": descriptor.name}
        ) from error


@dataclass(frozen=True, slots=True)
class RuntimeObservabilityStatus:
    started: bool
    closed: bool
    logging_available: bool
    tracing_available: bool
    metrics_available: bool
    audit_available: bool
    metrics_registered: bool
    failure_count: int
    created_at: datetime
    started_at: datetime | None = None
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        flags = (
            self.started,
            self.closed,
            self.logging_available,
            self.tracing_available,
            self.metrics_available,
            self.audit_available,
            self.metrics_registered,
        )
        if any(type(value) is not bool for value in flags):
            raise RuntimeObservabilityLifecycleError("Invalid runtime observability status")
        if type(self.failure_count) is not int or self.failure_count < 0:
            raise RuntimeObservabilityLifecycleError("Invalid runtime observability failure count")
        _status_time(self.created_at, "created_at")
        if self.started_at is not None:
            _status_time(self.started_at, "started_at")
            if self.started_at < self.created_at:
                raise RuntimeObservabilityLifecycleError("Invalid runtime start time")
        if self.closed_at is not None:
            _status_time(self.closed_at, "closed_at")
            if self.closed_at < self.created_at:
                raise RuntimeObservabilityLifecycleError("Invalid runtime close time")
        if self.closed != (self.closed_at is not None):
            raise RuntimeObservabilityLifecycleError("Inconsistent runtime closed status")
        if self.started != (self.started_at is not None):
            raise RuntimeObservabilityLifecycleError("Inconsistent runtime started status")
        if self.metrics_registered and (not self.metrics_available or not self.started):
            raise RuntimeObservabilityLifecycleError("Inconsistent runtime metric status")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "started": self.started,
            "closed": self.closed,
            "logging_available": self.logging_available,
            "tracing_available": self.tracing_available,
            "metrics_available": self.metrics_available,
            "audit_available": self.audit_available,
            "metrics_registered": self.metrics_registered,
            "failure_count": self.failure_count,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at is not None else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at is not None else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            names = (
                "started",
                "closed",
                "logging_available",
                "tracing_available",
                "metrics_available",
                "audit_available",
                "metrics_registered",
            )
            if (
                not isinstance(value, Mapping)
                or any(type(value.get(name)) is not bool for name in names)
                or type(value.get("failure_count")) is not int
                or not isinstance(value.get("created_at"), str)
                or value.get("started_at") is not None
                and not isinstance(value.get("started_at"), str)
                or value.get("closed_at") is not None
                and not isinstance(value.get("closed_at"), str)
            ):
                raise ValueError
            started_at, closed_at = value.get("started_at"), value.get("closed_at")
            return cls(
                cast(bool, value["started"]),
                cast(bool, value["closed"]),
                cast(bool, value["logging_available"]),
                cast(bool, value["tracing_available"]),
                cast(bool, value["metrics_available"]),
                cast(bool, value["audit_available"]),
                cast(bool, value["metrics_registered"]),
                cast(int, value["failure_count"]),
                datetime.fromisoformat(cast(str, value["created_at"])),
                datetime.fromisoformat(started_at) if isinstance(started_at, str) else None,
                datetime.fromisoformat(closed_at) if isinstance(closed_at, str) else None,
            )
        except (KeyError, TypeError, ValueError, RuntimeObservabilityLifecycleError) as error:
            raise RuntimeObservabilityLifecycleError(
                "Invalid serialized runtime observability status"
            ) from error


def _status_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeObservabilityLifecycleError(
            "Invalid runtime observability timestamp", details={"field_name": field_name}
        )
    return value


class RuntimeObservability(Protocol):
    @property
    def logger(self) -> StructuredLogger | None: ...

    @property
    def tracer(self) -> Tracer | None: ...

    @property
    def metric_registry(self) -> MetricRegistry | None: ...

    @property
    def audit_ledger(self) -> AuditLedger | None: ...

    @property
    def metrics(self) -> RuntimeMetricInstruments | None: ...

    @property
    def options(self) -> RuntimeObservabilityOptions: ...

    async def start(self) -> None: ...

    def create_context(
        self,
        execution_context: ExecutionContext,
        *,
        trace_context: TraceContext | None = None,
        log_attributes: Mapping[str, object] | None = None,
        audit_metadata: Mapping[str, object] | None = None,
    ) -> RuntimeObservabilityContext: ...

    def bind(self, context: RuntimeObservabilityContext) -> AbstractAsyncContextManager[None]: ...

    async def status(self) -> RuntimeObservabilityStatus: ...

    async def close(self) -> None: ...


class _AsyncCloseable(Protocol):
    async def close(self) -> None: ...


class DefaultRuntimeObservability:
    def __init__(
        self,
        *,
        logger: StructuredLogger | None = None,
        tracer: Tracer | None = None,
        metric_registry: MetricRegistry | None = None,
        audit_ledger: AuditLedger | None = None,
        options: RuntimeObservabilityOptions | None = None,
        owns_tracer: bool = False,
        owns_metric_registry: bool = False,
        owns_audit_ledger: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if logger is not None and type(logger) is not StructuredLogger:
            raise RuntimeObservabilityConfigurationError("Invalid structured logger")
        if tracer is not None and not _has_methods(
            tracer, ("start_span", "start_as_current_span", "status", "close")
        ):
            raise RuntimeObservabilityConfigurationError("Invalid tracer")
        if metric_registry is not None:
            _validate_registry(metric_registry)
        if audit_ledger is not None and not _has_methods(
            audit_ledger,
            (
                "append",
                "get",
                "entries",
                "list",
                "count",
                "snapshot",
                "verify",
                "status",
                "close",
            ),
        ):
            raise RuntimeObservabilityConfigurationError("Invalid audit ledger")
        if any(
            type(value) is not bool
            for value in (owns_tracer, owns_metric_registry, owns_audit_ledger)
        ) or not callable(clock):
            raise RuntimeObservabilityConfigurationError(
                "Invalid runtime observability configuration"
            )
        configured_options = options if options is not None else RuntimeObservabilityOptions()
        if type(configured_options) is not RuntimeObservabilityOptions:
            raise RuntimeObservabilityConfigurationError("Invalid runtime observability options")
        try:
            created_at = _status_time(clock(), "created_at")
        except RuntimeObservabilityLifecycleError as error:
            raise RuntimeObservabilityConfigurationError(
                "Invalid runtime observability clock"
            ) from error
        except Exception as error:
            raise RuntimeObservabilityConfigurationError(
                "Runtime observability clock failed"
            ) from error
        self._logger = logger
        self._tracer = tracer
        self._metric_registry = metric_registry
        self._audit_ledger = audit_ledger
        self._options = configured_options
        self._owns_tracer = owns_tracer
        self._owns_metric_registry = owns_metric_registry
        self._owns_audit_ledger = owns_audit_ledger
        self._clock = clock
        self._created_at = created_at
        self._started_at: datetime | None = None
        self._closed_at: datetime | None = None
        self._metrics: RuntimeMetricInstruments | None = None
        self._failure_count = 0
        self._closing = False
        self._closed = False
        self._start_event: asyncio.Event | None = None
        self._close_event: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    @property
    def logger(self) -> StructuredLogger | None:
        return self._logger

    @property
    def tracer(self) -> Tracer | None:
        return self._tracer

    @property
    def metric_registry(self) -> MetricRegistry | None:
        return self._metric_registry

    @property
    def audit_ledger(self) -> AuditLedger | None:
        return self._audit_ledger

    @property
    def metrics(self) -> RuntimeMetricInstruments | None:
        return self._metrics

    @property
    def options(self) -> RuntimeObservabilityOptions:
        return self._options

    async def start(self) -> None:
        while True:
            async with self._lock:
                if self._closing or self._closed:
                    raise RuntimeObservabilityClosedError("Runtime observability is closed")
                if self._started_at is not None:
                    return
                if self._start_event is None:
                    event = asyncio.Event()
                    self._start_event = event
                    owner = True
                else:
                    event = self._start_event
                    owner = False
            if owner:
                break
            await event.wait()
        try:
            metrics = (
                await RuntimeMetricInstruments.register(self._metric_registry)
                if self._metric_registry is not None
                else None
            )
            started_at = _status_time(self._clock(), "started_at")
            if started_at < self._created_at:
                raise RuntimeObservabilityLifecycleError(
                    "Runtime observability start precedes creation"
                )
        except Exception as error:
            async with self._lock:
                self._failure_count += 1
                closed = self._closing or self._closed
                if (
                    not closed
                    and self._options.failure_mode is RuntimeObservabilityFailureMode.FAIL_OPEN
                ):
                    self._started_at = self._created_at
                    self._metrics = None
                self._start_event = None
                event.set()
            if closed:
                raise RuntimeObservabilityClosedError(
                    "Runtime observability closed during startup"
                ) from error
            if self._options.failure_mode is RuntimeObservabilityFailureMode.FAIL_CLOSED:
                raise RuntimeObservabilityLifecycleError(
                    "Runtime observability startup failed"
                ) from error
            return
        async with self._lock:
            if self._closing or self._closed:
                self._start_event = None
                event.set()
                raise RuntimeObservabilityClosedError("Runtime observability closed during startup")
            self._metrics = metrics
            self._started_at = started_at
            self._start_event = None
            event.set()

    def create_context(
        self,
        execution_context: ExecutionContext,
        *,
        trace_context: TraceContext | None = None,
        log_attributes: Mapping[str, object] | None = None,
        audit_metadata: Mapping[str, object] | None = None,
    ) -> RuntimeObservabilityContext:
        if self._closing or self._closed:
            raise RuntimeObservabilityClosedError("Runtime observability is closed")
        return RuntimeObservabilityContext.create(
            execution_context,
            trace_context=trace_context,
            log_attributes=log_attributes,
            audit_metadata=audit_metadata,
            options=self._options,
        )

    def bind(self, context: RuntimeObservabilityContext) -> AbstractAsyncContextManager[None]:
        if self._closing or self._closed:
            raise RuntimeObservabilityClosedError("Runtime observability is closed")
        if self._started_at is None:
            raise RuntimeObservabilityLifecycleError("Runtime observability is not started")
        return bind_runtime_observability_context(context)

    async def status(self) -> RuntimeObservabilityStatus:
        async with self._lock:
            return RuntimeObservabilityStatus(
                self._started_at is not None,
                self._closed,
                self._logger is not None,
                self._tracer is not None,
                self._metric_registry is not None,
                self._audit_ledger is not None,
                self._metrics is not None,
                self._failure_count,
                self._created_at,
                self._started_at,
                self._closed_at,
            )

    async def close(self) -> None:
        async with self._lock:
            if self._close_event is not None:
                event = self._close_event
                owner = False
            elif self._closed:
                return
            else:
                self._closing = True
                event = asyncio.Event()
                self._close_event = event
                owner = True
                start_event = self._start_event
                resources: tuple[object, ...] = tuple(
                    resource
                    for resource, owned in (
                        (self._tracer, self._owns_tracer),
                        (self._metric_registry, self._owns_metric_registry),
                        (self._audit_ledger, self._owns_audit_ledger),
                    )
                    if resource is not None and owned
                )
        if not owner:
            await event.wait()
            return
        if start_event is not None:
            await start_event.wait()
        failures: list[Exception] = []
        for resource in resources:
            try:
                await cast(_AsyncCloseable, resource).close()
            except Exception as error:
                failures.append(error)
        try:
            closed_at = _status_time(self._clock(), "closed_at")
            earliest_close = self._started_at or self._created_at
            if closed_at < earliest_close:
                raise RuntimeObservabilityLifecycleError(
                    "Runtime observability close precedes startup"
                )
        except Exception as error:
            failures.append(error)
            closed_at = self._started_at or self._created_at
        async with self._lock:
            self._failure_count += len(failures)
            self._closed_at = closed_at
            self._closing = False
            self._closed = True
            self._close_event = None
            event.set()
        if failures and self._options.failure_mode is RuntimeObservabilityFailureMode.FAIL_CLOSED:
            raise RuntimeObservabilityLifecycleError(
                "Runtime observability close failed"
            ) from failures[0]
