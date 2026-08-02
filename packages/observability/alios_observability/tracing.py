"""Immutable, provider-neutral trace context and completed span contracts."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Self, cast

from alios_core.errors import (
    LogSerializationError,
    SpanValidationError,
    TraceContextError,
    TraceSerializationError,
)
from alios_core.ids import CorrelationId, Identifier, RunId, SpanId, TenantId, TraceId, UserId
from alios_core.types import JsonValue

from .logging import LogException, RedactionPolicy, default_redaction_policy

_CURRENT: ContextVar[TraceContext | None] = ContextVar("alios_trace_context", default=None)
_BAGGAGE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SpanValidationError("Trace timestamps must be timezone-aware")
    return value


def _text(
    value: object, field_name: str, maximum: int, *, required: bool = False, newlines: bool = False
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise SpanValidationError(f"Invalid {field_name}")
    result = value.strip()
    if not result and required:
        raise SpanValidationError(f"Invalid {field_name}")
    if (
        len(result) > maximum
        or "\0" in result
        or any(
            (ord(char) < 32 and (not newlines or char not in "\n\r")) or ord(char) == 127
            for char in result
        )
    ):
        raise SpanValidationError(f"Invalid {field_name}")
    return result or None


def _normal(
    value: object, *, depth: int = 0, count: list[int] | None = None, seen: set[int] | None = None
) -> JsonValue:
    if depth > 16:
        raise TraceSerializationError("Trace data nesting is too deep")
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 16_384:
            raise TraceSerializationError("Trace string is too long")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceSerializationError("Trace data is not JSON-compatible")
        return value
    if isinstance(value, Identifier):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TraceSerializationError("Trace attribute datetimes must be timezone-aware")
        return value.isoformat()
    counter = count if count is not None else [0]
    visited = seen if seen is not None else set()
    if isinstance(value, Mapping):
        if id(value) in visited:
            raise TraceSerializationError("Trace data contains a cycle")
        visited.add(id(value))
        try:
            result: dict[str, JsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TraceSerializationError("Trace data mapping keys must be strings")
                counter[0] += 1
                if counter[0] > 10_000:
                    raise TraceSerializationError("Trace data is too large")
                result[key] = _normal(item, depth=depth + 1, count=counter, seen=visited)
            return result
        finally:
            visited.remove(id(value))
    if isinstance(value, (list, tuple)):
        if id(value) in visited:
            raise TraceSerializationError("Trace data contains a cycle")
        visited.add(id(value))
        try:
            sequence_result: list[JsonValue] = []
            for item in value:
                counter[0] += 1
                if counter[0] > 10_000:
                    raise TraceSerializationError("Trace data is too large")
                sequence_result.append(_normal(item, depth=depth + 1, count=counter, seen=visited))
            return sequence_result
        finally:
            visited.remove(id(value))
    raise TraceSerializationError("Trace data is not JSON-compatible")


def _freeze(value: JsonValue) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return cast(JsonValue, value)


def _attributes(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TraceSerializationError("Trace attributes must be a mapping")
    normalized = _normal(value)
    if not isinstance(normalized, dict):
        raise TraceSerializationError("Trace attributes must be a mapping")
    return MappingProxyType({key: _freeze(item) for key, item in normalized.items()})


def _baggage(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or len(value) > 64:
        raise TraceContextError("Invalid trace baggage")
    result: dict[str, str] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(item, str)
            or not _BAGGAGE.fullmatch(key)
            or key.startswith("__")
            or len(item) > 1024
            or "\0" in item
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
        ):
            raise TraceContextError("Invalid trace baggage")
        result[key] = item
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class TraceSource:
    component: str
    module: str | None = None
    operation: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component", _text(self.component, "trace component", 256, required=True)
        )
        object.__setattr__(self, "module", _text(self.module, "trace module", 256))
        object.__setattr__(self, "operation", _text(self.operation, "trace operation", 256))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"component": self.component, "module": self.module, "operation": self.operation}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("component"), str)
            or any(
                value.get(name) is not None and not isinstance(value.get(name), str)
                for name in ("module", "operation")
            )
        ):
            raise TraceSerializationError("Invalid trace source")
        return cls(
            cast(str, value["component"]),
            cast(str | None, value.get("module")),
            cast(str | None, value.get("operation")),
        )


class SpanKind(StrEnum):
    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"

    @classmethod
    def parse(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise SpanValidationError("Invalid span kind")
        try:
            return cls(value)
        except ValueError as error:
            raise SpanValidationError("Invalid span kind") from error


class SpanStatus(StrEnum):
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    @classmethod
    def parse(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise SpanValidationError("Invalid span status")
        try:
            return cls(value)
        except ValueError as error:
            raise SpanValidationError("Invalid span status") from error


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: TraceId
    span_id: SpanId
    parent_span_id: SpanId | None = None
    correlation_id: CorrelationId | None = None
    run_id: RunId | None = None
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    sampled: bool = True
    baggage: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected = (
            (self.trace_id, TraceId),
            (self.span_id, SpanId),
            (self.parent_span_id, SpanId),
            (self.correlation_id, CorrelationId),
            (self.run_id, RunId),
            (self.tenant_id, TenantId),
            (self.user_id, UserId),
        )
        if (
            any(value is not None and not isinstance(value, kind) for value, kind in expected)
            or not isinstance(self.sampled, bool)
            or self.parent_span_id == self.span_id
        ):
            raise TraceContextError("Invalid trace context")
        object.__setattr__(self, "baggage", _baggage(self.baggage))

    @classmethod
    def create_root(
        cls,
        *,
        trace_id: TraceId | None = None,
        span_id: SpanId | None = None,
        correlation_id: CorrelationId | None = None,
        run_id: RunId | None = None,
        tenant_id: TenantId | None = None,
        user_id: UserId | None = None,
        sampled: bool = True,
        baggage: Mapping[str, str] | None = None,
    ) -> Self:
        return cls(
            trace_id or TraceId(),
            span_id or SpanId(),
            None,
            correlation_id,
            run_id,
            tenant_id,
            user_id,
            sampled,
            baggage or {},
        )

    def create_child(
        self,
        *,
        span_id: SpanId | None = None,
        sampled: bool | None = None,
        baggage: Mapping[str, str] | None = None,
    ) -> Self:
        if baggage is not None and not isinstance(baggage, Mapping):
            raise TraceContextError("Invalid trace baggage")
        child = span_id or SpanId()
        if child == self.span_id:
            raise TraceContextError("Child span cannot equal parent span")
        return cast(
            Self,
            TraceContext(
                self.trace_id,
                child,
                self.span_id,
                self.correlation_id,
                self.run_id,
                self.tenant_id,
                self.user_id,
                self.sampled if sampled is None else sampled,
                {**self.baggage, **(baggage or {})},
            ),
        )

    def with_baggage(self, values: Mapping[str, str] | None = None, **kwargs: str) -> Self:
        if values is not None and not isinstance(values, Mapping):
            raise TraceContextError("Invalid trace baggage")
        if values is not None and set(values).intersection(kwargs):
            raise TraceContextError("Ambiguous trace baggage")
        return cast(
            Self,
            TraceContext(
                self.trace_id,
                self.span_id,
                self.parent_span_id,
                self.correlation_id,
                self.run_id,
                self.tenant_id,
                self.user_id,
                self.sampled,
                {**self.baggage, **(values or {}), **kwargs},
            ),
        )

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "trace_id": str(self.trace_id),
            "span_id": str(self.span_id),
            "parent_span_id": str(self.parent_span_id) if self.parent_span_id else None,
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "sampled": self.sampled,
            "baggage": policy.redact(dict(self.baggage)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("trace_id"), str)
                or not isinstance(value.get("span_id"), str)
                or not isinstance(value.get("sampled"), bool)
                or not isinstance(value.get("baggage", {}), Mapping)
            ):
                raise ValueError

            def optional(name: str, kind: type[Identifier]) -> Identifier | None:
                item = value.get(name)
                if item is not None and (not isinstance(item, str) or not item.strip()):
                    raise ValueError
                return kind(item) if item else None

            return cls(
                TraceId(cast(str, value["trace_id"])),
                SpanId(cast(str, value["span_id"])),
                cast(SpanId | None, optional("parent_span_id", SpanId)),
                cast(CorrelationId | None, optional("correlation_id", CorrelationId)),
                cast(RunId | None, optional("run_id", RunId)),
                cast(TenantId | None, optional("tenant_id", TenantId)),
                cast(UserId | None, optional("user_id", UserId)),
                cast(bool, value["sampled"]),
                cast(Mapping[str, str], value.get("baggage", {})),
            )
        except (TypeError, ValueError, TraceContextError) as error:
            raise TraceSerializationError("Invalid trace context") from error


class _Binding(AbstractContextManager[None], AbstractAsyncContextManager[None]):
    def __init__(self, context: TraceContext) -> None:
        self._context = context
        self._token: Token[TraceContext | None] | None = None

    def __enter__(self) -> None:
        if self._token is not None:
            raise TraceContextError("Trace context binding is already active")
        self._token = _CURRENT.set(self._context)

    def __exit__(self, *_: object) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(self, *_: object) -> None:
        self.__exit__()


def current_trace_context() -> TraceContext | None:
    return _CURRENT.get()


def require_trace_context() -> TraceContext:
    context = current_trace_context()
    if context is None:
        raise TraceContextError("Trace context is not bound")
    return context


def bind_trace_context(context: TraceContext) -> _Binding:
    if not isinstance(context, TraceContext):
        raise TraceContextError("Invalid trace context")
    return _Binding(context)


@dataclass(frozen=True, slots=True)
class SpanEvent:
    name: str
    timestamp: datetime
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "span event name", 512, required=True))
        _aware(self.timestamp)
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "name": policy.redact(self.name),
            "timestamp": self.timestamp.isoformat(),
            "attributes": policy.redact(self.attributes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("name"), str)
                or not isinstance(value.get("timestamp"), str)
                or not isinstance(value.get("attributes", {}), Mapping)
            ):
                raise ValueError
            return cls(
                cast(str, value["name"]),
                datetime.fromisoformat(cast(str, value["timestamp"])),
                cast(Mapping[str, object], value.get("attributes", {})),
            )
        except (TypeError, ValueError, SpanValidationError, TraceSerializationError) as error:
            raise TraceSerializationError("Invalid span event") from error


@dataclass(frozen=True, slots=True)
class SpanLink:
    context: TraceContext
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.context, TraceContext):
            raise SpanValidationError("Invalid span link context")
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "context": self.context.to_dict(policy),
            "attributes": policy.redact(self.attributes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("context"), Mapping)
                or not isinstance(value.get("attributes", {}), Mapping)
            ):
                raise ValueError
            return cls(
                TraceContext.from_dict(cast(Mapping[str, object], value["context"])),
                cast(Mapping[str, object], value.get("attributes", {})),
            )
        except (
            TypeError,
            ValueError,
            TraceContextError,
            TraceSerializationError,
            SpanValidationError,
        ) as error:
            raise TraceSerializationError("Invalid span link") from error


@dataclass(frozen=True, slots=True)
class SpanRecord:
    context: TraceContext
    name: str
    source: TraceSource
    kind: SpanKind
    status: SpanStatus
    started_at: datetime
    ended_at: datetime
    status_message: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)
    events: tuple[SpanEvent, ...] = ()
    links: tuple[SpanLink, ...] = ()
    exception: LogException | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.context, TraceContext)
            or not isinstance(self.source, TraceSource)
            or not isinstance(self.kind, SpanKind)
            or not isinstance(self.status, SpanStatus)
        ):
            raise SpanValidationError("Invalid span record")
        object.__setattr__(self, "name", _text(self.name, "span name", 512, required=True))
        started, ended = _aware(self.started_at), _aware(self.ended_at)
        if ended < started:
            raise SpanValidationError("Span end precedes start")
        object.__setattr__(
            self,
            "status_message",
            _text(self.status_message, "span status message", 4096, newlines=True),
        )
        object.__setattr__(self, "attributes", _attributes(self.attributes))
        if not isinstance(self.events, tuple) or not all(
            isinstance(event, SpanEvent) for event in self.events
        ):
            raise SpanValidationError("Invalid span events")
        if any(
            event.timestamp < started or event.timestamp > ended for event in self.events
        ) or tuple(event.timestamp for event in self.events) != tuple(
            sorted(event.timestamp for event in self.events)
        ):
            raise SpanValidationError("Invalid span event timestamps")
        if not isinstance(self.links, tuple) or not all(
            isinstance(link, SpanLink) for link in self.links
        ):
            raise SpanValidationError("Invalid span links")
        identities = tuple((link.context.trace_id, link.context.span_id) for link in self.links)
        if (self.context.trace_id, self.context.span_id) in identities or len(
            set(identities)
        ) != len(identities):
            raise SpanValidationError("Invalid span links")
        if self.exception is not None and not isinstance(self.exception, LogException):
            raise SpanValidationError("Invalid span exception")
        if self.exception is not None and self.status in (SpanStatus.OK, SpanStatus.UNSET):
            raise SpanValidationError("Invalid span status exception")

    @property
    def duration(self) -> timedelta:
        return self.ended_at - self.started_at

    @property
    def duration_ns(self) -> int:
        duration = self.duration
        return (
            duration.days * 86_400 + duration.seconds
        ) * 1_000_000_000 + duration.microseconds * 1_000

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "context": self.context.to_dict(policy),
            "name": policy.redact(self.name),
            "source": self.source.to_dict(),
            "kind": self.kind.value,
            "status": self.status.value,
            "status_message": policy.redact(self.status_message) if self.status_message else None,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "duration_ns": self.duration_ns,
            "attributes": policy.redact(self.attributes),
            "events": [event.to_dict(policy) for event in self.events],
            "links": [link.to_dict(policy) for link in self.links],
            "exception": self.exception.to_dict(policy) if self.exception else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("context"), Mapping)
                or not isinstance(value.get("source"), Mapping)
                or not isinstance(value.get("kind"), str)
                or not isinstance(value.get("status"), str)
                or not isinstance(value.get("name"), str)
                or not isinstance(value.get("started_at"), str)
                or not isinstance(value.get("ended_at"), str)
                or not isinstance(value.get("duration_ns"), int)
                or isinstance(value.get("duration_ns"), bool)
                or not isinstance(value.get("attributes", {}), Mapping)
                or not isinstance(value.get("events", []), (list, tuple))
                or not isinstance(value.get("links", []), (list, tuple))
            ):
                raise ValueError
            events = cast(
                list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
                value.get("events", []),
            )
            links = cast(
                list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
                value.get("links", []),
            )
            if not all(isinstance(item, Mapping) for item in events) or not all(
                isinstance(item, Mapping) for item in links
            ):
                raise ValueError
            raw_exception = value.get("exception")
            if raw_exception is not None and not isinstance(raw_exception, Mapping):
                raise ValueError
            result = cls(
                TraceContext.from_dict(cast(Mapping[str, object], value["context"])),
                cast(str, value["name"]),
                TraceSource.from_dict(cast(Mapping[str, object], value["source"])),
                SpanKind.parse(cast(str, value["kind"])),
                SpanStatus.parse(cast(str, value["status"])),
                datetime.fromisoformat(cast(str, value["started_at"])),
                datetime.fromisoformat(cast(str, value["ended_at"])),
                cast(str | None, value.get("status_message")),
                cast(Mapping[str, object], value.get("attributes", {})),
                tuple(SpanEvent.from_dict(item) for item in events),
                tuple(SpanLink.from_dict(item) for item in links),
                LogException.from_dict(cast(Mapping[str, object], raw_exception))
                if raw_exception is not None
                else None,
            )
            if result.duration_ns != value["duration_ns"]:
                raise ValueError
            return result
        except (
            TypeError,
            ValueError,
            LogSerializationError,
            SpanValidationError,
            TraceSerializationError,
        ) as error:
            raise TraceSerializationError("Invalid span record") from error
