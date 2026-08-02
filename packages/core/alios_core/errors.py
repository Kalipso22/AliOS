"""Safe, stable AliOS exception hierarchy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from .types import Metadata


@dataclass(eq=False)
class AliOSError(Exception):
    message: str
    details: Metadata = field(default_factory=dict)
    cause: Exception | None = None
    code: ClassVar[str] = "alios_error"

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.cause is not None:
            self.__cause__ = self.cause

    def to_dict(self) -> Metadata:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigurationError(AliOSError):
    code = "configuration_error"


class ValidationError(AliOSError):
    code = "validation_error"


class DependencyError(AliOSError):
    code = "dependency_error"


class LifecycleError(AliOSError):
    code = "lifecycle_error"


class RuntimeError(AliOSError):
    code = "runtime_error"


class RunNotFoundError(AliOSError):
    code = "run_not_found"


class InvalidStateTransitionError(AliOSError):
    code = "invalid_state_transition"


class RecoveryError(AliOSError):
    code = "recovery_error"


class RecoveryPlanStaleError(RecoveryError):
    code = "recovery_plan_stale"


class RecoveryStartedPublicationError(RecoveryError):
    code = "recovery_started_publication_failed"


class RecoveryCompletedPublicationError(RecoveryError):
    code = "recovery_completed_publication_failed"


class CheckpointError(AliOSError):
    code = "checkpoint_error"


class PolicyError(AliOSError):
    code = "policy_error"


class PermissionDeniedError(AliOSError):
    code = "permission_denied"


class EventBusError(AliOSError):
    code = "event_bus_error"


class EventHandlerError(AliOSError):
    code = "event_handler_error"


class ResourceConflictError(AliOSError):
    code = "resource_conflict"


class ResourceNotFoundError(AliOSError):
    code = "resource_not_found"


class TimeoutError(AliOSError):
    code = "timeout"


class RateLimitError(AliOSError):
    code = "rate_limited"


class ExternalServiceError(AliOSError):
    code = "external_service_error"


class SerializationError(AliOSError):
    code = "serialization_error"


class ObservabilityError(AliOSError):
    code = "observability_error"


class LoggingError(ObservabilityError):
    code = "logging_error"


class LogSerializationError(LoggingError):
    code = "log_serialization_error"


class LogSinkError(LoggingError):
    code = "log_sink_error"


class MetricsError(ObservabilityError):
    code = "metrics_error"


class MetricDefinitionError(MetricsError):
    code = "metric_definition_error"


class MetricValueError(MetricsError):
    code = "metric_value_error"


class MetricCardinalityError(MetricsError):
    code = "metric_cardinality_error"


class MetricRegistryClosedError(MetricsError):
    code = "metric_registry_closed"
