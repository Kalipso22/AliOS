"""Provider-neutral structured logging primitives; global logging configuration is out of scope."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
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
    LoggingError,
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


def _normal(
    value: object,
    *,
    maximum_depth: int = 16,
    maximum_items: int = 10_000,
    depth: int = 0,
    seen: set[int] | None = None,
    consumed: list[int] | None = None,
) -> JsonValue:
    if depth > maximum_depth:
        raise LogSerializationError("Logging value nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LogSerializationError("Logging value is not JSON-compatible")
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
    counter = consumed if consumed is not None else [0]
    if isinstance(value, Mapping):
        if id(value) in visited:
            raise LogSerializationError("Logging value contains a cycle")
        visited.add(id(value))
        try:
            output: dict[str, JsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise LogSerializationError("Logging value is not JSON-compatible")
                counter[0] += 1
                if counter[0] > maximum_items:
                    raise LogSerializationError("Logging value is too large")
                output[key] = _normal(
                    item,
                    maximum_depth=maximum_depth,
                    maximum_items=maximum_items,
                    depth=depth + 1,
                    seen=visited,
                    consumed=counter,
                )
            return output
        finally:
            visited.remove(id(value))
    if isinstance(value, (list, tuple)):
        if id(value) in visited:
            raise LogSerializationError("Logging value contains a cycle")
        visited.add(id(value))
        try:
            sequence_output: list[JsonValue] = []
            for item in value:
                counter[0] += 1
                if counter[0] > maximum_items:
                    raise LogSerializationError("Logging value is too large")
                sequence_output.append(
                    _normal(
                        item,
                        maximum_depth=maximum_depth,
                        maximum_items=maximum_items,
                        depth=depth + 1,
                        seen=visited,
                        consumed=counter,
                    )
                )
            return sequence_output
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
    if value is not None and not isinstance(value, Mapping):
        raise LogSerializationError("Logging attributes must be a mapping")
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
        expected: tuple[tuple[object | None, type[object]], ...] = (
            (self.correlation_id, CorrelationId),
            (self.run_id, RunId),
            (self.tenant_id, TenantId),
            (self.user_id, UserId),
            (self.task_id, TaskId),
            (self.agent_id, AgentId),
            (self.workflow_id, WorkflowId),
            (self.parent_log_record_id, LogRecordId),
        )
        if any(value is not None and not isinstance(value, kind) for value, kind in expected):
            raise ValidationError("Invalid log context identifier")
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
        output["attributes"] = _thaw(self.attributes)
        return output

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        if not isinstance(value, Mapping):
            raise LogSerializationError("Invalid log context")
        attributes = value.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise LogSerializationError("Invalid log context")

        def raw_identifier(name: str) -> str | None:
            raw = value.get(name)
            if raw is not None and not isinstance(raw, str):
                raise ValueError("invalid identifier")
            return raw

        try:
            correlation_id = raw_identifier("correlation_id")
            run_id = raw_identifier("run_id")
            tenant_id = raw_identifier("tenant_id")
            user_id = raw_identifier("user_id")
            task_id = raw_identifier("task_id")
            agent_id = raw_identifier("agent_id")
            workflow_id = raw_identifier("workflow_id")
            parent_record_id = raw_identifier("parent_log_record_id")
            return cls(
                CorrelationId(correlation_id) if correlation_id else None,
                RunId(run_id) if run_id else None,
                TenantId(tenant_id) if tenant_id else None,
                UserId(user_id) if user_id else None,
                TaskId(task_id) if task_id else None,
                AgentId(agent_id) if agent_id else None,
                WorkflowId(workflow_id) if workflow_id else None,
                LogRecordId(parent_record_id) if parent_record_id else None,
                attributes,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise LogSerializationError("Invalid log context") from error


class _Binding(AbstractContextManager[None], AbstractAsyncContextManager[None]):
    def __init__(self, context: LogContext) -> None:
        self._context = context
        self._token: Token[LogContext | None] | None = None

    def __enter__(self) -> None:
        if self._token is not None:
            raise LoggingError("Log context binding is already active")
        self._token = _CURRENT.set(self._context)

    def __exit__(self, *_: object) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(self, *_: object) -> None:
        self.__exit__()


def current_log_context() -> LogContext | None:
    return _CURRENT.get()


def bind_log_context(context: LogContext) -> _Binding:
    if not isinstance(context, LogContext):
        raise ValidationError("Invalid log context")
    return _Binding(context)


def _merge_log_contexts(*contexts: LogContext | None) -> LogContext | None:
    selected = tuple(context for context in contexts if context is not None)
    if not selected:
        return None
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
    identifiers = {
        name: next(
            (
                value
                for context in reversed(selected)
                if (value := getattr(context, name)) is not None
            ),
            None,
        )
        for name in names
    }
    return LogContext(
        **identifiers,
        attributes={
            key: value for context in selected for key, value in context.attributes.items()
        },
    )


def merge_current_log_context(context: LogContext | None) -> LogContext | None:
    return _merge_log_contexts(current_log_context(), context)


@dataclass(frozen=True, slots=True)
class RedactionRule:
    name: str
    action: RedactionAction = RedactionAction.MASK
    keys: frozenset[str] = field(default_factory=frozenset)
    path_patterns: tuple[str, ...] = ()
    value_patterns: tuple[str, ...] = ()
    replacement: str = "[REDACTED]"
    case_sensitive: bool = False
    _compiled_value_patterns: tuple[re.Pattern[str], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_text(self.name, "rule name", required=True))
        keys = frozenset(_validate_text(item, "redaction key", required=True) for item in self.keys)
        paths = tuple(self._validate_path(pattern) for pattern in self.path_patterns)
        patterns = tuple(
            _validate_text(pattern, "redaction pattern", required=True)
            for pattern in self.value_patterns
        )
        object.__setattr__(self, "keys", keys)
        object.__setattr__(self, "path_patterns", paths)
        object.__setattr__(self, "value_patterns", patterns)
        if not (self.keys or self.path_patterns or self.value_patterns):
            raise ValidationError("Redaction rule requires a matcher")
        if not isinstance(self.action, RedactionAction):
            raise ValidationError("Invalid redaction action")
        if not isinstance(self.case_sensitive, bool):
            raise ValidationError("Invalid redaction case sensitivity")
        replacement = _validate_text(self.replacement, "redaction replacement", required=True)
        if replacement is None:
            raise ValidationError("Invalid redaction replacement")
        object.__setattr__(self, "replacement", replacement)
        try:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            object.__setattr__(
                self,
                "_compiled_value_patterns",
                tuple(re.compile(pattern, flags) for pattern in patterns),
            )
        except re.error as error:
            raise ValidationError("Invalid redaction pattern") from error

    @staticmethod
    def _validate_path(value: str) -> str:
        text = _validate_text(value, "redaction path", required=True)
        if text is None:
            raise ValidationError("Invalid redaction path")
        parts = tuple(part.strip() for part in text.split("."))
        if any(not part or ("*" in part and part not in {"*", "**"}) for part in parts):
            raise ValidationError("Invalid redaction path")
        if "**" in parts[:-1]:
            raise ValidationError("Invalid redaction path")
        return ".".join(parts)

    def matches(self, key: str | None, path: tuple[str, ...], value: JsonValue) -> bool:
        normalize = (lambda item: item) if self.case_sensitive else str.lower
        if key is not None and any(normalize(key) == normalize(item) for item in self.keys):
            return True
        for pattern in self.path_patterns:
            parts = pattern.split(".")
            if parts[-1:] == ["**"]:
                if len(path) >= len(parts) - 1 and all(
                    item == "*" or normalize(item) == normalize(part)
                    for item, part in zip(parts[:-1], path, strict=False)
                ):
                    return True
            elif len(parts) == len(path) and all(
                item == "*" or normalize(item) == normalize(part)
                for item, part in zip(parts, path, strict=True)
            ):
                return True
        return isinstance(value, str) and any(
            pattern.search(value) for pattern in self._compiled_value_patterns
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

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            keys = value.get("keys", ())
            paths = value.get("path_patterns", ())
            patterns = value.get("value_patterns", ())
            if any(
                not isinstance(item, Sequence) or isinstance(item, str)
                for item in (keys, paths, patterns)
            ):
                raise ValueError("invalid matchers")
            if not isinstance(keys, Sequence) or isinstance(keys, str):
                raise ValueError("invalid keys")
            if not isinstance(paths, Sequence) or isinstance(paths, str):
                raise ValueError("invalid paths")
            if not isinstance(patterns, Sequence) or isinstance(patterns, str):
                raise ValueError("invalid patterns")
            if (
                not isinstance(value.get("name"), str)
                or not isinstance(value.get("replacement", "[REDACTED]"), str)
                or not isinstance(value.get("case_sensitive", False), bool)
            ):
                raise ValueError("invalid rule")
            name = value.get("name")
            replacement = value.get("replacement", "[REDACTED]")
            case_sensitive = value.get("case_sensitive", False)
            if (
                not isinstance(name, str)
                or not isinstance(replacement, str)
                or not isinstance(case_sensitive, bool)
            ):
                raise ValueError("invalid rule")
            return cls(
                name,
                RedactionAction(str(value.get("action", RedactionAction.MASK))),
                frozenset(str(item) for item in keys),
                tuple(str(item) for item in paths),
                tuple(str(item) for item in patterns),
                replacement,
                case_sensitive,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise LogSerializationError("Invalid redaction rule") from error


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

_SENSITIVE_VALUE_PATTERNS = (
    r"\b(?:password|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|session[_-]?id|private[_-]?key|credential|cookie|set-cookie|proxy[_-]?authorization)\s*[:=]\s*(?:Bearer\s+)?\S+",
    r"\bauthorization\s*[:=]\s*Bearer\s+\S+",
    r"\bBearer\s+[-A-Za-z0-9._~+/=]+",
    r"-----BEGIN (?:RSA )?PRIVATE KEY-----",
)

_DEFAULT_REDACTION_RULE = RedactionRule(
    "default-sensitive", keys=_SENSITIVE_KEYS, value_patterns=_SENSITIVE_VALUE_PATTERNS
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
        rules = tuple(self.rules)
        if not all(isinstance(rule, RedactionRule) for rule in rules) or not isinstance(
            self.include_default_rules, bool
        ):
            raise ValidationError("Invalid redaction policy")
        object.__setattr__(self, "rules", rules)

    def redact(self, value: object) -> JsonValue:
        rules = self.rules + ((_DEFAULT_REDACTION_RULE,) if self.include_default_rules else ())
        normalized = _normal(
            value, maximum_depth=self.maximum_depth, maximum_items=self.maximum_items
        )

        def render(
            item: JsonValue, path: tuple[str, ...], key: str | None, depth: int
        ) -> JsonValue:
            if depth > self.maximum_depth:
                raise LogSerializationError("Logging value nesting is too deep")
            normal = item
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

        return render(normalized, (), None, 0)

    def to_dict(self) -> dict[str, object]:
        return {
            "rules": [rule.to_dict() for rule in self.rules],
            "include_default_rules": self.include_default_rules,
            "maximum_depth": self.maximum_depth,
            "maximum_items": self.maximum_items,
            "maximum_string_length": self.maximum_string_length,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        rules = value.get("rules")
        try:
            if (
                not isinstance(rules, Sequence)
                or isinstance(rules, str)
                or not isinstance(value.get("include_default_rules"), bool)
            ):
                raise ValueError("invalid policy")
            if not all(isinstance(rule, Mapping) for rule in rules):
                raise ValueError("invalid rule")
            include_default_rules = value["include_default_rules"]
            if not isinstance(include_default_rules, bool):
                raise ValueError("invalid policy")
            return cls(
                tuple(RedactionRule.from_dict(rule) for rule in rules),
                include_default_rules,
                _integer(value["maximum_depth"]),
                _integer(value["maximum_items"]),
                _integer(value["maximum_string_length"]),
            )
        except (KeyError, TypeError, ValueError, ValidationError, LogSerializationError) as error:
            raise LogSerializationError("Invalid redaction policy") from error


_DEFAULT_REDACTION_POLICY = RedactionPolicy()


def default_redaction_policy() -> RedactionPolicy:
    return _DEFAULT_REDACTION_POLICY


@dataclass(frozen=True, slots=True)
class LogException:
    error_type: str
    error_code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        for name, value, limit, newlines in (
            ("error type", self.error_type, 256, False),
            ("error code", self.error_code, 256, False),
            ("error message", self.message, 4096, True),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > limit
                or "\0" in value
                or (not newlines and any(ord(char) < 32 for char in value))
            ):
                raise ValidationError(f"Invalid {name}")
        if not isinstance(self.retryable, bool):
            raise ValidationError("Invalid retryable value")
        object.__setattr__(self, "error_type", self.error_type.strip())
        object.__setattr__(self, "error_code", self.error_code.strip())
        object.__setattr__(self, "message", self.message.strip())
        object.__setattr__(self, "details", _attributes(self.details))

    @classmethod
    def from_exception(cls, error: BaseException) -> Self:
        if isinstance(
            error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)
        ):
            raise error
        if isinstance(error, AliOSError):
            return cls(type(error).__name__, error.code, error.message, error.details)
        return cls(type(error).__name__, "unhandled_exception", "An unexpected error occurred")

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "error_type": self.error_type,
            "error_code": self.error_code,
            "message": policy.redact(self.message),
            "details": policy.redact(self.details),
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        if not isinstance(value, Mapping):
            raise LogSerializationError("Invalid log exception")
        details = value.get("details", {})
        error_type = value.get("error_type")
        error_code = value.get("error_code")
        message = value.get("message")
        retryable = value.get("retryable")
        try:
            if (
                not all(isinstance(item, str) for item in (error_type, error_code, message))
                or not isinstance(details, Mapping)
                or not isinstance(retryable, bool)
            ):
                raise ValueError("invalid exception")
            if (
                not isinstance(error_type, str)
                or not isinstance(error_code, str)
                or not isinstance(message, str)
                or not isinstance(retryable, bool)
            ):
                raise ValueError("invalid exception")
            return cls(
                error_type,
                error_code,
                message,
                details,
                retryable,
            )
        except (TypeError, ValueError, ValidationError, LogSerializationError) as error:
            raise LogSerializationError("Invalid log exception") from error


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
        if (
            not isinstance(self.record_id, LogRecordId)
            or not isinstance(self.level, LogLevel)
            or not isinstance(self.source, LogSource)
            or not isinstance(self.context, LogContext)
        ):
            raise ValidationError("Invalid log record")
        if not isinstance(self.message, str) or (
            self.exception is not None and not isinstance(self.exception, LogException)
        ):
            raise ValidationError("Invalid log record")
        message = self.message.strip()
        if not message or len(message) > 16_384 or "\0" in message:
            raise ValidationError("Invalid log message")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValidationError("Log timestamp must be timezone-aware")
        if self.sequence is not None and (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
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
        exception = value.get("exception")
        message = value.get("message")
        if (
            not isinstance(timestamp, str)
            or not isinstance(source, Mapping)
            or not isinstance(context, Mapping)
            or not isinstance(attributes, Mapping)
            or not isinstance(message, str)
        ):
            raise LogSerializationError("Invalid log record")
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("invalid timestamp")
            if isinstance(sequence, bool) or (
                sequence is not None and not isinstance(sequence, (int, str))
            ):
                raise ValueError("invalid sequence")
            if exception is not None and not isinstance(exception, Mapping):
                raise ValueError("invalid exception")
            if not isinstance(message, str):
                raise ValueError("invalid message")
            return cls(
                LogLevel.parse(str(value["level"])),
                message,
                LogSource.from_dict(source),
                LogContext.from_dict(context),
                attributes,
                LogException.from_dict(exception) if isinstance(exception, Mapping) else None,
                _integer(sequence) if sequence is not None else None,
                LogRecordId(str(value["record_id"])),
                parsed,
            )
        except (KeyError, TypeError, ValueError, ValidationError, LogSerializationError) as error:
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
        if not isinstance(level, LogLevel):
            raise ValidationError("Invalid log level")
        if level.severity < self.minimum_level.severity:
            return None
        merged = _merge_log_contexts(current_log_context(), self.base_context, context)
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
        error: BaseException,
        *,
        operation: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> LogRecord | None:
        if LogLevel.ERROR.severity < self.minimum_level.severity:
            return None
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
            source_module=self.source_module
            if module is None
            else _validate_text(module, "module", required=True),
            base_context=_merge_log_contexts(self.base_context, context) or LogContext(),
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
        self._close_lock = asyncio.Lock()

    def get_logger(
        self,
        component: str,
        *,
        module: str | None = None,
        context: LogContext | None = None,
        attributes: Mapping[str, object] | None = None,
    ) -> StructuredLogger:
        if self._closed:
            raise LoggingError("Logger factory is closed")
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
        async with self._close_lock:
            if self._closed:
                return
            if self.owns_sink:
                await self.sink.close()
            self._closed = True
