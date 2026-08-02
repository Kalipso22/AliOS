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

from alios_core.errors import (
    AliOSError,
    LogSerializationError,
    LogSinkError,
    ResourceConflictError,
    ResourceNotFoundError,
    ValidationError,
)
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


def _filter_texts(values: frozenset[str] | None, field_name: str) -> frozenset[str] | None:
    if values is None:
        return None
    normalized_values: set[str] = set()
    for value in values:
        normalized_value = _validate_text(value, field_name, required=True)
        if normalized_value is None:
            raise ValidationError(f"{field_name} is required")
        normalized_values.add(normalized_value)
    normalized = frozenset(normalized_values)
    return normalized or None


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Expected an integer")
    return int(value)


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
    modules: frozenset[str] | None = None
    operations: frozenset[str] | None = None
    correlation_id: CorrelationId | None = None
    run_id: RunId | None = None
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    record_ids: frozenset[LogRecordId] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    message_contains: str | None = None
    attribute_equals: Mapping[str, object] = field(default_factory=dict)
    limit: int | None = None
    offset: int = 0

    def __post_init__(self) -> None:
        if (self.limit is not None and self.limit < 0) or self.offset < 0:
            raise ValidationError("Invalid log filter")
        if any(
            level is not None and not isinstance(level, LogLevel)
            for level in (self.minimum_level, self.maximum_level)
        ):
            raise ValidationError("Invalid log level")
        if (
            self.minimum_level
            and self.maximum_level
            and self.minimum_level.severity > self.maximum_level.severity
        ):
            raise ValidationError("Invalid log level range")
        identifier_types: tuple[tuple[object | None, type[object]], ...] = (
            (self.correlation_id, CorrelationId),
            (self.run_id, RunId),
            (self.tenant_id, TenantId),
            (self.user_id, UserId),
        )
        if any(
            item is not None and not isinstance(item, expected)
            for item, expected in identifier_types
        ):
            raise ValidationError("Invalid log filter identifier")
        for value in (self.created_after, self.created_before):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValidationError("Log filter times must be timezone-aware")
        if self.created_after and self.created_before and self.created_after >= self.created_before:
            raise ValidationError("Invalid log time range")
        if self.message_contains is not None:
            object.__setattr__(
                self,
                "message_contains",
                _validate_text(self.message_contains, "message query", required=True),
            )
        object.__setattr__(self, "components", _filter_texts(self.components, "component"))
        object.__setattr__(self, "modules", _filter_texts(self.modules, "module"))
        object.__setattr__(self, "operations", _filter_texts(self.operations, "operation"))
        levels = frozenset(self.levels or ()) or None
        if levels is not None and not all(isinstance(level, LogLevel) for level in levels):
            raise ValidationError("Invalid log levels")
        record_ids = frozenset(self.record_ids or ()) or None
        if record_ids is not None and not all(
            isinstance(record_id, LogRecordId) for record_id in record_ids
        ):
            raise ValidationError("Invalid log record identifiers")
        object.__setattr__(self, "levels", levels)
        object.__setattr__(self, "record_ids", record_ids)
        object.__setattr__(self, "attribute_equals", _attributes(self.attribute_equals))

    def matches(self, record: LogRecord) -> bool:
        return (
            (self.minimum_level is None or record.level.severity >= self.minimum_level.severity)
            and (self.maximum_level is None or record.level.severity <= self.maximum_level.severity)
            and (self.levels is None or record.level in self.levels)
            and (self.components is None or record.source.component in self.components)
            and (self.modules is None or record.source.module in self.modules)
            and (self.operations is None or record.source.operation in self.operations)
            and (
                self.correlation_id is None or record.context.correlation_id == self.correlation_id
            )
            and (self.run_id is None or record.context.run_id == self.run_id)
            and (self.tenant_id is None or record.context.tenant_id == self.tenant_id)
            and (self.user_id is None or record.context.user_id == self.user_id)
            and (self.record_ids is None or record.record_id in self.record_ids)
            and (self.created_after is None or record.timestamp > self.created_after)
            and (self.created_before is None or record.timestamp < self.created_before)
            and (self.message_contains is None or self.message_contains in record.message)
            and all(
                ({**dict(record.context.attributes), **dict(record.attributes)}).get(key) == value
                for key, value in self.attribute_equals.items()
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "minimum_level": self.minimum_level.value if self.minimum_level else None,
            "maximum_level": self.maximum_level.value if self.maximum_level else None,
            "levels": sorted(level.value for level in self.levels) if self.levels else None,
            "components": sorted(self.components) if self.components else None,
            "modules": sorted(self.modules) if self.modules else None,
            "operations": sorted(self.operations) if self.operations else None,
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "run_id": str(self.run_id) if self.run_id else None,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "record_ids": sorted(str(record_id) for record_id in self.record_ids)
            if self.record_ids
            else None,
            "created_after": self.created_after.isoformat() if self.created_after else None,
            "created_before": self.created_before.isoformat() if self.created_before else None,
            "message_contains": self.message_contains,
            "attribute_equals": _thaw(self.attribute_equals),
            "limit": self.limit,
            "offset": self.offset,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        def text_set(name: str) -> frozenset[str] | None:
            raw = value.get(name)
            if raw is None:
                return None
            if not isinstance(raw, Sequence) or isinstance(raw, str):
                raise LogSerializationError("Invalid log filter")
            return frozenset(str(item) for item in raw)

        def parsed_time(name: str) -> datetime | None:
            raw = value.get(name)
            if raw is None:
                return None
            if not isinstance(raw, str):
                raise LogSerializationError("Invalid log filter")
            return datetime.fromisoformat(raw)

        raw_levels = value.get("levels")
        raw_record_ids = value.get("record_ids")
        attributes = value.get("attribute_equals", {})
        message_contains = value.get("message_contains")
        try:
            if raw_levels is not None and (
                not isinstance(raw_levels, Sequence) or isinstance(raw_levels, str)
            ):
                raise ValueError("Invalid log levels")
            if raw_record_ids is not None and (
                not isinstance(raw_record_ids, Sequence) or isinstance(raw_record_ids, str)
            ):
                raise ValueError("Invalid log record identifiers")
            if not isinstance(attributes, Mapping) or (
                message_contains is not None and not isinstance(message_contains, str)
            ):
                raise ValueError("Invalid log filter values")
            return cls(
                LogLevel.parse(str(value["minimum_level"]))
                if value.get("minimum_level") is not None
                else None,
                LogLevel.parse(str(value["maximum_level"]))
                if value.get("maximum_level") is not None
                else None,
                frozenset(LogLevel.parse(str(item)) for item in raw_levels)
                if isinstance(raw_levels, Sequence) and not isinstance(raw_levels, str)
                else None,
                text_set("components"),
                text_set("modules"),
                text_set("operations"),
                CorrelationId(str(value["correlation_id"]))
                if value.get("correlation_id")
                else None,
                RunId(str(value["run_id"])) if value.get("run_id") else None,
                TenantId(str(value["tenant_id"])) if value.get("tenant_id") else None,
                UserId(str(value["user_id"])) if value.get("user_id") else None,
                frozenset(LogRecordId(str(item)) for item in raw_record_ids)
                if isinstance(raw_record_ids, Sequence) and not isinstance(raw_record_ids, str)
                else None,
                parsed_time("created_after"),
                parsed_time("created_before"),
                message_contains,
                attributes,
                _integer(value["limit"]) if value.get("limit") is not None else None,
                _integer(value.get("offset", 0)),
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise LogSerializationError("Invalid log filter") from error


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

    def __post_init__(self) -> None:
        if (
            self.capacity < 1
            or self.stored_count < 0
            or self.stored_count > self.capacity
            or self.accepted_count < 0
            or self.dropped_count < 0
            or self.failed_count < 0
        ):
            raise ValidationError("Invalid log sink snapshot")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValidationError("Log sink snapshot time must be timezone-aware")

    def to_dict(self) -> dict[str, object]:
        return {
            "stored_count": self.stored_count,
            "capacity": self.capacity,
            "accepted_count": self.accepted_count,
            "dropped_count": self.dropped_count,
            "failed_count": self.failed_count,
            "closed": self.closed,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        created_at = value.get("created_at")
        closed = value.get("closed")
        try:
            if not isinstance(created_at, str) or not isinstance(closed, bool):
                raise ValueError("invalid snapshot")
            return cls(
                _integer(value["stored_count"]),
                _integer(value["capacity"]),
                _integer(value["accepted_count"]),
                _integer(value["dropped_count"]),
                _integer(value["failed_count"]),
                closed,
                datetime.fromisoformat(created_at),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise LogSerializationError("Invalid log sink snapshot") from error


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
            incoming = tuple(records)
            if not all(isinstance(record, LogRecord) for record in incoming):
                self._failed += 1
                raise LogSinkError("Invalid log record batch")
            identifiers = [record.record_id for record in incoming]
            if len(set(identifiers)) != len(identifiers) or any(
                record_id in {item.record_id for item in self._records} for record_id in identifiers
            ):
                self._failed += 1
                raise ResourceConflictError("Duplicate log record identity")
            if (
                self.drop_policy is LogDropPolicy.RAISE
                and len(self._records) + len(incoming) > self.capacity
            ):
                self._failed += 1
                raise LogSinkError("Log sink capacity exceeded")
            if self.drop_policy is LogDropPolicy.DROP_OLDEST:
                combined = [*self._records, *incoming]
                evicted = max(0, len(combined) - self.capacity)
                self._records = combined[-self.capacity :]
                self._accepted += len(incoming)
                self._dropped += evicted
            elif self.drop_policy is LogDropPolicy.DROP_NEWEST:
                room = max(0, self.capacity - len(self._records))
                accepted = incoming[:room]
                self._records.extend(accepted)
                self._accepted += len(accepted)
                self._dropped += len(incoming) - len(accepted)
            else:
                self._records.extend(incoming)
                self._accepted += len(incoming)

    async def get_optional(self, record_id: LogRecordId) -> LogRecord | None:
        async with self._lock:
            return next((item for item in self._records if item.record_id == record_id), None)

    async def get(self, record_id: LogRecordId) -> LogRecord:
        record = await self.get_optional(record_id)
        if record is None:
            raise ResourceNotFoundError("Log record was not found", {"record_id": str(record_id)})
        return record

    async def list(self, filter: LogFilter | None = None) -> tuple[LogRecord, ...]:
        async with self._lock:
            values = tuple(self._records)
        selected = tuple(item for item in values if filter is None or filter.matches(item))
        selected = tuple(
            sorted(
                selected,
                key=lambda item: (
                    item.timestamp,
                    item.sequence is None,
                    item.sequence if item.sequence is not None else 0,
                    str(item.record_id),
                ),
            )
        )
        if filter is None:
            return selected
        return selected[
            filter.offset : None if filter.limit is None else filter.offset + filter.limit
        ]

    async def count(self, filter: LogFilter | None = None) -> int:
        async with self._lock:
            return sum(1 for item in self._records if filter is None or filter.matches(item))

    async def clear(self) -> int:
        async with self._lock:
            removed = len(self._records)
            self._records.clear()
            return removed

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
        return None

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
