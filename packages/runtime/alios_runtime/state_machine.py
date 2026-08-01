"""Concurrency-safe run state transitions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from alios_core.errors import InvalidStateTransitionError, ResourceConflictError, SerializationError
from alios_core.events import RunStateChanged
from alios_core.ids import CorrelationId, RunId
from alios_core.types import RunStatus, utc_now

TERMINAL = frozenset(
    {RunStatus.CANCELLED, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMED_OUT}
)
TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = MappingProxyType(
    {
        RunStatus.CREATED: frozenset(
            {
                RunStatus.QUEUED,
                RunStatus.INITIALIZING,
                RunStatus.CANCELLING,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
            }
        ),
        RunStatus.QUEUED: frozenset(
            {
                RunStatus.INITIALIZING,
                RunStatus.CANCELLING,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            }
        ),
        RunStatus.INITIALIZING: frozenset(
            {
                RunStatus.RUNNING,
                RunStatus.RETRYING,
                RunStatus.CANCELLING,
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            }
        ),
        RunStatus.RUNNING: frozenset(
            {
                RunStatus.WAITING,
                RunStatus.WAITING_FOR_APPROVAL,
                RunStatus.RETRYING,
                RunStatus.PAUSED,
                RunStatus.CANCELLING,
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            }
        ),
        RunStatus.WAITING: frozenset(
            {
                RunStatus.RUNNING,
                RunStatus.RETRYING,
                RunStatus.PAUSED,
                RunStatus.CANCELLING,
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            }
        ),
        RunStatus.WAITING_FOR_APPROVAL: frozenset(
            {
                RunStatus.RUNNING,
                RunStatus.PAUSED,
                RunStatus.CANCELLING,
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            }
        ),
        RunStatus.RETRYING: frozenset(
            {
                RunStatus.QUEUED,
                RunStatus.INITIALIZING,
                RunStatus.RUNNING,
                RunStatus.CANCELLING,
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            }
        ),
        RunStatus.PAUSED: frozenset(
            {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.CANCELLING,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            }
        ),
        RunStatus.CANCELLING: frozenset(
            {RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.TIMED_OUT}
        ),
        **{status: frozenset() for status in TERMINAL},
    }
)


def _freeze(values: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(values or {}))


@dataclass(frozen=True, slots=True)
class RunTransition:
    transition_id: str = field(default_factory=lambda: str(uuid4()))
    run_id: RunId = field(default_factory=RunId)
    previous_status: RunStatus = RunStatus.CREATED
    new_status: RunStatus = RunStatus.CREATED
    version: int = 0
    occurred_at: datetime = field(default_factory=utc_now)
    reason: str | None = None
    actor: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "run_id": str(self.run_id),
            "previous_status": self.previous_status.value,
            "new_status": self.new_status.value,
            "version": self.version,
            "occurred_at": self.occurred_at.isoformat(),
            "reason": self.reason,
            "actor": self.actor,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunTransition:
        try:
            return cls(
                str(data["transition_id"]),
                RunId(data["run_id"]),
                RunStatus(data["previous_status"]),
                RunStatus(data["new_status"]),
                int(data["version"]),
                datetime.fromisoformat(data["occurred_at"]),
                data.get("reason"),
                data.get("actor"),
                data.get("metadata", {}),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise SerializationError("Invalid run transition", cause=error) from error


class RunStateMachine:
    def __init__(
        self,
        run_id: RunId | None = None,
        *,
        status: RunStatus = RunStatus.CREATED,
        version: int = 0,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        transition_history: tuple[RunTransition, ...] = (),
        event_publisher: Callable[[RunStateChanged], Awaitable[object]] | None = None,
    ) -> None:
        self.run_id = run_id or RunId()
        self.status = status
        self.version = version
        self.created_at = created_at or utc_now()
        self.updated_at = updated_at or self.created_at
        self._history = tuple(transition_history)
        self._lock = asyncio.Lock()
        self._publisher = event_publisher

    @property
    def transition_history(self) -> tuple[RunTransition, ...]:
        return self._history

    def can_transition(self, status: RunStatus) -> bool:
        return status in TRANSITIONS[self.status]

    def allowed_transitions(self) -> frozenset[RunStatus]:
        return TRANSITIONS[self.status]

    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def last_transition(self) -> RunTransition | None:
        return self._history[-1] if self._history else None

    @property
    def transition_count(self) -> int:
        return len(self._history)

    async def transition(
        self,
        new_status: RunStatus,
        *,
        expected_version: int | None = None,
        reason: str | None = None,
        actor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        correlation_id: CorrelationId | None = None,
    ) -> RunTransition:
        async with self._lock:
            if expected_version is not None and expected_version != self.version:
                raise ResourceConflictError(
                    "Run state version conflict",
                    {
                        "run_id": str(self.run_id),
                        "expected_version": expected_version,
                        "actual_version": self.version,
                    },
                )
            if not self.can_transition(new_status):
                raise InvalidStateTransitionError(
                    "Invalid run state transition",
                    {
                        "run_id": str(self.run_id),
                        "current_status": self.status.value,
                        "requested_status": new_status.value,
                    },
                )
            previous = self.status
            next_version = self.version + 1
            record = RunTransition(
                run_id=self.run_id,
                previous_status=previous,
                new_status=new_status,
                version=next_version,
                reason=reason,
                actor=actor,
                metadata=metadata or {},
            )
            self.status = new_status
            self.version = next_version
            self.updated_at = record.occurred_at
            self._history = (*self._history, record)
        if self._publisher:
            await self._publisher(
                RunStateChanged(
                    correlation_id=correlation_id or CorrelationId(),
                    run_id=str(self.run_id),
                    previous_state=previous.value,
                    current_state=new_status.value,
                    metadata={
                        "version": next_version,
                        "reason": reason or "",
                        "actor": actor or "",
                    },
                )
            )
        return record

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "status": self.status.value,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "transition_history": [item.to_dict() for item in self._history],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunStateMachine:
        try:
            history = tuple(
                RunTransition.from_dict(item) for item in data.get("transition_history", [])
            )
            result = cls(
                RunId(data["run_id"]),
                status=RunStatus(data["status"]),
                version=int(data["version"]),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                transition_history=history,
            )
        except (KeyError, ValueError, TypeError) as error:
            raise SerializationError("Invalid run state machine", cause=error) from error
        if len(history) != result.version or (
            history and history[-1].new_status is not result.status
        ):
            raise SerializationError("Inconsistent run transition history")
        return result
