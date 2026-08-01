"""Immutable, context-local runtime execution context."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from alios_core.ids import CorrelationId, RunId, TaskId, TenantId, UserId
from alios_core.types import ExecutionMode, Metadata, utc_now

_current: ContextVar[ExecutionContext | None] = ContextVar("alios_execution_context", default=None)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    run_id: RunId
    correlation_id: CorrelationId
    mode: ExecutionMode = ExecutionMode.INTERACTIVE
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    parent_run_id: RunId | None = None
    task_id: TaskId | None = None
    deadline: datetime | None = None
    cancelled: bool = False
    metadata: Metadata = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    attempt: int = 1

    def with_metadata(self, metadata: Metadata) -> ExecutionContext:
        """Return a context with replacement metadata."""
        return replace(self, metadata=metadata)

    def cancel(self) -> ExecutionContext:
        """Return a cancelled immutable context."""
        return replace(self, cancelled=True)

    def child(self, run_id: RunId) -> ExecutionContext:
        return replace(self, run_id=run_id, parent_run_id=self.run_id, attempt=1)

    def retry(self) -> ExecutionContext:
        return replace(self, attempt=self.attempt + 1)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.deadline is not None and (now or utc_now()) >= self.deadline

    def remaining(self, now: datetime | None = None) -> timedelta | None:
        return None if self.deadline is None else self.deadline - (now or utc_now())

    @asynccontextmanager
    async def bind(self) -> AsyncIterator[None]:
        token: Token[ExecutionContext | None] = _current.set(self)
        try:
            yield
        finally:
            _current.reset(token)


def current_execution_context() -> ExecutionContext | None:
    return _current.get()
