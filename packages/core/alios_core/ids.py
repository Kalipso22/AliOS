"""Strongly typed UUID identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True, eq=False)
class Identifier:
    value: UUID

    def __init__(self, value: UUID | str | None = None) -> None:
        parsed = uuid4() if value is None else UUID(str(value))
        if parsed.version != 4:
            raise ValueError("AliOS identifiers must use UUID version 4")
        object.__setattr__(self, "value", parsed)

    def __str__(self) -> str:
        return str(self.value)

    def __eq__(self, other: object) -> bool:
        return (
            type(self) is type(other)
            and isinstance(other, Identifier)
            and self.value == other.value
        )

    def __hash__(self) -> int:
        return hash((type(self), self.value))

    def to_json(self) -> str:
        return str(self)


class RunId(Identifier):
    pass


class TaskId(Identifier):
    pass


class AgentId(Identifier):
    pass


class WorkflowId(Identifier):
    pass


class ToolId(Identifier):
    pass


class ProviderId(Identifier):
    pass


class PluginId(Identifier):
    pass


class EventId(Identifier):
    pass


class CorrelationId(Identifier):
    pass


class TenantId(Identifier):
    pass


class UserId(Identifier):
    pass


class MemoryId(Identifier):
    pass


class ArtifactId(Identifier):
    pass


class ScheduleId(Identifier):
    pass


class CheckpointId(Identifier):
    pass
