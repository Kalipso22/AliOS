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


class ConfigurationError(AliOSError): code = "configuration_error"
class ValidationError(AliOSError): code = "validation_error"
class DependencyError(AliOSError): code = "dependency_error"
class LifecycleError(AliOSError): code = "lifecycle_error"
class RuntimeError(AliOSError): code = "runtime_error"
class RunNotFoundError(AliOSError): code = "run_not_found"
class InvalidStateTransitionError(AliOSError): code = "invalid_state_transition"
class RecoveryError(AliOSError): code = "recovery_error"
class CheckpointError(AliOSError): code = "checkpoint_error"
class PolicyError(AliOSError): code = "policy_error"
class PermissionDeniedError(AliOSError): code = "permission_denied"
class EventBusError(AliOSError): code = "event_bus_error"
class EventHandlerError(AliOSError): code = "event_handler_error"
class ResourceConflictError(AliOSError): code = "resource_conflict"
class ResourceNotFoundError(AliOSError): code = "resource_not_found"
class TimeoutError(AliOSError): code = "timeout"
class RateLimitError(AliOSError): code = "rate_limited"
class ExternalServiceError(AliOSError): code = "external_service_error"
class SerializationError(AliOSError): code = "serialization_error"
