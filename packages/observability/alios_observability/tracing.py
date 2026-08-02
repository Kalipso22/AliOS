"""Immutable, provider-neutral trace context and completed span contracts."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, Self, cast

from alios_core.errors import (
    LogSerializationError,
    SamplingError,
    SpanCompletionError,
    SpanLimitError,
    SpanStateError,
    SpanValidationError,
    TraceContextError,
    TracerClosedError,
    TraceSerializationError,
)
from alios_core.ids import CorrelationId, Identifier, RunId, SpanId, TenantId, TraceId, UserId
from alios_core.types import JsonValue, utc_now

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


class SamplingDecision(StrEnum):
    DROP = "drop"
    RECORD_ONLY = "record_only"
    RECORD_AND_SAMPLE = "record_and_sample"

    @classmethod
    def parse(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise SamplingError("Invalid sampling decision")
        try:
            return cls(value)
        except ValueError as error:
            raise SamplingError("Invalid sampling decision") from error


def _link_identities(links: tuple[SpanLink, ...]) -> tuple[tuple[TraceId, SpanId], ...]:
    if not isinstance(links, tuple) or not all(isinstance(link, SpanLink) for link in links):
        raise SpanValidationError("Invalid span links")
    identities = tuple((link.context.trace_id, link.context.span_id) for link in links)
    if len(set(identities)) != len(identities):
        raise SpanValidationError("Duplicate span links")
    return identities


@dataclass(frozen=True, slots=True)
class SamplingRequest:
    trace_id: TraceId
    parent_context: TraceContext | None
    name: str
    source: TraceSource
    kind: SpanKind
    attributes: Mapping[str, object] = field(default_factory=dict)
    links: tuple[SpanLink, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.trace_id, TraceId)
            or self.parent_context is not None
            and not isinstance(self.parent_context, TraceContext)
            or not isinstance(self.source, TraceSource)
            or not isinstance(self.kind, SpanKind)
        ):
            raise SamplingError("Invalid sampling request")
        object.__setattr__(self, "name", _text(self.name, "span name", 512, required=True))
        object.__setattr__(self, "attributes", _attributes(self.attributes))
        _link_identities(self.links)

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "trace_id": str(self.trace_id),
            "parent_context": self.parent_context.to_dict(policy) if self.parent_context else None,
            "name": policy.redact(self.name),
            "source": self.source.to_dict(),
            "kind": self.kind.value,
            "attributes": policy.redact(self.attributes),
            "links": [link.to_dict(policy) for link in self.links],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("trace_id"), str)
                or not isinstance(value.get("name"), str)
                or not isinstance(value.get("source"), Mapping)
                or not isinstance(value.get("kind"), str)
                or not isinstance(value.get("attributes", {}), Mapping)
                or not isinstance(value.get("links", []), (list, tuple))
            ):
                raise ValueError
            parent = value.get("parent_context")
            links = value.get("links", [])
            if parent is not None and not isinstance(parent, Mapping):
                raise ValueError
            typed_links = cast(list[object] | tuple[object, ...], links)
            if not all(isinstance(link, Mapping) for link in typed_links):
                raise ValueError
            return cls(
                TraceId(cast(str, value["trace_id"])),
                TraceContext.from_dict(cast(Mapping[str, object], parent))
                if parent is not None
                else None,
                cast(str, value["name"]),
                TraceSource.from_dict(cast(Mapping[str, object], value["source"])),
                SpanKind.parse(cast(str, value["kind"])),
                cast(Mapping[str, object], value.get("attributes", {})),
                tuple(SpanLink.from_dict(cast(Mapping[str, object], link)) for link in typed_links),
            )
        except (
            TypeError,
            ValueError,
            SamplingError,
            SpanValidationError,
            TraceSerializationError,
        ) as error:
            raise TraceSerializationError("Invalid sampling request") from error


@dataclass(frozen=True, slots=True)
class SamplingResult:
    decision: SamplingDecision
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.decision, SamplingDecision):
            raise SamplingError("Invalid sampling result")
        object.__setattr__(self, "attributes", _normalize_span_attributes(self.attributes))

    @property
    def is_recording(self) -> bool:
        return self.decision is not SamplingDecision.DROP

    @property
    def is_sampled(self) -> bool:
        return self.decision is SamplingDecision.RECORD_AND_SAMPLE

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {"decision": self.decision.value, "attributes": policy.redact(self.attributes)}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("decision"), str)
                or not isinstance(value.get("attributes", {}), Mapping)
            ):
                raise ValueError
            return cls(
                SamplingDecision.parse(cast(str, value["decision"])),
                cast(Mapping[str, object], value.get("attributes", {})),
            )
        except (TypeError, ValueError, SamplingError, TraceSerializationError) as error:
            raise TraceSerializationError("Invalid sampling result") from error


class Sampler(Protocol):
    async def should_sample(self, request: SamplingRequest) -> SamplingResult: ...


def _sampling_request(value: object) -> SamplingRequest:
    if not isinstance(value, SamplingRequest):
        raise SamplingError("Invalid sampling request")
    return value


class AlwaysOnSampler:
    async def should_sample(self, request: SamplingRequest) -> SamplingResult:
        _sampling_request(request)
        return SamplingResult(SamplingDecision.RECORD_AND_SAMPLE)


class AlwaysOffSampler:
    async def should_sample(self, request: SamplingRequest) -> SamplingResult:
        _sampling_request(request)
        return SamplingResult(SamplingDecision.DROP)


class AlwaysRecordSampler:
    async def should_sample(self, request: SamplingRequest) -> SamplingResult:
        _sampling_request(request)
        return SamplingResult(SamplingDecision.RECORD_ONLY)


@dataclass(frozen=True, slots=True)
class TraceIdRatioSampler:
    ratio: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.ratio, bool)
            or not isinstance(self.ratio, (int, float))
            or not math.isfinite(self.ratio)
            or not 0 <= self.ratio <= 1
        ):
            raise SamplingError("Invalid sampling ratio")
        object.__setattr__(self, "ratio", float(self.ratio))

    async def should_sample(self, request: SamplingRequest) -> SamplingResult:
        request = _sampling_request(request)
        if self.ratio == 0:
            return SamplingResult(SamplingDecision.DROP)
        if self.ratio == 1:
            return SamplingResult(SamplingDecision.RECORD_AND_SAMPLE)
        threshold = int(self.ratio * (1 << 128))
        decision = (
            SamplingDecision.RECORD_AND_SAMPLE
            if request.trace_id.value.int < threshold
            else SamplingDecision.DROP
        )
        return SamplingResult(decision)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"ratio": self.ratio}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        if (
            not isinstance(value, Mapping)
            or isinstance(value.get("ratio"), bool)
            or not isinstance(value.get("ratio"), (int, float))
        ):
            raise SamplingError("Invalid sampling ratio")
        return cls(cast(float, value["ratio"]))


@dataclass(frozen=True, slots=True)
class ParentBasedSampler:
    root_sampler: Sampler

    async def should_sample(self, request: SamplingRequest) -> SamplingResult:
        request = _sampling_request(request)
        if request.parent_context is not None:
            return SamplingResult(
                SamplingDecision.RECORD_AND_SAMPLE
                if request.parent_context.sampled
                else SamplingDecision.DROP
            )
        try:
            result = await self.root_sampler.should_sample(request)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except SamplingError:
            raise
        except Exception as error:
            raise SamplingError("Sampling failed") from error
        if not isinstance(result, SamplingResult):
            raise SamplingError("Invalid sampling result")
        return result


@dataclass(frozen=True, slots=True)
class SpanLimits:
    maximum_attributes: int = 128
    maximum_events: int = 128
    maximum_links: int = 128

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100_000
            for value in (self.maximum_attributes, self.maximum_events, self.maximum_links)
        ):
            raise SpanLimitError("Invalid span limits")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "maximum_attributes": self.maximum_attributes,
            "maximum_events": self.maximum_events,
            "maximum_links": self.maximum_links,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if not isinstance(value, Mapping):
                raise ValueError
            limits = tuple(
                value[name] for name in ("maximum_attributes", "maximum_events", "maximum_links")
            )
            if any(isinstance(item, bool) or not isinstance(item, int) for item in limits):
                raise ValueError
            return cls(cast(int, limits[0]), cast(int, limits[1]), cast(int, limits[2]))
        except (KeyError, TypeError, ValueError, SpanLimitError) as error:
            raise SpanLimitError("Invalid span limits") from error


class SpanEndHandler(Protocol):
    async def on_end(self, record: SpanRecord) -> None: ...


class NoOpSpanEndHandler:
    async def on_end(self, record: SpanRecord) -> None:
        if not isinstance(record, SpanRecord):
            raise SpanValidationError("Invalid span record")


def _attribute_name(value: object) -> str:
    if not isinstance(value, str):
        raise SpanValidationError("Invalid span attribute name")
    result = value.strip()
    if (
        not result
        or len(result) > 256
        or "\0" in result
        or any(ord(char) < 32 or ord(char) == 127 for char in result)
    ):
        raise SpanValidationError("Invalid span attribute name")
    return result


def _normalize_span_attributes(values: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(values, Mapping):
        raise SpanValidationError("Invalid span attributes")
    normalized: dict[str, object] = {}
    for raw_name, value in values.items():
        name = _attribute_name(raw_name)
        if name in normalized:
            raise SpanValidationError("Duplicate span attribute name")
        normalized[name] = value
    return _attributes(normalized)


def _process_control(error: BaseException) -> bool:
    return isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit))


class ActiveSpan(Protocol):
    @property
    def context(self) -> TraceContext: ...

    @property
    def parent_context(self) -> TraceContext | None: ...

    @property
    def name(self) -> str: ...

    @property
    def source(self) -> TraceSource: ...

    @property
    def kind(self) -> SpanKind: ...

    @property
    def started_at(self) -> datetime: ...

    @property
    def is_recording(self) -> bool: ...

    @property
    def is_ended(self) -> bool: ...

    async def set_attribute(self, name: str, value: object) -> None: ...

    async def set_attributes(self, values: Mapping[str, object]) -> None: ...

    async def add_event(
        self,
        name: str,
        *,
        attributes: Mapping[str, object] | None = None,
        timestamp: datetime | None = None,
    ) -> SpanEvent | None: ...

    async def add_link(self, link: SpanLink) -> bool: ...

    async def set_status(self, status: SpanStatus, message: str | None = None) -> None: ...

    async def record_exception(self, error: BaseException) -> LogException | None: ...

    async def end(self, *, ended_at: datetime | None = None) -> SpanRecord | None: ...


CompletionCallback = Callable[[bool], Awaitable[None]]


class RecordingSpan:
    def __init__(
        self,
        context: TraceContext,
        parent_context: TraceContext | None,
        name: str,
        source: TraceSource,
        kind: SpanKind,
        started_at: datetime,
        attributes: Mapping[str, object],
        links: tuple[SpanLink, ...],
        limits: SpanLimits,
        clock: Callable[[], datetime],
        end_handler: SpanEndHandler,
        completion_callback: CompletionCallback,
    ) -> None:
        self._context = context
        self._parent_context = parent_context
        self._name = name
        self._source = source
        self._kind = kind
        self._started_at = _aware(started_at)
        self._attributes = dict(_attributes(attributes))
        self._links = list(links)
        self._events: list[SpanEvent] = []
        self._status = SpanStatus.UNSET
        self._status_message: str | None = None
        self._exception: LogException | None = None
        self._limits = limits
        self._clock = clock
        self._end_handler = end_handler
        self._completion_callback = completion_callback
        self._lock = asyncio.Lock()
        self._ended = False
        self._record: SpanRecord | None = None
        self._completion: asyncio.Future[None] | None = None

    @property
    def context(self) -> TraceContext:
        return self._context

    @property
    def parent_context(self) -> TraceContext | None:
        return self._parent_context

    @property
    def name(self) -> str:
        return self._name

    @property
    def source(self) -> TraceSource:
        return self._source

    @property
    def kind(self) -> SpanKind:
        return self._kind

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def status(self) -> SpanStatus:
        return self._status

    @property
    def is_recording(self) -> bool:
        return True

    @property
    def is_ended(self) -> bool:
        return self._ended

    def _require_active(self) -> None:
        if self._ended:
            raise SpanStateError("Span is already ended")

    async def set_attribute(self, name: str, value: object) -> None:
        key = _attribute_name(name)
        normalized = _attributes({key: value})[key]
        async with self._lock:
            self._require_active()
            if (
                key not in self._attributes
                and len(self._attributes) >= self._limits.maximum_attributes
            ):
                raise SpanLimitError("Span attribute limit reached")
            self._attributes[key] = normalized

    async def set_attributes(self, values: Mapping[str, object]) -> None:
        safe = dict(_normalize_span_attributes(values))
        async with self._lock:
            self._require_active()
            added = sum(name not in self._attributes for name in safe)
            if len(self._attributes) + added > self._limits.maximum_attributes:
                raise SpanLimitError("Span attribute limit reached")
            self._attributes.update(safe)

    async def add_event(
        self,
        name: str,
        *,
        attributes: Mapping[str, object] | None = None,
        timestamp: datetime | None = None,
    ) -> SpanEvent:
        event = SpanEvent(
            name, _aware(self._clock() if timestamp is None else timestamp), attributes or {}
        )
        async with self._lock:
            self._require_active()
            if event.timestamp < self._started_at or (
                self._events and event.timestamp < self._events[-1].timestamp
            ):
                raise SpanValidationError("Invalid span event timestamp")
            if len(self._events) >= self._limits.maximum_events:
                raise SpanLimitError("Span event limit reached")
            self._events.append(event)
            return event

    async def add_link(self, link: SpanLink) -> bool:
        if not isinstance(link, SpanLink):
            raise SpanValidationError("Invalid span link")
        identity = (link.context.trace_id, link.context.span_id)
        async with self._lock:
            self._require_active()
            if identity == (self._context.trace_id, self._context.span_id):
                raise SpanValidationError("Invalid span self-link")
            if identity in _link_identities(tuple(self._links)):
                raise SpanValidationError("Duplicate span link")
            if len(self._links) >= self._limits.maximum_links:
                raise SpanLimitError("Span link limit reached")
            self._links.append(link)
            return True

    async def set_status(self, status: SpanStatus, message: str | None = None) -> None:
        if not isinstance(status, SpanStatus):
            raise SpanValidationError("Invalid span status")
        safe_message = _text(message, "span status message", 4096, newlines=True)
        async with self._lock:
            self._require_active()
            if self._exception is not None and status in (SpanStatus.OK, SpanStatus.UNSET):
                raise SpanStateError("Span exception requires an error status")
            self._status, self._status_message = status, safe_message

    async def record_exception(self, error: BaseException) -> LogException:
        if _process_control(error):
            raise error
        if not isinstance(error, BaseException):
            raise SpanValidationError("Invalid span exception")
        safe = LogException.from_exception(error)
        async with self._lock:
            self._require_active()
            if self._exception is not None:
                raise SpanStateError("Span exception is already recorded")
            self._exception = safe
            self._status = SpanStatus.ERROR
            if self._status_message is None:
                self._status_message = safe.message
            return safe

    async def end(self, *, ended_at: datetime | None = None) -> SpanRecord:
        timestamp = _aware(self._clock() if ended_at is None else ended_at)
        async with self._lock:
            if self._ended:
                waiting = self._completion
                record = self._record
                owns_completion = False
            else:
                if timestamp < self._started_at or (
                    self._events and timestamp < self._events[-1].timestamp
                ):
                    raise SpanValidationError("Invalid span end timestamp")
                record = SpanRecord(
                    self._context,
                    self._name,
                    self._source,
                    self._kind,
                    self._status,
                    self._started_at,
                    timestamp,
                    self._status_message,
                    dict(self._attributes),
                    tuple(self._events),
                    tuple(self._links),
                    self._exception,
                )
                self._record = record
                self._ended = True
                waiting = asyncio.get_running_loop().create_future()
                self._completion = waiting
                owns_completion = True
        if record is None:
            raise SpanStateError("Span completion is unavailable")
        if waiting is None:
            raise SpanStateError("Span completion is unavailable")
        if owns_completion:
            try:
                await self._end_handler.on_end(record)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit):
                await self._completion_callback(True)
                waiting.set_result(None)
                raise
            except Exception as error:
                await self._completion_callback(True)
                waiting.set_result(None)
                raise SpanCompletionError("Span completion failed") from error
            else:
                await self._completion_callback(False)
                waiting.set_result(None)
        elif waiting is not None:
            await waiting
        return record


class NonRecordingSpan:
    def __init__(
        self,
        context: TraceContext,
        parent_context: TraceContext | None,
        name: str,
        source: TraceSource,
        kind: SpanKind,
        started_at: datetime,
        clock: Callable[[], datetime],
        completion_callback: Callable[[], Awaitable[None]],
    ) -> None:
        self._context = context
        self._parent_context = parent_context
        self._name = name
        self._source = source
        self._kind = kind
        self._started_at = started_at
        self._clock = clock
        self._completion_callback = completion_callback
        self._ended = False
        self._lock = asyncio.Lock()

    @property
    def context(self) -> TraceContext:
        return self._context

    @property
    def parent_context(self) -> TraceContext | None:
        return self._parent_context

    @property
    def name(self) -> str:
        return self._name

    @property
    def source(self) -> TraceSource:
        return self._source

    @property
    def kind(self) -> SpanKind:
        return self._kind

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def is_ended(self) -> bool:
        return self._ended

    async def _active(self) -> None:
        async with self._lock:
            if self._ended:
                raise SpanStateError("Span is already ended")

    async def set_attribute(self, name: str, value: object) -> None:
        _attribute_name(name)
        await self._active()

    async def set_attributes(self, values: Mapping[str, object]) -> None:
        if not isinstance(values, Mapping):
            raise SpanValidationError("Invalid span attributes")
        for name in values:
            _attribute_name(name)
        await self._active()

    async def add_event(
        self,
        name: str,
        *,
        attributes: Mapping[str, object] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        SpanEvent(name, _aware(self._clock() if timestamp is None else timestamp), attributes or {})
        await self._active()
        return None

    async def add_link(self, link: SpanLink) -> bool:
        if not isinstance(link, SpanLink):
            raise SpanValidationError("Invalid span link")
        await self._active()
        return False

    async def set_status(self, status: SpanStatus, message: str | None = None) -> None:
        if not isinstance(status, SpanStatus):
            raise SpanValidationError("Invalid span status")
        _text(message, "span status message", 4096, newlines=True)
        await self._active()

    async def record_exception(self, error: BaseException) -> None:
        if _process_control(error):
            raise error
        await self._active()
        return None

    async def end(self, *, ended_at: datetime | None = None) -> None:
        if ended_at is not None:
            _aware(ended_at)
        async with self._lock:
            first = not self._ended
            self._ended = True
        if first:
            await self._completion_callback()
        return None


@dataclass(frozen=True, slots=True)
class TracerStatus:
    started_count: int
    recording_count: int
    dropped_count: int
    completed_count: int
    failed_completion_count: int
    active_count: int
    closed: bool
    created_at: datetime

    def __post_init__(self) -> None:
        counts = (
            self.started_count,
            self.recording_count,
            self.dropped_count,
            self.completed_count,
            self.failed_completion_count,
            self.active_count,
        )
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in counts
            )
            or self.recording_count + self.dropped_count != self.started_count
            or self.completed_count > self.recording_count
            or self.failed_completion_count > self.completed_count
            or self.active_count > self.started_count
            or not isinstance(self.closed, bool)
        ):
            raise SpanStateError("Invalid tracer status")
        _aware(self.created_at)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "started_count": self.started_count,
            "recording_count": self.recording_count,
            "dropped_count": self.dropped_count,
            "completed_count": self.completed_count,
            "failed_completion_count": self.failed_completion_count,
            "active_count": self.active_count,
            "closed": self.closed,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("closed"), bool)
                or not isinstance(value.get("created_at"), str)
            ):
                raise ValueError
            names = (
                "started_count",
                "recording_count",
                "dropped_count",
                "completed_count",
                "failed_completion_count",
                "active_count",
            )
            counts = tuple(value[name] for name in names)
            if any(isinstance(item, bool) or not isinstance(item, int) for item in counts):
                raise ValueError
            return cls(
                cast(int, counts[0]),
                cast(int, counts[1]),
                cast(int, counts[2]),
                cast(int, counts[3]),
                cast(int, counts[4]),
                cast(int, counts[5]),
                cast(bool, value["closed"]),
                datetime.fromisoformat(cast(str, value["created_at"])),
            )
        except (KeyError, TypeError, ValueError, SpanStateError) as error:
            raise SpanStateError("Invalid tracer status") from error


class Tracer(Protocol):
    @property
    def source(self) -> TraceSource: ...

    async def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: TraceContext | None = None,
        root: bool = False,
        attributes: Mapping[str, object] | None = None,
        links: tuple[SpanLink, ...] = (),
        started_at: datetime | None = None,
        trace_id: TraceId | None = None,
        span_id: SpanId | None = None,
    ) -> ActiveSpan: ...

    def start_as_current_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: TraceContext | None = None,
        root: bool = False,
        attributes: Mapping[str, object] | None = None,
        links: tuple[SpanLink, ...] = (),
        started_at: datetime | None = None,
        trace_id: TraceId | None = None,
        span_id: SpanId | None = None,
    ) -> AbstractAsyncContextManager[ActiveSpan]: ...

    async def status(self) -> TracerStatus: ...

    async def close(self) -> None: ...


class DefaultTracer:
    def __init__(
        self,
        source: TraceSource,
        *,
        sampler: Sampler | None = None,
        end_handler: SpanEndHandler | None = None,
        limits: SpanLimits | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(source, TraceSource) or not callable(clock):
            raise SpanValidationError("Invalid tracer configuration")
        self._source = source
        self._sampler = sampler if sampler is not None else AlwaysOnSampler()
        self._end_handler = end_handler if end_handler is not None else NoOpSpanEndHandler()
        self._limits = limits if limits is not None else SpanLimits()
        if not isinstance(self._limits, SpanLimits):
            raise SpanLimitError("Invalid span limits")
        self._clock = clock
        self._created_at = _aware(clock())
        self._lock = asyncio.Lock()
        self._closed = False
        self._started = self._recording = self._dropped = 0
        self._completed = self._failed = self._active = 0

    @property
    def source(self) -> TraceSource:
        return self._source

    async def _on_complete(self, failed: bool) -> None:
        async with self._lock:
            self._completed += 1
            if failed:
                self._failed += 1
            self._active -= 1

    async def _on_drop_end(self) -> None:
        async with self._lock:
            self._active -= 1

    async def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: TraceContext | None = None,
        root: bool = False,
        attributes: Mapping[str, object] | None = None,
        links: tuple[SpanLink, ...] = (),
        started_at: datetime | None = None,
        trace_id: TraceId | None = None,
        span_id: SpanId | None = None,
    ) -> ActiveSpan:
        if not isinstance(root, bool) or not isinstance(kind, SpanKind):
            raise SpanValidationError("Invalid span configuration")
        if parent is not None and not isinstance(parent, TraceContext):
            raise TraceContextError("Invalid trace context")
        if root and parent is not None:
            raise TraceContextError("Root span cannot have a parent")
        resolved_parent = (
            None if root else (parent if parent is not None else current_trace_context())
        )
        is_root = resolved_parent is None
        if trace_id is not None and not isinstance(trace_id, TraceId):
            raise TraceContextError("Invalid trace identifier")
        if not is_root and trace_id is not None:
            raise TraceContextError("Child span cannot specify a trace identifier")
        if span_id is not None and not isinstance(span_id, SpanId):
            raise TraceContextError("Invalid span identifier")
        active_trace_id = (
            trace_id
            if trace_id is not None
            else (resolved_parent.trace_id if resolved_parent else TraceId())
        )
        active_span_id = span_id if span_id is not None else SpanId()
        if resolved_parent is not None and active_span_id == resolved_parent.span_id:
            raise TraceContextError("Child span cannot equal parent span")
        start = _aware(self._clock() if started_at is None else started_at)
        initial_attributes = _normalize_span_attributes(
            attributes if attributes is not None else {}
        )
        request = SamplingRequest(
            active_trace_id, resolved_parent, name, self._source, kind, initial_attributes, links
        )
        try:
            result = await self._sampler.should_sample(request)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except SamplingError:
            raise
        except Exception as error:
            raise SamplingError("Sampling failed") from error
        if not isinstance(result, SamplingResult):
            raise SamplingError("Invalid sampling result")
        merged_attributes = {**request.attributes, **result.attributes}
        if result.is_recording:
            if len(merged_attributes) > self._limits.maximum_attributes:
                raise SpanLimitError("Span attribute limit reached")
            if len(request.links) > self._limits.maximum_links:
                raise SpanLimitError("Span link limit reached")
        if resolved_parent is None:
            context = TraceContext.create_root(
                trace_id=active_trace_id, span_id=active_span_id, sampled=result.is_sampled
            )
        else:
            context = resolved_parent.create_child(
                span_id=active_span_id, sampled=result.is_sampled
            )
        identities = _link_identities(request.links)
        if (context.trace_id, context.span_id) in identities:
            raise SpanValidationError("Invalid span self-link")
        if result.is_recording:
            span: ActiveSpan = RecordingSpan(
                context,
                resolved_parent,
                request.name,
                self._source,
                kind,
                start,
                merged_attributes,
                request.links,
                self._limits,
                self._clock,
                self._end_handler,
                self._on_complete,
            )
        else:
            span = NonRecordingSpan(
                context,
                resolved_parent,
                request.name,
                self._source,
                kind,
                start,
                self._clock,
                self._on_drop_end,
            )
        async with self._lock:
            if self._closed:
                raise TracerClosedError("Tracer is closed")
            self._started += 1
            self._active += 1
            if result.is_recording:
                self._recording += 1
            else:
                self._dropped += 1
        return span

    def start_as_current_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: TraceContext | None = None,
        root: bool = False,
        attributes: Mapping[str, object] | None = None,
        links: tuple[SpanLink, ...] = (),
        started_at: datetime | None = None,
        trace_id: TraceId | None = None,
        span_id: SpanId | None = None,
    ) -> AbstractAsyncContextManager[ActiveSpan]:
        return _SpanScope(
            self, name, kind, parent, root, attributes, links, started_at, trace_id, span_id
        )

    async def status(self) -> TracerStatus:
        async with self._lock:
            return TracerStatus(
                self._started,
                self._recording,
                self._dropped,
                self._completed,
                self._failed,
                self._active,
                self._closed,
                self._created_at,
            )

    async def close(self) -> None:
        async with self._lock:
            self._closed = True


class _SpanScope(AbstractAsyncContextManager[ActiveSpan]):
    def __init__(
        self,
        tracer: DefaultTracer,
        name: str,
        kind: SpanKind,
        parent: TraceContext | None,
        root: bool,
        attributes: Mapping[str, object] | None,
        links: tuple[SpanLink, ...],
        started_at: datetime | None,
        trace_id: TraceId | None,
        span_id: SpanId | None,
    ) -> None:
        self._tracer = tracer
        self._name = name
        self._kind = kind
        self._parent = parent
        self._root = root
        self._attributes = attributes
        self._links = links
        self._started_at = started_at
        self._trace_id = trace_id
        self._span_id = span_id
        self._span: ActiveSpan | None = None
        self._binding: _Binding | None = None

    async def __aenter__(self) -> ActiveSpan:
        self._span = await self._tracer.start_span(
            self._name,
            kind=self._kind,
            parent=self._parent,
            root=self._root,
            attributes=self._attributes,
            links=self._links,
            started_at=self._started_at,
            trace_id=self._trace_id,
            span_id=self._span_id,
        )
        self._binding = bind_trace_context(self._span.context)
        await self._binding.__aenter__()
        return self._span

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        span, binding = self._span, self._binding
        if span is None or binding is None:
            raise SpanStateError("Span scope is not active")
        try:
            if span.is_ended:
                return False
            if exc is None:
                if isinstance(span, RecordingSpan) and span.status is SpanStatus.UNSET:
                    await span.set_status(SpanStatus.OK)
                await span.end()
            elif isinstance(exc, TimeoutError):
                await span.set_status(SpanStatus.TIMEOUT, "Span timed out")
                try:
                    await span.end()
                except SpanCompletionError:
                    pass
            elif isinstance(exc, asyncio.CancelledError):
                await span.set_status(SpanStatus.CANCELLED)
                try:
                    await span.end()
                except SpanCompletionError:
                    pass
            elif isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                await span.set_status(SpanStatus.ERROR)
                try:
                    await span.end()
                except SpanCompletionError:
                    pass
            elif isinstance(exc, BaseException):
                try:
                    await span.record_exception(exc)
                    await span.end()
                except SpanCompletionError:
                    pass
            return False
        finally:
            await binding.__aexit__(exc_type, exc, traceback)
