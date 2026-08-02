"""Stable AliOS core contracts."""

from .config import (
    AliOSConfig,
    ConfigurationLoader,
    ConfigurationSource,
    Environment,
    SecretReference,
    SensitiveValue,
    SettingsSnapshot,
    SourceMetadata,
)
from .container import Container, ServiceKey, ServiceLifetime
from .events import AsyncEventBus, BaseEvent, DispatchMode, Event, EventBus, PolicyEvaluated
from .ids import CorrelationId, LogRecordId, RunId, SpanId, TraceId
from .lifecycle import LifecycleHealth, LifecycleManager, LifecycleState, ManagedLifecycleComponent
from .policy import (
    InMemoryPolicyEvaluator,
    PolicyAction,
    PolicyCondition,
    PolicyContext,
    PolicyResource,
    PolicyResult,
    PolicyRule,
    PolicySubject,
)
from .types import ExecutionMode, RunStatus

__all__ = [
    "AliOSConfig",
    "AsyncEventBus",
    "BaseEvent",
    "ConfigurationLoader",
    "ConfigurationSource",
    "Container",
    "CorrelationId",
    "DispatchMode",
    "Environment",
    "Event",
    "EventBus",
    "ExecutionMode",
    "InMemoryPolicyEvaluator",
    "LifecycleHealth",
    "LifecycleManager",
    "LifecycleState",
    "LogRecordId",
    "ManagedLifecycleComponent",
    "PolicyAction",
    "PolicyCondition",
    "PolicyContext",
    "PolicyEvaluated",
    "PolicyResource",
    "PolicyResult",
    "PolicyRule",
    "PolicySubject",
    "RunId",
    "RunStatus",
    "SecretReference",
    "SensitiveValue",
    "ServiceKey",
    "ServiceLifetime",
    "SettingsSnapshot",
    "SourceMetadata",
    "SpanId",
    "TraceId",
]
