"""Immutable run records, repository contracts, and lifecycle manager."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from alios_core.errors import AliOSError, ResourceConflictError, RunNotFoundError, ValidationError
from alios_core.events import BaseEvent, RunCompleted, RunCreated, RunFailed
from alios_core.ids import CorrelationId, RunId, TenantId, UserId
from alios_core.types import ExecutionMode, RunOutcome, RunStatus, utc_now

from .state_machine import TERMINAL, RunStateMachine


def _freeze(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType({str(k): v for k, v in (value or {}).items()})


@dataclass(frozen=True, slots=True)
class RunFailure:
    error_code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utc_now)
    retryable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze(self.details))


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: RunId
    status: RunStatus = RunStatus.CREATED
    version: int = 0
    correlation_id: CorrelationId = field(default_factory=CorrelationId)
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    parent_run_id: RunId | None = None
    root_run_id: RunId | None = None
    idempotency_key: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.INTERACTIVE
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    deadline: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    failure: RunFailure | None = None
    outcome: RunOutcome | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "root_run_id", self.root_run_id or self.run_id)


@dataclass(frozen=True, slots=True)
class RunFilter:
    statuses: frozenset[RunStatus] | None = None
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    parent_run_id: RunId | None = None
    root_run_id: RunId | None = None
    correlation_id: CorrelationId | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    has_deadline: bool | None = None
    terminal: bool | None = None
    limit: int | None = None
    offset: int = 0

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 0 or self.offset < 0:
            raise ValidationError("Pagination values cannot be negative")


class RunRepository(Protocol):
    async def create(self, run: RunRecord) -> RunRecord: ...
    async def get(self, run_id: RunId) -> RunRecord: ...
    async def get_optional(self, run_id: RunId) -> RunRecord | None: ...
    async def update(self, run: RunRecord, *, expected_version: int | None = None) -> RunRecord: ...
    async def list(self, filters: RunFilter | None = None) -> tuple[RunRecord, ...]: ...
    async def find_by_idempotency_key(
        self, key: str, tenant_id: TenantId | None
    ) -> RunRecord | None: ...
    async def list_children(self, parent_run_id: RunId) -> tuple[RunRecord, ...]: ...
    async def delete(self, run_id: RunId) -> None: ...
    async def count(self, filters: RunFilter | None = None) -> int: ...


class InMemoryRunRepository:
    def __init__(self) -> None:
        self._runs: dict[RunId, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: RunRecord) -> RunRecord:
        async with self._lock:
            if run.run_id in self._runs:
                raise ResourceConflictError("Run already exists")
            self._runs[run.run_id] = run
            return run

    async def get(self, run_id: RunId) -> RunRecord:
        value = await self.get_optional(run_id)
        if value is None:
            raise RunNotFoundError("Run was not found", {"run_id": str(run_id)})
        return value

    async def get_optional(self, run_id: RunId) -> RunRecord | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def update(self, run: RunRecord, *, expected_version: int | None = None) -> RunRecord:
        async with self._lock:
            old = self._runs.get(run.run_id)
            if old is None:
                raise RunNotFoundError("Run was not found")
            if expected_version is not None and old.version != expected_version:
                raise ResourceConflictError("Run version conflict")
            self._runs[run.run_id] = run
            return run

    async def list(self, filters: RunFilter | None = None) -> tuple[RunRecord, ...]:
        filters = filters or RunFilter()
        async with self._lock:
            values = tuple(self._runs.values())

        def matches(run: RunRecord) -> bool:
            return (
                (filters.statuses is None or run.status in filters.statuses)
                and (filters.tenant_id is None or run.tenant_id == filters.tenant_id)
                and (filters.user_id is None or run.user_id == filters.user_id)
                and (filters.parent_run_id is None or run.parent_run_id == filters.parent_run_id)
                and (filters.root_run_id is None or run.root_run_id == filters.root_run_id)
                and (filters.correlation_id is None or run.correlation_id == filters.correlation_id)
                and (
                    filters.has_deadline is None
                    or (run.deadline is not None) == filters.has_deadline
                )
                and (filters.terminal is None or (run.status in TERMINAL) == filters.terminal)
                and (filters.created_after is None or run.created_at >= filters.created_after)
                and (filters.created_before is None or run.created_at <= filters.created_before)
                and (filters.updated_after is None or run.updated_at >= filters.updated_after)
                and (filters.updated_before is None or run.updated_at <= filters.updated_before)
            )

        output = tuple(
            sorted(
                (run for run in values if matches(run)),
                key=lambda run: (run.created_at, str(run.run_id)),
            )
        )
        return output[
            filters.offset : None if filters.limit is None else filters.offset + filters.limit
        ]

    async def find_by_idempotency_key(
        self, key: str, tenant_id: TenantId | None
    ) -> RunRecord | None:
        for run in await self.list():
            if run.idempotency_key == key and run.tenant_id == tenant_id:
                return run
        return None

    async def list_children(self, parent_run_id: RunId) -> tuple[RunRecord, ...]:
        return await self.list(RunFilter(parent_run_id=parent_run_id))

    async def delete(self, run_id: RunId) -> None:
        async with self._lock:
            if run_id not in self._runs:
                raise RunNotFoundError("Run was not found")
            del self._runs[run_id]

    async def count(self, filters: RunFilter | None = None) -> int:
        return len(await self.list(filters))


class RunManager:
    def __init__(
        self,
        repository: RunRepository | None = None,
        event_publisher: Callable[[BaseEvent], Awaitable[object]] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository or InMemoryRunRepository()
        self._publisher = event_publisher
        self._clock = clock
        self._machines: dict[RunId, RunStateMachine] = {}
        self._lock = asyncio.Lock()

    async def create_run(
        self,
        *,
        run_id: RunId | None = None,
        correlation_id: CorrelationId | None = None,
        tenant_id: TenantId | None = None,
        user_id: UserId | None = None,
        parent_run_id: RunId | None = None,
        idempotency_key: str | None = None,
        execution_mode: ExecutionMode = ExecutionMode.INTERACTIVE,
        deadline: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
        allow_terminal_parent: bool = False,
    ) -> RunRecord:
        if deadline and deadline.tzinfo is None:
            raise ValidationError("deadline must be timezone-aware")
        if deadline and deadline <= self._clock():
            raise ValidationError("deadline has already expired")
        if idempotency_key is not None and (
            not idempotency_key.strip() or len(idempotency_key) > 256
        ):
            raise ValidationError("Invalid idempotency key")
        async with self._lock:
            if idempotency_key:
                existing = await self.repository.find_by_idempotency_key(idempotency_key, tenant_id)
                if existing:
                    return existing
            parent = await self.repository.get(parent_run_id) if parent_run_id else None
            if parent and parent.status in TERMINAL and not allow_terminal_parent:
                raise ValidationError("Parent run is terminal")
            if parent and parent.tenant_id and tenant_id and parent.tenant_id != tenant_id:
                raise ValidationError("Parent tenant mismatch")
            identifier = run_id or RunId()
            now = self._clock()
            run = RunRecord(
                identifier,
                correlation_id=correlation_id or CorrelationId(),
                tenant_id=tenant_id,
                user_id=user_id,
                parent_run_id=parent_run_id,
                root_run_id=parent.root_run_id if parent else identifier,
                idempotency_key=idempotency_key,
                execution_mode=execution_mode,
                created_at=now,
                updated_at=now,
                deadline=deadline,
                metadata=metadata or {},
            )
            await self.repository.create(run)
            self._machines[identifier] = RunStateMachine(
                identifier, event_publisher=self._publisher
            )
        if self._publisher:
            await self._publisher(
                RunCreated(correlation_id=run.correlation_id, run_id=str(run.run_id))
            )
        return run

    async def get_run(self, run_id: RunId) -> RunRecord:
        return await self.repository.get(run_id)

    async def get_optional_run(self, run_id: RunId) -> RunRecord | None:
        return await self.repository.get_optional(run_id)

    async def list_runs(self, filters: RunFilter | None = None) -> tuple[RunRecord, ...]:
        return await self.repository.list(filters)

    async def list_child_runs(self, parent_run_id: RunId) -> tuple[RunRecord, ...]:
        return await self.repository.list_children(parent_run_id)

    async def _transition(self, run_id: RunId, status: RunStatus, **kwargs: Any) -> RunRecord:
        run = await self.repository.get(run_id)
        machine = self._machines.setdefault(
            run_id,
            RunStateMachine(
                run_id,
                status=run.status,
                version=run.version,
                event_publisher=self._publisher,
            ),
        )
        await machine.transition(
            status, expected_version=run.version, correlation_id=run.correlation_id, **kwargs
        )
        now = self._clock()
        outcome = {
            RunStatus.SUCCEEDED: RunOutcome.SUCCEEDED,
            RunStatus.FAILED: RunOutcome.FAILED,
            RunStatus.CANCELLED: RunOutcome.CANCELLED,
            RunStatus.TIMED_OUT: RunOutcome.TIMED_OUT,
        }.get(status)
        updated = replace(
            run,
            status=status,
            version=run.version + 1,
            updated_at=now,
            started_at=run.started_at or (now if status is RunStatus.RUNNING else None),
            completed_at=now if outcome else run.completed_at,
            outcome=outcome,
        )
        await self.repository.update(updated, expected_version=run.version)
        if self._publisher and outcome:
            await self._publisher(
                RunFailed(correlation_id=updated.correlation_id, run_id=str(run_id))
                if status is RunStatus.FAILED
                else RunCompleted(correlation_id=updated.correlation_id, run_id=str(run_id))
            )
        return updated

    async def queue_run(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.QUEUED)

    async def mark_initializing(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.INITIALIZING)

    async def mark_running(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.RUNNING)

    async def start_run(self, run_id: RunId) -> RunRecord:
        run = await self.get_run(run_id)
        if run.status in {RunStatus.CREATED, RunStatus.QUEUED}:
            await self.mark_initializing(run_id)
        return await self.mark_running(run_id)

    async def mark_waiting(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.WAITING)

    async def mark_approval_required(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.WAITING_FOR_APPROVAL)

    async def mark_retrying(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.RETRYING)

    async def pause_run(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.PAUSED)

    async def resume_run(self, run_id: RunId, *, to_queued: bool = False) -> RunRecord:
        return await self._transition(run_id, RunStatus.QUEUED if to_queued else RunStatus.RUNNING)

    async def request_cancellation(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.CANCELLING)

    async def cancel_run(self, run_id: RunId) -> RunRecord:
        run = await self.get_run(run_id)
        return (
            run
            if run.status is RunStatus.CANCELLED
            else await self._transition(run_id, RunStatus.CANCELLED)
        )

    async def complete_run(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.SUCCEEDED)

    async def fail_run(self, run_id: RunId, error: Exception | RunFailure) -> RunRecord:
        failure = (
            error
            if isinstance(error, RunFailure)
            else RunFailure(
                error.code if isinstance(error, AliOSError) else "run_failure",
                error.message if isinstance(error, AliOSError) else "Run failed",
            )
        )
        updated = await self._transition(run_id, RunStatus.FAILED)
        updated = replace(updated, failure=failure)
        return await self.repository.update(updated, expected_version=updated.version)

    async def mark_timed_out(self, run_id: RunId) -> RunRecord:
        return await self._transition(run_id, RunStatus.TIMED_OUT)

    async def record_heartbeat(
        self,
        run_id: RunId,
        *,
        timestamp: datetime | None = None,
        expected_version: int | None = None,
    ) -> RunRecord:
        run = await self.get_run(run_id)
        time = timestamp or self._clock()
        if time.tzinfo is None or run.status in TERMINAL:
            raise ValidationError("Invalid heartbeat")
        updated = replace(run, heartbeat_at=time, updated_at=time, version=run.version + 1)
        result = await self.repository.update(
            updated,
            expected_version=expected_version if expected_version is not None else run.version,
        )
        machine = self._machines.get(run_id)
        if machine is not None:
            machine.version = result.version
            machine.updated_at = result.updated_at
        return result

    async def query_stale_runs(
        self,
        *,
        stale_before: datetime,
        statuses: frozenset[RunStatus] | None = None,
        limit: int | None = None,
    ) -> tuple[RunRecord, ...]:
        if stale_before.tzinfo is None or (limit is not None and limit < 0):
            raise ValidationError("Invalid stale-run query")
        active = statuses or frozenset(set(RunStatus) - set(TERMINAL))
        candidates = await self.repository.list(RunFilter(statuses=active))
        stale = tuple(
            run for run in candidates if (run.heartbeat_at or run.updated_at) < stale_before
        )
        ordered = tuple(
            sorted(stale, key=lambda run: (run.heartbeat_at or run.updated_at, str(run.run_id)))
        )
        return ordered if limit is None else ordered[:limit]

    async def delete_run(
        self, run_id: RunId, *, force: bool = False, cascade: bool = False
    ) -> None:
        run = await self.repository.get(run_id)
        children = await self.repository.list_children(run_id)
        if children and not cascade:
            raise ResourceConflictError("Run has child runs")
        if run.status not in TERMINAL and not force:
            raise ValidationError("Active runs require force deletion")
        if cascade:
            for child in children:
                await self.delete_run(child.run_id, force=True, cascade=True)
        await self.repository.delete(run_id)
        self._machines.pop(run_id, None)
