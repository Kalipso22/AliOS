"""Checkpoint persistence and deterministic recovery coordination."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, cast
from uuid import uuid4

from alios_core.errors import (
    AliOSError,
    CheckpointError,
    RecoveryError,
    RecoveryPlanStaleError,
    ResourceConflictError,
    SerializationError,
    ValidationError,
)
from alios_core.events import BaseEvent, CheckpointCreated, RecoveryCompleted, RecoveryStarted
from alios_core.ids import CheckpointId, CorrelationId, RunId
from alios_core.types import RunStatus, utc_now

from .execution_context import ExecutionContext
from .run_manager import RunManager


class CheckpointKind(StrEnum):
    MANUAL = "manual"
    RUN_STARTED = "run_started"
    PROGRESS = "progress"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_TIMED_OUT = "run_timed_out"
    RECOVERY = "recovery"


class RecoveryMode(StrEnum):
    RESUME = "resume"
    RETRY = "retry"
    RESTART = "restart"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(_plain(value), sort_keys=True))
    except (TypeError, ValueError) as error:
        raise SerializationError("Checkpoint state is not JSON-compatible", cause=error) from error


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: CheckpointId = field(default_factory=CheckpointId)
    run_id: RunId = field(default_factory=RunId)
    correlation_id: CorrelationId = field(default_factory=CorrelationId)
    version: int = 1
    kind: CheckpointKind = CheckpointKind.MANUAL
    run_status: RunStatus = RunStatus.CREATED
    state: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    checksum: str = ""

    def __post_init__(self) -> None:
        if self.version < 1 or self.schema_version < 1:
            raise ValidationError("Checkpoint versions must be positive")
        if self.created_at.tzinfo is None:
            raise ValidationError("Checkpoint timestamp must be timezone-aware")
        state = _freeze(_json(self.state))
        metadata = _freeze(_json(self.metadata))
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "metadata", metadata)
        expected = self._checksum()
        if self.checksum and self.checksum != expected:
            raise SerializationError("Checkpoint checksum is invalid")
        object.__setattr__(self, "checksum", expected)

    def _checksum(self) -> str:
        return hashlib.sha256(
            _canonical(
                {
                    "checkpoint_id": str(self.checkpoint_id),
                    "run_id": str(self.run_id),
                    "correlation_id": str(self.correlation_id),
                    "version": self.version,
                    "kind": self.kind.value,
                    "run_status": self.run_status.value,
                    "state": self.state,
                    "metadata": self.metadata,
                    "schema_version": self.schema_version,
                    "created_at": self.created_at.isoformat(),
                }
            ).encode()
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": str(self.checkpoint_id),
            "run_id": str(self.run_id),
            "correlation_id": str(self.correlation_id),
            "version": self.version,
            "kind": self.kind.value,
            "run_status": self.run_status.value,
            "state": _json(self.state),
            "metadata": _json(self.metadata),
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat(),
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Checkpoint:
        try:
            return cls(
                CheckpointId(data["checkpoint_id"]),
                RunId(data["run_id"]),
                CorrelationId(data["correlation_id"]),
                int(data["version"]),
                CheckpointKind(data["kind"]),
                RunStatus(data["run_status"]),
                data["state"],
                data.get("metadata", {}),
                int(data.get("schema_version", 1)),
                datetime.fromisoformat(data["created_at"]),
                str(data["checksum"]),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise SerializationError("Invalid checkpoint", cause=error) from error


@dataclass(frozen=True, slots=True)
class CheckpointFilter:
    run_id: RunId | None = None
    correlation_id: CorrelationId | None = None
    kinds: frozenset[CheckpointKind] | None = None
    statuses: frozenset[RunStatus] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    minimum_version: int | None = None
    maximum_version: int | None = None
    limit: int | None = None
    offset: int = 0

    def __post_init__(self) -> None:
        if (
            self.limit is not None
            and self.limit < 0
            or self.offset < 0
            or self.minimum_version is not None
            and self.minimum_version < 1
            or self.maximum_version is not None
            and self.minimum_version is not None
            and self.maximum_version < self.minimum_version
        ):
            raise ValidationError("Invalid checkpoint filter")


class CheckpointRepository(Protocol):
    async def append(
        self,
        *,
        run_id: RunId,
        correlation_id: CorrelationId,
        kind: CheckpointKind,
        run_status: RunStatus,
        state: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
        schema_version: int = 1,
        expected_latest_version: int | None = None,
    ) -> Checkpoint: ...


class InMemoryCheckpointRepository:
    def __init__(self) -> None:
        self._items: dict[CheckpointId, Checkpoint] = {}
        self._lock = asyncio.Lock()

    async def append(self, **kwargs: Any) -> Checkpoint:
        async with self._lock:
            run_id: RunId = kwargs["run_id"]
            items = [item for item in self._items.values() if item.run_id == run_id]
            latest = max((item.version for item in items), default=0)
            if (
                kwargs.get("expected_latest_version") is not None
                and kwargs["expected_latest_version"] != latest
            ):
                raise ResourceConflictError("Checkpoint version conflict")
            item = Checkpoint(
                run_id=run_id,
                correlation_id=kwargs["correlation_id"],
                version=latest + 1,
                kind=kwargs["kind"],
                run_status=kwargs["run_status"],
                state=kwargs["state"],
                metadata=kwargs.get("metadata") or {},
                schema_version=kwargs.get("schema_version", 1),
            )
            self._items[item.checkpoint_id] = item
            return item

    async def get_optional(self, checkpoint_id: CheckpointId) -> Checkpoint | None:
        async with self._lock:
            return self._items.get(checkpoint_id)

    async def get(self, checkpoint_id: CheckpointId) -> Checkpoint:
        item = await self.get_optional(checkpoint_id)
        if item is None:
            raise CheckpointError("Checkpoint was not found")
        return item

    async def list(self, filters: CheckpointFilter | None = None) -> tuple[Checkpoint, ...]:
        filters = filters or CheckpointFilter()
        async with self._lock:
            items = tuple(self._items.values())
        output = tuple(
            item
            for item in items
            if (filters.run_id is None or item.run_id == filters.run_id)
            and (filters.correlation_id is None or item.correlation_id == filters.correlation_id)
            and (filters.kinds is None or item.kind in filters.kinds)
            and (filters.statuses is None or item.run_status in filters.statuses)
            and (filters.minimum_version is None or item.version >= filters.minimum_version)
            and (filters.maximum_version is None or item.version <= filters.maximum_version)
        )
        output = tuple(sorted(output, key=lambda item: (str(item.run_id), item.version)))
        return output[
            filters.offset : None if filters.limit is None else filters.offset + filters.limit
        ]

    async def latest(self, run_id: RunId) -> Checkpoint | None:
        items = await self.list(CheckpointFilter(run_id=run_id))
        return items[-1] if items else None

    async def delete(self, checkpoint_id: CheckpointId) -> None:
        async with self._lock:
            if checkpoint_id not in self._items:
                raise CheckpointError("Checkpoint was not found")
            del self._items[checkpoint_id]

    async def delete_for_run(self, run_id: RunId) -> int:
        async with self._lock:
            keys = [key for key, item in self._items.items() if item.run_id == run_id]
            for key in keys:
                del self._items[key]
            return len(keys)

    async def count(self, filters: CheckpointFilter | None = None) -> int:
        return len(await self.list(filters))


class CheckpointService:
    def __init__(
        self,
        repository: InMemoryCheckpointRepository,
        run_manager: RunManager,
        event_publisher: Callable[[CheckpointCreated], Awaitable[object]] | None = None,
    ) -> None:
        self.repository = repository
        self.run_manager = run_manager
        self._publisher = event_publisher

    async def create_checkpoint(
        self,
        run_id: RunId,
        *,
        state: Mapping[str, Any],
        kind: CheckpointKind = CheckpointKind.MANUAL,
        metadata: Mapping[str, Any] | None = None,
        expected_latest_version: int | None = None,
    ) -> Checkpoint:
        run = await self.run_manager.get_run(run_id)
        terminal = {
            CheckpointKind.RUN_SUCCEEDED: RunStatus.SUCCEEDED,
            CheckpointKind.RUN_FAILED: RunStatus.FAILED,
            CheckpointKind.RUN_CANCELLED: RunStatus.CANCELLED,
            CheckpointKind.RUN_TIMED_OUT: RunStatus.TIMED_OUT,
        }
        if kind in terminal and run.status is not terminal[kind]:
            raise CheckpointError("Terminal checkpoint status mismatch")
        item = await self.repository.append(
            run_id=run_id,
            correlation_id=run.correlation_id,
            kind=kind,
            run_status=run.status,
            state=state,
            metadata=metadata,
            expected_latest_version=expected_latest_version,
        )
        if self._publisher:
            await self._publisher(
                CheckpointCreated(
                    correlation_id=run.correlation_id,
                    checkpoint_id=str(item.checkpoint_id),
                    run_id=str(run_id),
                )
            )
        return item

    async def get_checkpoint(self, checkpoint_id: CheckpointId) -> Checkpoint:
        return await self.repository.get(checkpoint_id)

    async def get_latest_checkpoint(self, run_id: RunId) -> Checkpoint | None:
        return await self.repository.latest(run_id)

    async def list_checkpoints(
        self, filters: CheckpointFilter | None = None
    ) -> tuple[Checkpoint, ...]:
        return await self.repository.list(filters)

    async def delete_checkpoint(self, checkpoint_id: CheckpointId, *, force: bool = False) -> None:
        checkpoint = await self.repository.get(checkpoint_id)
        run = await self.run_manager.get_run(checkpoint.run_id)
        if (
            run.status
            not in {RunStatus.CANCELLED, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMED_OUT}
            and not force
        ):
            raise CheckpointError("Active run checkpoints require force deletion")
        await self.repository.delete(checkpoint_id)

    async def delete_run_checkpoints(self, run_id: RunId, *, force: bool = False) -> int:
        run = await self.run_manager.get_run(run_id)
        if (
            run.status
            not in {RunStatus.CANCELLED, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMED_OUT}
            and not force
        ):
            raise CheckpointError("Active run checkpoints require force deletion")
        return await self.repository.delete_for_run(run_id)

    async def verify_checkpoint(self, checkpoint: Checkpoint) -> bool:
        return checkpoint.checksum == checkpoint._checksum()


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    recovery_id: str
    run_id: RunId
    correlation_id: CorrelationId
    checkpoint_id: CheckpointId | None
    checkpoint_version: int | None
    mode: RecoveryMode
    source_status: RunStatus
    target_status: RunStatus
    created_at: datetime = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode in {RecoveryMode.RESUME, RecoveryMode.RETRY} and self.checkpoint_id is None:
            raise ValidationError("Recovery mode requires a checkpoint")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class RecoveryFailure:
    error_code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False
    occurred_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _freeze(self.details))


@dataclass(frozen=True, slots=True)
class RecoveredPayload:
    state: Mapping[str, Any]
    execution_context: ExecutionContext | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _freeze(_json(self.state)))
        object.__setattr__(self, "metadata", _freeze(_json(self.metadata)))


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    plan: RecoveryPlan
    success: bool
    recovered_state: Mapping[str, Any] | None
    recovered_context: ExecutionContext | None
    started_at: datetime
    completed_at: datetime
    duration: timedelta
    failure: RecoveryFailure | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.duration < timedelta():
            raise ValidationError("Recovery duration cannot be negative")
        if self.success != (self.failure is None):
            raise ValidationError("Recovery result success and failure are inconsistent")
        if not self.success and (
            self.recovered_state is not None or self.recovered_context is not None
        ):
            raise ValidationError("Failed recovery cannot expose recovered payload")
        if self.recovered_state is not None:
            object.__setattr__(self, "recovered_state", _freeze(_json(self.recovered_state)))
        object.__setattr__(self, "warnings", tuple(self.warnings))


class RecoveryCoordinator:
    def __init__(
        self,
        repository: InMemoryCheckpointRepository,
        run_manager: RunManager,
        event_publisher: Callable[[BaseEvent], Awaitable[object]] | None = None,
        clock: Callable[[], datetime] = utc_now,
        default_timeout: timedelta | None = None,
    ) -> None:
        self.repository = repository
        self.run_manager = run_manager
        self._publisher = event_publisher
        self._clock = clock
        if default_timeout is not None and default_timeout < timedelta():
            raise ValidationError("Recovery timeout cannot be negative")
        self.default_timeout = default_timeout
        self._active: set[RunId] = set()
        self._lock = asyncio.Lock()

    async def _revalidate_plan(self, plan: RecoveryPlan) -> Checkpoint | None:
        try:
            run = await self.run_manager.get_run(plan.run_id)
        except Exception as error:
            raise RecoveryPlanStaleError(
                "Recovery plan is stale",
                {
                    "run_id": str(plan.run_id),
                    "recovery_id": plan.recovery_id,
                    "mode": plan.mode.value,
                    "reason": "run_missing",
                },
                error,
            ) from error
        if run.correlation_id != plan.correlation_id or run.status is not plan.source_status:
            raise RecoveryPlanStaleError(
                "Recovery plan is stale",
                {
                    "run_id": str(plan.run_id),
                    "recovery_id": plan.recovery_id,
                    "mode": plan.mode.value,
                    "reason": "run_changed",
                    "expected_source_status": plan.source_status.value,
                    "actual_source_status": run.status.value,
                },
            )
        if plan.mode is RecoveryMode.RESTART:
            if plan.checkpoint_id is not None or plan.checkpoint_version is not None:
                raise RecoveryPlanStaleError(
                    "Recovery plan is stale",
                    {
                        "run_id": str(plan.run_id),
                        "recovery_id": plan.recovery_id,
                        "mode": plan.mode.value,
                        "reason": "restart_has_checkpoint",
                    },
                )
            return None
        if plan.checkpoint_id is None or plan.checkpoint_version is None:
            raise RecoveryPlanStaleError(
                "Recovery plan is stale",
                {
                    "run_id": str(plan.run_id),
                    "recovery_id": plan.recovery_id,
                    "mode": plan.mode.value,
                    "reason": "checkpoint_missing",
                },
            )
        try:
            checkpoint = await self.repository.get(plan.checkpoint_id)
        except Exception as error:
            raise RecoveryPlanStaleError(
                "Recovery plan is stale",
                {
                    "run_id": str(plan.run_id),
                    "recovery_id": plan.recovery_id,
                    "mode": plan.mode.value,
                    "reason": "checkpoint_missing",
                },
                error,
            ) from error
        if (
            checkpoint.run_id != plan.run_id
            or checkpoint.correlation_id != plan.correlation_id
            or checkpoint.version != plan.checkpoint_version
            or not await CheckpointService(self.repository, self.run_manager).verify_checkpoint(
                checkpoint
            )
        ):
            raise RecoveryPlanStaleError(
                "Recovery plan is stale",
                {
                    "run_id": str(plan.run_id),
                    "recovery_id": plan.recovery_id,
                    "mode": plan.mode.value,
                    "reason": "checkpoint_changed",
                    "expected_checkpoint_version": plan.checkpoint_version,
                    "actual_checkpoint_version": checkpoint.version,
                },
            )
        return checkpoint

    def _completed_event(
        self, plan: RecoveryPlan, *, success: bool, failure_code: str | None
    ) -> RecoveryCompleted:
        return RecoveryCompleted(
            correlation_id=plan.correlation_id,
            run_id=str(plan.run_id),
            recovery_id=plan.recovery_id,
            mode=plan.mode.value,
            success=success,
            failure_code=failure_code,
            checkpoint_id=str(plan.checkpoint_id) if plan.checkpoint_id else None,
        )

    async def create_plan(
        self,
        run_id: RunId,
        *,
        mode: RecoveryMode = RecoveryMode.RESUME,
        checkpoint_id: CheckpointId | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RecoveryPlan:
        run = await self.run_manager.get_run(run_id)
        checkpoint = (
            await self.repository.get(checkpoint_id)
            if checkpoint_id
            else await self.repository.latest(run_id)
        )
        if mode in {RecoveryMode.RESUME, RecoveryMode.RETRY} and checkpoint is None:
            raise CheckpointError("Recovery checkpoint is required")
        if checkpoint and (
            checkpoint.run_id != run_id or checkpoint.correlation_id != run.correlation_id
        ):
            raise CheckpointError("Checkpoint does not belong to run")
        return RecoveryPlan(
            str(uuid4()),
            run_id,
            run.correlation_id,
            checkpoint.checkpoint_id if checkpoint else None,
            checkpoint.version if checkpoint else None,
            mode,
            run.status,
            RunStatus.QUEUED if mode is RecoveryMode.RETRY else RunStatus.RUNNING,
            metadata=metadata or {},
        )

    async def recover(
        self,
        plan: RecoveryPlan,
        restore: Callable[[Checkpoint | None, RecoveryPlan], Any],
        *,
        timeout: timedelta | None = None,
    ) -> RecoveryResult:
        async with self._lock:
            if plan.run_id in self._active:
                raise ResourceConflictError("Recovery already active")
            self._active.add(plan.run_id)
        started = self._clock()
        checkpoint = await self._revalidate_plan(plan)
        try:
            if self._publisher:
                try:
                    await self._publisher(
                        RecoveryStarted(correlation_id=plan.correlation_id, run_id=str(plan.run_id))
                    )
                except Exception as error:
                    raise RecoveryError(
                        "Recovery started publication failed", {"stage": "started"}, error
                    ) from error

            async def invoke() -> RecoveredPayload:
                value = restore(checkpoint, plan)
                return cast(RecoveredPayload, await value if inspect.isawaitable(value) else value)

            effective = timeout if timeout is not None else self.default_timeout
            if effective is not None and effective < timedelta():
                raise ValidationError("Recovery timeout cannot be negative")
            worker = asyncio.create_task(invoke())
            try:
                payload = (
                    await asyncio.wait_for(worker, effective.total_seconds())
                    if effective is not None
                    else await worker
                )
            except TimeoutError:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                payload = None
                failure = RecoveryFailure("recovery_timeout", "Recovery restoration timed out")
                result = RecoveryResult(
                    plan,
                    False,
                    None,
                    None,
                    started,
                    self._clock(),
                    self._clock() - started,
                    failure,
                )
                if self._publisher:
                    try:
                        await self._publisher(
                            self._completed_event(
                                plan, success=False, failure_code="recovery_timeout"
                            )
                        )
                    except Exception as error:
                        raise RecoveryError(
                            "Recovery completed publication failed", {"success": False}, error
                        ) from error
                return result
            if not isinstance(payload, RecoveredPayload):
                raise ValidationError("Restorer must return RecoveredPayload")
            result = RecoveryResult(
                plan,
                True,
                _freeze(payload.state),
                payload.execution_context,
                started,
                self._clock(),
                self._clock() - started,
            )
            if self._publisher:
                try:
                    await self._publisher(
                        self._completed_event(plan, success=True, failure_code=None)
                    )
                except Exception as error:
                    raise RecoveryError(
                        "Recovery completed publication failed", {"success": True}, error
                    ) from error
            return result
        except asyncio.CancelledError:
            raise
        except RecoveryPlanStaleError:
            raise
        except Exception as error:
            failure = RecoveryFailure(
                error.code if isinstance(error, AliOSError) else "recovery_failure",
                error.message if isinstance(error, AliOSError) else "Recovery failed",
            )
            result = RecoveryResult(
                plan,
                False,
                None,
                None,
                started,
                self._clock(),
                self._clock() - started,
                failure,
            )
            if self._publisher:
                try:
                    await self._publisher(
                        self._completed_event(plan, success=False, failure_code=failure.error_code)
                    )
                except Exception as publication_error:
                    raise RecoveryError(
                        "Recovery completed publication failed",
                        {"success": False},
                        publication_error,
                    ) from publication_error
            return result
        finally:
            async with self._lock:
                self._active.discard(plan.run_id)
