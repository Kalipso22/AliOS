"""Immutable domain primitives shared by AliOS subsystems."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, TypeAlias, TypeVar

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
Metadata: TypeAlias = dict[str, JsonValue]
TSerializable = TypeVar("TSerializable", bound="Serializable")


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class Serializable(Protocol):
    """Object with a safe JSON-compatible representation."""

    def to_dict(self) -> Metadata: ...


class AsyncCloseable(Protocol):
    """Resource with asynchronous close semantics."""

    async def aclose(self) -> None: ...


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ComponentStatus(StrEnum):
    CREATED = "created"
    INITIALIZED = "initialized"
    STARTED = "started"
    STOPPED = "stopped"
    CLOSED = "closed"
    FAILED = "failed"


class RunStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    INITIALIZING = "initializing"
    RUNNING = "running"
    WAITING = "waiting"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RETRYING = "retrying"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class EventPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PermissionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class RetryStrategy(StrEnum):
    NONE = "none"
    IMMEDIATE = "immediate"
    EXPONENTIAL_BACKOFF = "exponential_backoff"


class ExecutionMode(StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    SCHEDULED = "scheduled"


@dataclass(frozen=True, slots=True)
class HealthResult:
    status: HealthStatus
    checked_at: datetime
    details: Metadata

    def to_dict(self) -> Metadata:
        return {**asdict(self), "status": self.status, "checked_at": self.checked_at.isoformat()}
