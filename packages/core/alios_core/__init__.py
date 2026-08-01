"""Stable AliOS core contracts."""

from .config import AliOSConfig
from .container import Container, ServiceKey, ServiceLifetime
from .events import Event, EventBus
from .ids import CorrelationId, RunId
from .lifecycle import LifecycleHealth, LifecycleManager, LifecycleState, ManagedLifecycleComponent
from .types import ExecutionMode, RunStatus

__all__ = [
    "AliOSConfig",
    "Container",
    "Container",
    "CorrelationId",
    "Event",
    "EventBus",
    "ExecutionMode",
    "LifecycleHealth",
    "LifecycleManager",
    "LifecycleState",
    "ManagedLifecycleComponent",
    "RunId",
    "RunStatus",
    "ServiceKey",
    "ServiceLifetime",
]
