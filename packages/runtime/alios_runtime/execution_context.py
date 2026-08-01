"""Immutable, context-local execution context contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from alios_core.errors import AliOSError, SerializationError, TimeoutError, ValidationError
from alios_core.ids import CorrelationId, RunId, TaskId, TenantId, UserId
from alios_core.types import ExecutionMode, utc_now

_current: ContextVar[ExecutionContext | None] = ContextVar("alios_execution_context", default=None)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _merge(lower: Mapping[str, Any], higher: Mapping[str, Any]) -> dict[str, Any]:
    result = {str(key): _freeze(value) for key, value in lower.items()}
    for key, value in higher.items():
        name = str(key)
        if isinstance(value, Mapping) and isinstance(result.get(name), Mapping):
            result[name] = _merge(result[name], value)
        else:
            result[name] = _freeze(value)
    return result


class CancellationError(AliOSError):
    code = "execution_cancelled"


class CancellationToken:
    """One-way, task-safe cancellation notification primitive."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str = "cancelled") -> bool:
        if self._event.is_set():
            return False
        self._reason = reason
        self._event.set()
        return True

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CancellationError(
                "Execution was cancelled", {"reason": self._reason or "cancelled"}
            )


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    run_id: RunId
    correlation_id: CorrelationId
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    parent_run_id: RunId | None = None
    current_task_id: TaskId | None = None
    execution_mode: ExecutionMode = ExecutionMode.INTERACTIVE
    deadline: datetime | None = None
    cancellation_token: CancellationToken = field(
        default_factory=CancellationToken, compare=False, repr=False
    )
    metadata: Mapping[str, Any] = field(default_factory=dict)
    policy_context: Mapping[str, Any] = field(default_factory=dict)
    trace_context: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    attempt_number: int = 1

    def __post_init__(self) -> None:
        for value, label in ((self.deadline, "deadline"), (self.created_at, "created_at")):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValidationError(f"{label} must be timezone-aware")
        if self.attempt_number < 1:
            raise ValidationError("attempt_number must be positive")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "policy_context", _freeze(self.policy_context))
        object.__setattr__(self, "trace_context", _freeze(self.trace_context))

    @property
    def mode(self) -> ExecutionMode:
        return self.execution_mode

    @property
    def task_id(self) -> TaskId | None:
        return self.current_task_id

    @property
    def attempt(self) -> int:
        return self.attempt_number

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId | None = None,
        correlation_id: CorrelationId | None = None,
        **kwargs: Any,
    ) -> ExecutionContext:
        return cls(run_id or RunId(), correlation_id or CorrelationId(), **kwargs)

    def with_metadata(self, metadata: Mapping[str, Any]) -> ExecutionContext:
        return replace(self, metadata=_freeze(metadata))

    def merge_metadata(self, metadata: Mapping[str, Any]) -> ExecutionContext:
        return replace(self, metadata=_merge(self.metadata, metadata))

    def with_deadline(self, deadline: datetime | None) -> ExecutionContext:
        return replace(self, deadline=deadline)

    def with_policy_context(self, value: Mapping[str, Any]) -> ExecutionContext:
        return replace(self, policy_context=_freeze(value))

    def with_trace_context(self, value: Mapping[str, Any]) -> ExecutionContext:
        return replace(self, trace_context=_freeze(value))

    def with_current_task(self, task_id: TaskId | None) -> ExecutionContext:
        return replace(self, current_task_id=task_id)

    def with_execution_mode(self, mode: ExecutionMode) -> ExecutionContext:
        return replace(self, execution_mode=mode)

    def with_tenant(self, tenant_id: TenantId | None) -> ExecutionContext:
        return replace(self, tenant_id=tenant_id)

    def with_user(self, user_id: UserId | None) -> ExecutionContext:
        return replace(self, user_id=user_id)

    def create_child(
        self,
        *,
        run_id: RunId | None = None,
        current_task_id: TaskId | None = None,
        inherit_cancellation: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionContext:
        return ExecutionContext.create(
            run_id=run_id,
            correlation_id=self.correlation_id,
            parent_run_id=self.run_id,
            current_task_id=current_task_id,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            execution_mode=self.execution_mode,
            deadline=self.deadline,
            cancellation_token=self.cancellation_token
            if inherit_cancellation
            else CancellationToken(),
            metadata=_merge(self.metadata, metadata or {}),
            policy_context=self.policy_context,
            trace_context=self.trace_context,
        )

    def create_retry(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
        deadline: datetime | None = None,
        inherit_cancellation: bool = True,
    ) -> ExecutionContext:
        return replace(
            self,
            attempt_number=self.attempt_number + 1,
            deadline=deadline if deadline is not None else self.deadline,
            metadata=_merge(self.metadata, metadata or {}),
            cancellation_token=self.cancellation_token
            if inherit_cancellation
            else CancellationToken(),
        )

    @property
    def has_deadline(self) -> bool:
        return self.deadline is not None

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.deadline is not None and (now or utc_now()) >= self.deadline

    def remaining_time(self, now: datetime | None = None) -> timedelta | None:
        if self.deadline is None:
            return None
        return max(self.deadline - (now or utc_now()), timedelta())

    def raise_if_expired(self, now: datetime | None = None) -> None:
        if self.is_expired(now):
            raise TimeoutError("Execution deadline has expired", {"run_id": str(self.run_id)})

    def effective_timeout(
        self, requested_timeout: timedelta | None, now: datetime | None = None
    ) -> timedelta | None:
        if requested_timeout is not None and requested_timeout < timedelta():
            raise ValidationError("requested timeout cannot be negative")
        remaining = self.remaining_time(now)
        return (
            remaining
            if requested_timeout is None
            else requested_timeout
            if remaining is None
            else min(requested_timeout, remaining)
        )

    @asynccontextmanager
    async def bind(self) -> AsyncIterator[None]:
        token: Token[ExecutionContext | None] = _current.set(self)
        try:
            yield
        finally:
            _current.reset(token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "correlation_id": str(self.correlation_id),
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "parent_run_id": str(self.parent_run_id) if self.parent_run_id else None,
            "current_task_id": str(self.current_task_id) if self.current_task_id else None,
            "execution_mode": self.execution_mode.value,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "metadata": dict(self.metadata),
            "policy_context": dict(self.policy_context),
            "trace_context": dict(self.trace_context),
            "created_at": self.created_at.isoformat(),
            "attempt_number": self.attempt_number,
            "cancelled": self.cancellation_token.is_cancelled,
            "cancellation_reason": self.cancellation_token.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionContext:
        try:
            context = cls.create(
                run_id=RunId(data["run_id"]),
                correlation_id=CorrelationId(data["correlation_id"]),
                tenant_id=TenantId(data["tenant_id"]) if data.get("tenant_id") else None,
                user_id=UserId(data["user_id"]) if data.get("user_id") else None,
                parent_run_id=RunId(data["parent_run_id"]) if data.get("parent_run_id") else None,
                current_task_id=TaskId(data["current_task_id"])
                if data.get("current_task_id")
                else None,
                execution_mode=ExecutionMode(data.get("execution_mode", ExecutionMode.INTERACTIVE)),
                deadline=datetime.fromisoformat(data["deadline"]) if data.get("deadline") else None,
                metadata=data.get("metadata", {}),
                policy_context=data.get("policy_context", {}),
                trace_context=data.get("trace_context", {}),
                created_at=datetime.fromisoformat(data["created_at"]),
                attempt_number=int(data.get("attempt_number", 1)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SerializationError("Invalid execution context", cause=error) from error
        if data.get("cancelled"):
            context.cancellation_token.cancel(str(data.get("cancellation_reason") or "cancelled"))
        return context


def current_execution_context() -> ExecutionContext | None:
    return _current.get()


def require_execution_context() -> ExecutionContext:
    context = current_execution_context()
    if context is None:
        raise ValidationError("No execution context is bound")
    return context


@asynccontextmanager
async def bind_execution_context(context: ExecutionContext) -> AsyncIterator[None]:
    async with context.bind():
        yield
