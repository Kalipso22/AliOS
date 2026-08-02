"""Provider-neutral structured logging primitives; global logging configuration is out of scope."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, Self

from alios_core.errors import AliOSError, LogSerializationError, LogSinkError, ValidationError
from alios_core.ids import (
    AgentId,
    CorrelationId,
    LogRecordId,
    RunId,
    TaskId,
    TenantId,
    UserId,
    WorkflowId,
)
from alios_core.types import JsonValue, utc_now

_CURRENT: ContextVar[LogContext | None] = ContextVar("alios_log_context", default=None)
_IDENTIFIERS = (CorrelationId, RunId, TenantId, UserId, TaskId, AgentId, WorkflowId, LogRecordId)


def _validate_text(value: str | None, field_name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValidationError(f"{field_name} is required")
        return None
    result = value.strip()
    if (required and not result) or len(result) > 256 or any(ord(item) < 32 for item in result):
        raise ValidationError(f"Invalid {field_name}")
    return result


def _normal(value: object, *, depth: int = 0, seen: set[int] | None = None) -> JsonValue:
    if depth > 16:
        raise LogSerializationError("Logging value nesting is too deep")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise LogSerializationError("Logging datetimes must be timezone-aware")
        return value.isoformat()
    if isinstance(value, _IDENTIFIERS):
        return str(value)
    visited = seen if seen is not None else set()
    if isinstance(value, Mapping):
        if id(value) in visited:
            raise LogSerializationError("Logging value contains a cycle")
        visited.add(id(value))
        try:
            if len(value) > 10_000:
                raise LogSerializationError("Logging mapping is too large")
            return {
                str(key): _normal(item, depth=depth + 1, seen=visited)
                for key, item in value.items()
            }
        finally:
            visited.remove(id(value))
    if isinstance(value, (list, tuple)):
        if id(value) in visited:
            raise LogSerializationError("Logging value contains a cycle")
        visited.add(id(value))
        try:
            if len(value) > 10_000:
                raise LogSerializationError("Logging sequence is too large")
            return [_normal(item, depth=depth + 1, seen=visited) for item in value]
        finally:
            visited.remove(id(value))
    raise LogSerializationError("Logging value is not JSON-compatible")


def _freeze(value: JsonValue) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _attributes(value: Mapping[str, object] | None) -> Mapping[str, object]:
    normalized = _normal(value or {})
    if not isinstance(normalized, dict):
        raise LogSerializationError("Logging attributes must be a mapping")
    return MappingProxyType({key: _freeze(item) for key, item in normalized.items()})


class LogLevel(StrEnum):
    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def severity(self) -> int:
        return list(LogLevel).index(self)

    @classmethod
    def parse(cls, value: str) -> Self:
        try:
            return cls(value.strip().lower())
        except ValueError as error:
            raise ValidationError("Invalid log level") from error


class RedactionAction(StrEnum):
    MASK = "mask"
    REMOVE = "remove"
    HASH = "hash"


class LogDropPolicy(StrEnum):
    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    RAISE = "raise"


@dataclass(frozen=True, slots=True)
class LogSource:
    component: str
    module: str | None = None
    operation: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component", _validate_text(self.component, "component", required=True)
        )
        object.__setattr__(self, "module", _validate_text(self.module, "module"))
        object.__setattr__(self, "operation", _validate_text(self.operation, "operation"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"component": self.component, "module": self.module, "operation": self.operation}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        component = value.get("component")
        module = value.get("module")
        operation = value.get("operation")
        return cls(
            component if isinstance(component, str) else "",
            module if isinstance(module, str) else None,
            operation if isinstance(operation, str) else None,
        )


@dataclass(frozen=True, slots=True)
class LogContext:
    correlation_id: CorrelationId | None = None
    run_id: RunId | None = None
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    task_id: TaskId | None = None
    agent_id: AgentId | None = None
    workflow_id: WorkflowId | None = None
    parent_log_record_id: LogRecordId | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    def with_attributes(self, attributes: Mapping[str, object]) -> Self:
        return replace(self, attributes={**dict(self.attributes), **attributes})

    def to_dict(self) -> dict[str, object]:
        fields = (
            "correlation_id",
            "run_id",
            "tenant_id",
            "user_id",
            "task_id",
            "agent_id",
            "workflow_id",
            "parent_log_record_id",
        )
        output: dict[str, object] = {
            name: str(item) if (item := getattr(self, name)) else None for name in fields
        }
        output["attributes"] = dict(self.attributes)
        return output

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        attributes = value.get("attributes", {})
        return cls(
            CorrelationId(str(value["correlation_id"])) if value.get("correlation_id") else None,
            RunId(str(value["run_id"])) if value.get("run_id") else None,
            TenantId(str(value["tenant_id"])) if value.get("tenant_id") else None,
            UserId(str(value["user_id"])) if value.get("user_id") else None,
            TaskId(str(value["task_id"])) if value.get("task_id") else None,
            AgentId(str(value["agent_id"])) if value.get("agent_id") else None,
            WorkflowId(str(value["workflow_id"])) if value.get("workflow_id") else None,
            LogRecordId(str(value["parent_log_record_id"]))
            if value.get("parent_log_record_id")
            else None,
            attributes if isinstance(attributes, Mapping) else {},
        )


class _Binding(AbstractContextManager[None], AbstractAsyncContextManager[None]):
    def __init__(self, context: LogContext) -> None:
        self._context = context
        self._token: Token[LogContext | None] | None = None

    def __enter__(self) -> None:
        self._token = _CURRENT.set(self._context)

    def __exit__(self, *_: object) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(self, *_: object) -> None:
        self.__exit__()


def current_log_context() -> LogContext | None:
    return _CURRENT.get()


def bind_log_context(context: LogContext) -> _Binding:
    return _Binding(context)


def merge_current_log_context(context: LogContext | None) -> LogContext | None:
    current = current_log_context()
    if current is None:
        return context
    if context is None:
        return current
    names = (
        "correlation_id",
        "run_id",
        "tenant_id",
        "user_id",
        "task_id",
        "agent_id",
        "workflow_id",
        "parent_log_record_id",
    )
    identifiers = {name: getattr(context, name) or getattr(current, name) for name in names}
    return LogContext(
        **identifiers,
        attributes={**dict(current.attributes), **dict(context.attributes)},
    )


@dataclass(frozen=True, slots=True)
class RedactionRule:
    name: str
    action: RedactionAction = RedactionAction.MASK
    keys: frozenset[str] = field(default_factory=frozenset)
    path_patterns: tuple[str, ...] = ()
    value_patterns: tuple[str, ...] = ()
    replacement: str = "[REDACTED]"
    case_sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_text(self.name, "rule name", required=True))
        object.__setattr__(
            self, "keys", frozenset(item.strip() for item in self.keys if item.strip())
        )
        object.__setattr__(self, "path_patterns", tuple(self.path_patterns))
        object.__setattr__(self, "value_patterns", tuple(self.value_patterns))
        if not (self.keys or self.path_patterns or self.value_patterns):
            raise ValidationError("Redaction rule requires a matcher")
        if any(ord(item) < 32 for item in self.replacement):
            raise ValidationError("Invalid redaction replacement")
        try:
            for pattern in self.value_patterns:
                re.compile(pattern)
        except re.error as error:
            raise ValidationError("Invalid redaction pattern") from error

    def matches(self, key: str | None, path: tuple[str, ...], value: JsonValue) -> bool:
        normalize = (lambda item: item) if self.case_sensitive else str.lower
        if key is not None and any(normalize(key) == normalize(item) for item in self.keys):
            return True
        for pattern in self.path_patterns:
            parts = pattern.split(".")
            if parts[-1:] == ["**"]:
                if len(path) >= len(parts) - 1 and all(
                    item == "*" or item == part
                    for item, part in zip(parts[:-1], path, strict=False)
                ):
                    return True
            elif len(parts) == len(path) and all(
                item == "*" or item == part for item, part in zip(parts, path, strict=True)
            ):
                return True
        return isinstance(value, str) and any(
            re.search(pattern, value) for pattern in self.value_patterns
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "action": self.action.value,
            "keys": sorted(self.keys),
            "path_patterns": list(self.path_patterns),
            "value_patterns": list(self.value_patterns),
            "replacement": self.replacement,
            "case_sensitive": self.case_sensitive,
        }


_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "session",
        "session_id",
        "private_key",
        "credential",
        "credentials",
        "api-key",
        "access-token",
        "refresh-token",
        "client-secret",
        "private-key",
    }
)


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    rules: tuple[RedactionRule, ...] = ()
    include_default_rules: bool = True
    maximum_depth: int = 16
    maximum_items: int = 10_000
    maximum_string_length: int = 16_384

    def __post_init__(self) -> None:
        if min(self.maximum_depth, self.maximum_items, self.maximum_string_length) < 1:
            raise ValidationError("Invalid redaction limits")
        object.__setattr__(self, "rules", tuple(self.rules))

    def redact(self, value: object) -> JsonValue:
        rules = (
            (RedactionRule("default-sensitive", keys=_SENSITIVE_KEYS),)
            if self.include_default_rules
            else ()
        ) + self.rules

        def render(item: object, path: tuple[str, ...], key: str | None, depth: int) -> JsonValue:
            if depth > self.maximum_depth:
                raise LogSerializationError("Logging value nesting is too deep")
            normal = _normal(item)
            rule = next(
                (candidate for candidate in rules if candidate.matches(key, path, normal)), None
            )
            if rule is not None:
                if rule.action is RedactionAction.MASK:
                    return rule.replacement
                if rule.action is RedactionAction.HASH:
                    payload = json.dumps(_thaw(normal), sort_keys=True, separators=(",", ":"))
                    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
                return "[REMOVED]"
            if isinstance(normal, dict):
                output: dict[str, JsonValue] = {}
                for name, child in normal.items():
                    remove = next(
                        (
                            candidate
                            for candidate in rules
                            if candidate.matches(name, path + (name,), child)
                        ),
                        None,
                    )
                    if remove is not None and remove.action is RedactionAction.REMOVE:
                        continue
                    output[name] = render(child, path + (name,), name, depth + 1)
                return output
            if isinstance(normal, list):
                return [
                    render(child, path + (str(index),), None, depth + 1)
                    for index, child in enumerate(normal)
                ]
            return normal[: self.maximum_string_length] if isinstance(normal, str) else normal

        return render(value, (), None, 0)


def default_redaction_policy() -> RedactionPolicy:
    return RedactionPolicy()


@dataclass(frozen=True, slots=True)
class LogException:
    error_type: str
    error_code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _attributes(self.details))

    @classmethod
    def from_exception(cls, error: Exception) -> Self:
        if isinstance(error, AliOSError):
            return cls(type(error).__name__, error.code, error.message, error.details)
        return cls(type(error).__name__, "unhandled_exception", "An unexpected error occurred")

    def to_dict(self, policy: RedactionPolicy) -> dict[str, JsonValue]:
        return {
            "error_type": self.error_type,
            "error_code": self.error_code,
            "message": policy.redact(self.message),
            "details": policy.redact(self.details),
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class LogRecord:
    level: LogLevel
    message: str
    source: LogSource
    context: LogContext = field(default_factory=LogContext)
    attributes: Mapping[str, object] = field(default_factory=dict)
    exception: LogException | None = None
    sequence: int | None = None
    record_id: LogRecordId = field(default_factory=LogRecordId)
    timestamp: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        message = self.message.strip()
        if not message or len(message) > 16_384 or "\0" in message:
            raise ValidationError("Invalid log message")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValidationError("Log timestamp must be timezone-aware")
        if self.sequence is not None and self.sequence < 0:
            raise ValidationError("Log sequence cannot be negative")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "record_id": str(self.record_id),
            "timestamp": self.timestamp.isoformat(),
            "level": self.level.value,
            "message": policy.redact(self.message),
            "source": self.source.to_dict(),
            "context": policy.redact(self.context.to_dict()),
            "attributes": policy.redact(self.attributes),
            "exception": self.exception.to_dict(policy) if self.exception else None,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        timestamp = value.get("timestamp")
        source = value.get("source")
        context = value.get("context")
        attributes = value.get("attributes", {})
        sequence = value.get("sequence")
        if (
            not isinstance(timestamp, str)
            or not isinstance(source, Mapping)
            or not isinstance(context, Mapping)
        ):
            raise LogSerializationError("Invalid log record")
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise LogSerializationError("Invalid log record timestamp")
        try:
            return cls(
                LogLevel.parse(str(value["level"])),
                str(value["message"]),
                LogSource.from_dict(source),
                LogContext.from_dict(context),
                attributes if isinstance(attributes, Mapping) else {},
                None,
                int(sequence) if isinstance(sequence, (int, str)) else None,
                LogRecordId(str(value["record_id"])),
                parsed,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise LogSerializationError("Invalid log record") from error


@dataclass(frozen=True, slots=True)
class LogFilter:
    minimum_level: LogLevel | None = None
    maximum_level: LogLevel | None = None
    levels: frozenset[LogLevel] | None = None
    components: frozenset[str] | None = None
    correlation_id: CorrelationId | None = None
    run_id: RunId | None = None
    limit: int | None = None
    offset: int = 0

    def __post_init__(self) -> None:
        if (self.limit is not None and self.limit < 0) or self.offset < 0:
            raise ValidationError("Invalid log filter")

    def matches(self, record: LogRecord) -> bool:
        return (
            (self.minimum_level is None or record.level.severity >= self.minimum_level.severity)
            and (self.maximum_level is None or record.level.severity <= self.maximum_level.severity)
            and (self.levels is None or record.level in self.levels)
            and (self.components is None or record.source.component in self.components)
            and (
                self.correlation_id is None or record.context.correlation_id == self.correlation_id
            )
            and (self.run_id is None or record.context.run_id == self.run_id)
        )


class LogSink(Protocol):
    async def emit(self, record: LogRecord) -> None: ...
    async def emit_many(self, records: Sequence[LogRecord]) -> None: ...
    async def flush(self) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LogSinkSnapshot:
    stored_count: int
    capacity: int
    accepted_count: int
    dropped_count: int
    failed_count: int
    closed: bool
    created_at: datetime = field(default_factory=utc_now)


class InMemoryLogSink:
    def __init__(
        self,
        capacity: int = 10_000,
        drop_policy: LogDropPolicy = LogDropPolicy.DROP_OLDEST,
        redaction_policy: RedactionPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if capacity < 1:
            raise ValidationError("Log sink capacity must be positive")
        self.capacity = capacity
        self.drop_policy = drop_policy
        self.redaction_policy = redaction_policy or default_redaction_policy()
        self._clock = clock
        self._records: list[LogRecord] = []
        self._lock = asyncio.Lock()
        self._closed = False
        self._accepted = self._dropped = self._failed = 0

    async def emit(self, record: LogRecord) -> None:
        await self.emit_many((record,))

    async def emit_many(self, records: Sequence[LogRecord]) -> None:
        async with self._lock:
            if self._closed:
                raise LogSinkError("Log sink is closed")
            for record in records:
                if any(item.record_id == record.record_id for item in self._records):
                    self._failed += 1
                    raise LogSinkError("Duplicate log record identity")
                if len(self._records) >= self.capacity:
                    if self.drop_policy is LogDropPolicy.DROP_NEWEST:
                        self._dropped += 1
                        continue
                    if self.drop_policy is LogDropPolicy.RAISE:
                        self._failed += 1
                        raise LogSinkError("Log sink capacity exceeded")
                    self._records.pop(0)
                    self._dropped += 1
                self._records.append(record)
                self._accepted += 1

    async def get_optional(self, record_id: LogRecordId) -> LogRecord | None:
        async with self._lock:
            return next((item for item in self._records if item.record_id == record_id), None)

    async def get(self, record_id: LogRecordId) -> LogRecord:
        record = await self.get_optional(record_id)
        if record is None:
            raise LogSinkError("Log record was not found")
        return record

    async def list(self, filter: LogFilter | None = None) -> tuple[LogRecord, ...]:
        async with self._lock:
            values = tuple(self._records)
        selected = tuple(item for item in values if filter is None or filter.matches(item))
        selected = tuple(sorted(selected, key=lambda item: (item.timestamp, str(item.record_id))))
        if filter is None:
            return selected
        return selected[
            filter.offset : None if filter.limit is None else filter.offset + filter.limit
        ]

    async def snapshot(self) -> LogSinkSnapshot:
        async with self._lock:
            return LogSinkSnapshot(
                len(self._records),
                self.capacity,
                self._accepted,
                self._dropped,
                self._failed,
                self._closed,
                self._clock(),
            )

    async def flush(self) -> None:
        if self._closed:
            raise LogSinkError("Log sink is closed")

    async def close(self) -> None:
        async with self._lock:
            self._closed = True


class _Sequences:
    def __init__(self) -> None:
        self._value = 0
        self._lock = asyncio.Lock()

    async def next(self) -> int:
        async with self._lock:
            self._value += 1
            return self._value


class StructuredLogger:
    def __init__(
        self,
        *,
        component: str,
        sink: LogSink,
        minimum_level: LogLevel = LogLevel.INFO,
        source_module: str | None = None,
        base_context: LogContext | None = None,
        base_attributes: Mapping[str, object] | None = None,
        redaction_policy: RedactionPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        _sequences: _Sequences | None = None,
    ) -> None:
        self.component = _validate_text(component, "component", required=True)
        self.sink = sink
        self.minimum_level = minimum_level
        self.source_module = _validate_text(source_module, "module")
        self.base_context = base_context or LogContext()
        self.base_attributes = _attributes(base_attributes)
        self.redaction_policy = redaction_policy or default_redaction_policy()
        self._clock = clock
        self._sequences = _sequences or _Sequences()

    async def log(
        self,
        level: LogLevel,
        message: str,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
        exception: LogException | None = None,
    ) -> LogRecord | None:
        if level.severity < self.minimum_level.severity:
            return None
        merged = merge_current_log_context(self.base_context)
        merged = merge_current_log_context(context) if context else merged
        bound = current_log_context() or LogContext()
        values = {
            **dict(bound.attributes),
            **dict(self.base_context.attributes),
            **dict(self.base_attributes),
            **(dict(context.attributes) if context else {}),
            **(attributes or {}),
        }
        record = LogRecord(
            level,
            message,
            LogSource(self.component or "", self.source_module, operation),
            merged or LogContext(),
            values,
            exception,
            await self._sequences.next(),
            timestamp=self._clock(),
        )
        try:
            await self.sink.emit(record)
        except AliOSError:
            raise
        except Exception as error:
            raise LogSinkError("Log sink emission failed", cause=error) from error
        return record

    async def _emit_at(
        self,
        level: LogLevel,
        message: str,
        operation: str | None,
        context: LogContext | None,
        attributes: Mapping[str, object] | None,
    ) -> LogRecord | None:
        return await self.log(
            level, message, operation=operation, context=context, attributes=attributes
        )

    async def trace(
        self,
        message: str,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> LogRecord | None:
        return await self._emit_at(LogLevel.TRACE, message, operation, context, attributes)

    async def debug(
        self,
        message: str,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> LogRecord | None:
        return await self._emit_at(LogLevel.DEBUG, message, operation, context, attributes)

    async def info(
        self,
        message: str,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> LogRecord | None:
        return await self._emit_at(LogLevel.INFO, message, operation, context, attributes)

    async def warning(
        self,
        message: str,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> LogRecord | None:
        return await self._emit_at(LogLevel.WARNING, message, operation, context, attributes)

    async def error(
        self,
        message: str,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> LogRecord | None:
        return await self._emit_at(LogLevel.ERROR, message, operation, context, attributes)

    async def critical(
        self,
        message: str,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> LogRecord | None:
        return await self._emit_at(LogLevel.CRITICAL, message, operation, context, attributes)

    async def exception(
        self,
        message: str,
        error: Exception,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> LogRecord | None:
        return await self.log(
            LogLevel.ERROR,
            message,
            operation=operation,
            context=context,
            attributes=attributes,
            exception=LogException.from_exception(error),
        )

    def bind(
        self,
        *,
        module: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> StructuredLogger:
        return StructuredLogger(
            component=self.component or "",
            sink=self.sink,
            minimum_level=self.minimum_level,
            source_module=module or self.source_module,
            base_context=merge_current_log_context(context) or self.base_context,
            base_attributes={**dict(self.base_attributes), **(attributes or {})},
            redaction_policy=self.redaction_policy,
            clock=self._clock,
            _sequences=self._sequences,
        )

    child = bind


class LoggerFactory:
    def __init__(
        self,
        sink: LogSink | None = None,
        *,
        minimum_level: LogLevel = LogLevel.INFO,
        redaction_policy: RedactionPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        owns_sink: bool = False,
    ) -> None:
        self.sink = sink or InMemoryLogSink()
        self.minimum_level = minimum_level
        self.redaction_policy = redaction_policy or default_redaction_policy()
        self.clock = clock
        self.owns_sink = owns_sink or sink is None
        self._closed = False

    def get_logger(
        self,
        component: str,
        *,
        module: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> StructuredLogger:
        return StructuredLogger(
            component=component,
            sink=self.sink,
            minimum_level=self.minimum_level,
            source_module=module,
            base_context=context,
            base_attributes=attributes,
            redaction_policy=self.redaction_policy,
            clock=self.clock,
        )

    async def close(self) -> None:
        if not self._closed and self.owns_sink:
            await self.sink.close()
        self._closed = True
