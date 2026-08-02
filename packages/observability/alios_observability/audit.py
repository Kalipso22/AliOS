"""Immutable, provider-neutral audit identity and record contracts.

Integrity digests are fingerprints, not digital signatures: they do not authenticate a
producer or create a tamper-proof ledger. Hash chaining and append-only storage belong to C2B.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Self, cast

from alios_core.errors import (
    AuditContextError,
    AuditIntegrityError,
    AuditSerializationError,
    AuditValidationError,
)
from alios_core.ids import (
    AuditRecordId,
    CorrelationId,
    Identifier,
    RunId,
    SpanId,
    TenantId,
    TraceId,
    UserId,
)
from alios_core.types import JsonValue, utc_now

from .logging import RedactionPolicy, default_redaction_policy
from .tracing import TraceContext, TraceSource

_CURRENT: ContextVar[AuditContext | None] = ContextVar("alios_audit_context", default=None)
_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:/-]*$")
_LOWER_HEX = re.compile(r"^[0-9a-f]{64}$")


def _validation(message: str, field_name: str | None = None) -> AuditValidationError:
    return AuditValidationError(
        message, details={"field_name": field_name} if field_name is not None else {}
    )


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _validation("Audit timestamp must be timezone-aware", field_name)
    return value


def _human(value: object, field_name: str, maximum: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _validation("Invalid audit text", field_name)
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or "\0" in result
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise _validation("Invalid audit text", field_name)
    return result


def _token(value: object, field_name: str, maximum: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or value.startswith("__")
        or _TOKEN.fullmatch(value) is None
    ):
        raise _validation("Invalid audit structural token", field_name)
    return value


def _normalize(
    value: object,
    *,
    depth: int = 0,
    count: list[int] | None = None,
    seen: set[int] | None = None,
) -> JsonValue:
    if depth > 16:
        raise AuditSerializationError(
            "Audit data nesting is too deep", details={"maximum_depth": 16}
        )
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Identifier):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise AuditSerializationError("Audit data datetime must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if len(value) > 16_384:
            raise AuditSerializationError(
                "Audit data string is too long", details={"maximum_string_length": 16_384}
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditSerializationError("Audit data must be finite")
        return value
    counter = count if count is not None else [0]
    visited = seen if seen is not None else set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in visited:
            raise AuditSerializationError("Audit data contains a cycle")
        visited.add(identity)
        try:
            result: dict[str, JsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise AuditSerializationError("Audit data mapping keys must be strings")
                if len(key) > 16_384:
                    raise AuditSerializationError("Audit data string is too long")
                counter[0] += 1
                if counter[0] > 10_000:
                    raise AuditSerializationError(
                        "Audit data is too large", details={"maximum_items": 10_000}
                    )
                result[key] = _normalize(item, depth=depth + 1, count=counter, seen=visited)
            return result
        finally:
            visited.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in visited:
            raise AuditSerializationError("Audit data contains a cycle")
        visited.add(identity)
        try:
            result_list: list[JsonValue] = []
            for item in value:
                counter[0] += 1
                if counter[0] > 10_000:
                    raise AuditSerializationError(
                        "Audit data is too large", details={"maximum_items": 10_000}
                    )
                result_list.append(_normalize(item, depth=depth + 1, count=counter, seen=visited))
            return result_list
        finally:
            visited.remove(identity)
    raise AuditSerializationError("Audit data type is unsupported")


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


def _data(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _validation("Audit attributes must be a mapping", field_name)
    normalized = _normalize(value)
    if not isinstance(normalized, dict):
        raise _validation("Audit attributes must be a mapping", field_name)
    return MappingProxyType({key: _freeze(item) for key, item in normalized.items()})


class _AuditEnum(StrEnum):
    @classmethod
    def parse(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise _validation("Invalid audit enumeration")
        try:
            return cls(value)
        except ValueError as error:
            raise _validation("Invalid audit enumeration") from error


class AuditCategory(_AuditEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    DATA_ACCESS = "data_access"
    CONFIGURATION = "configuration"
    EXECUTION = "execution"
    LIFECYCLE = "lifecycle"
    SECURITY = "security"
    SYSTEM = "system"


class AuditSeverity(_AuditEnum):
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditOutcome(_AuditEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    ERROR = "error"
    UNKNOWN = "unknown"


class AuditActorKind(_AuditEnum):
    USER = "user"
    SERVICE = "service"
    AGENT = "agent"
    SYSTEM = "system"
    ANONYMOUS = "anonymous"


def _render(policy: RedactionPolicy, value: object) -> JsonValue:
    return policy.redact(_thaw(value))


@dataclass(frozen=True, slots=True)
class AuditActor:
    kind: AuditActorKind
    identifier: str | None = None
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AuditActorKind):
            raise _validation("Invalid audit actor kind", "kind")
        if self.kind is AuditActorKind.ANONYMOUS:
            if self.identifier is not None:
                raise _validation("Anonymous audit actor cannot have an identifier", "identifier")
        else:
            object.__setattr__(self, "identifier", _human(self.identifier, "identifier", 512))
        if self.tenant_id is not None and type(self.tenant_id) is not TenantId:
            raise _validation("Invalid audit actor identifier", "tenant_id")
        if self.user_id is not None and type(self.user_id) is not UserId:
            raise _validation("Invalid audit actor identifier", "user_id")
        object.__setattr__(self, "attributes", _data(self.attributes, "attributes"))

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "kind": self.kind.value,
            "identifier": policy.redact(self.identifier),
            "tenant_id": str(self.tenant_id) if self.tenant_id is not None else None,
            "user_id": str(self.user_id) if self.user_id is not None else None,
            "attributes": _render(policy, self.attributes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("kind"), str)
                or value.get("identifier") is not None
                and not isinstance(value.get("identifier"), str)
                or not isinstance(value.get("attributes", {}), Mapping)
            ):
                raise ValueError

            def optional(name: str, kind: type[Identifier]) -> Identifier | None:
                item = value.get(name)
                if item is None:
                    return None
                if not isinstance(item, str) or not item.strip():
                    raise ValueError
                return kind(item)

            return cls(
                AuditActorKind.parse(cast(str, value["kind"])),
                cast(str | None, value.get("identifier")),
                cast(TenantId | None, optional("tenant_id", TenantId)),
                cast(UserId | None, optional("user_id", UserId)),
                cast(Mapping[str, object], value.get("attributes", {})),
            )
        except (TypeError, ValueError, AuditValidationError, AuditSerializationError) as error:
            raise AuditSerializationError("Invalid serialized audit actor", cause=error) from error


@dataclass(frozen=True, slots=True)
class AuditTarget:
    kind: str
    identifier: str
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _token(self.kind, "kind", 256))
        object.__setattr__(self, "identifier", _human(self.identifier, "identifier", 512))
        object.__setattr__(self, "attributes", _data(self.attributes, "attributes"))

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "kind": self.kind,
            "identifier": policy.redact(self.identifier),
            "attributes": _render(policy, self.attributes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("kind"), str)
                or not isinstance(value.get("identifier"), str)
                or not isinstance(value.get("attributes", {}), Mapping)
            ):
                raise ValueError
            return cls(
                cast(str, value["kind"]),
                cast(str, value["identifier"]),
                cast(Mapping[str, object], value.get("attributes", {})),
            )
        except (TypeError, ValueError, AuditValidationError, AuditSerializationError) as error:
            raise AuditSerializationError("Invalid serialized audit target", cause=error) from error


@dataclass(frozen=True, slots=True)
class AuditAction:
    name: str
    attributes: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _token(self.name, "name", 256))
        object.__setattr__(self, "attributes", _data(self.attributes, "attributes"))

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {"name": self.name, "attributes": _render(policy, self.attributes)}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("name"), str)
                or not isinstance(value.get("attributes", {}), Mapping)
            ):
                raise ValueError
            return cls(
                cast(str, value["name"]), cast(Mapping[str, object], value.get("attributes", {}))
            )
        except (TypeError, ValueError, AuditValidationError, AuditSerializationError) as error:
            raise AuditSerializationError("Invalid serialized audit action", cause=error) from error


@dataclass(frozen=True, slots=True)
class AuditContext:
    correlation_id: CorrelationId | None = None
    run_id: RunId | None = None
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    trace_id: TraceId | None = None
    span_id: SpanId | None = None
    parent_span_id: SpanId | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected = (
            (self.correlation_id, CorrelationId, "correlation_id"),
            (self.run_id, RunId, "run_id"),
            (self.tenant_id, TenantId, "tenant_id"),
            (self.user_id, UserId, "user_id"),
            (self.trace_id, TraceId, "trace_id"),
            (self.span_id, SpanId, "span_id"),
            (self.parent_span_id, SpanId, "parent_span_id"),
        )
        for value, kind, name in expected:
            if value is not None and type(value) is not kind:
                raise _validation("Invalid audit context identifier", name)
        if (self.trace_id is None) != (self.span_id is None):
            raise AuditContextError("Audit trace and span identifiers must appear together")
        if self.parent_span_id is not None and self.trace_id is None:
            raise AuditContextError("Audit parent span requires trace context")
        if self.parent_span_id is not None and self.parent_span_id == self.span_id:
            raise AuditContextError("Audit parent span cannot equal span")
        object.__setattr__(self, "metadata", _data(self.metadata, "metadata"))

    @classmethod
    def from_trace_context(
        cls, trace_context: TraceContext, *, metadata: Mapping[str, object] | None = None
    ) -> Self:
        if not isinstance(trace_context, TraceContext):
            raise AuditContextError("Invalid trace context")
        return cls(
            trace_context.correlation_id,
            trace_context.run_id,
            trace_context.tenant_id,
            trace_context.user_id,
            trace_context.trace_id,
            trace_context.span_id,
            trace_context.parent_span_id,
            metadata or {},
        )

    def with_metadata(self, values: Mapping[str, object] | None = None, **kwargs: object) -> Self:
        if values is not None and not isinstance(values, Mapping):
            raise AuditContextError("Invalid audit context metadata")
        if values is not None and set(values).intersection(kwargs):
            raise AuditContextError("Ambiguous audit context metadata")
        return type(self)(
            self.correlation_id,
            self.run_id,
            self.tenant_id,
            self.user_id,
            self.trace_id,
            self.span_id,
            self.parent_span_id,
            {**self.metadata, **(values or {}), **kwargs},
        )

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        result: dict[str, JsonValue] = {"metadata": _render(policy, self.metadata)}
        for name in (
            "correlation_id",
            "run_id",
            "tenant_id",
            "user_id",
            "trace_id",
            "span_id",
            "parent_span_id",
        ):
            item = getattr(self, name)
            result[name] = str(item) if item is not None else None
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if not isinstance(value, Mapping) or not isinstance(value.get("metadata", {}), Mapping):
                raise ValueError

            def optional(name: str, kind: type[Identifier]) -> Identifier | None:
                item = value.get(name)
                if item is None:
                    return None
                if not isinstance(item, str) or not item.strip():
                    raise ValueError
                return kind(item)

            return cls(
                cast(CorrelationId | None, optional("correlation_id", CorrelationId)),
                cast(RunId | None, optional("run_id", RunId)),
                cast(TenantId | None, optional("tenant_id", TenantId)),
                cast(UserId | None, optional("user_id", UserId)),
                cast(TraceId | None, optional("trace_id", TraceId)),
                cast(SpanId | None, optional("span_id", SpanId)),
                cast(SpanId | None, optional("parent_span_id", SpanId)),
                cast(Mapping[str, object], value.get("metadata", {})),
            )
        except (
            TypeError,
            ValueError,
            AuditContextError,
            AuditValidationError,
            AuditSerializationError,
        ) as error:
            raise AuditSerializationError(
                "Invalid serialized audit context", cause=error
            ) from error


class _Binding(AbstractContextManager[None], AbstractAsyncContextManager[None]):
    def __init__(self, context: AuditContext) -> None:
        self._context = context
        self._token: Token[AuditContext | None] | None = None

    def __enter__(self) -> None:
        if self._token is not None:
            raise AuditContextError("Audit context binding is already active")
        self._token = _CURRENT.set(self._context)

    def __exit__(self, *_: object) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(self, *_: object) -> None:
        self.__exit__()


def current_audit_context() -> AuditContext | None:
    return _CURRENT.get()


def require_audit_context() -> AuditContext:
    context = current_audit_context()
    if context is None:
        raise AuditContextError("Audit context is not bound")
    return context


def bind_audit_context(context: AuditContext) -> _Binding:
    if not isinstance(context, AuditContext):
        raise AuditContextError("Invalid audit context")
    return _Binding(context)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    record_id: AuditRecordId
    occurred_at: datetime
    category: AuditCategory
    severity: AuditSeverity
    outcome: AuditOutcome
    actor: AuditActor
    action: AuditAction
    source: TraceSource
    summary: str
    context: AuditContext = field(default_factory=AuditContext)
    target: AuditTarget | None = None
    reason_code: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)
    tags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        expected = (
            (self.record_id, AuditRecordId),
            (self.category, AuditCategory),
            (self.severity, AuditSeverity),
            (self.outcome, AuditOutcome),
            (self.actor, AuditActor),
            (self.action, AuditAction),
            (self.source, TraceSource),
            (self.context, AuditContext),
        )
        if any(type(value) is not kind for value, kind in expected):
            raise _validation("Invalid audit record contract")
        _aware(self.occurred_at, "occurred_at")
        object.__setattr__(self, "summary", _human(self.summary, "summary", 1_024))
        if self.target is not None and type(self.target) is not AuditTarget:
            raise _validation("Invalid audit target", "target")
        object.__setattr__(
            self, "reason_code", _token(self.reason_code, "reason_code", 128, optional=True)
        )
        object.__setattr__(self, "attributes", _data(self.attributes, "attributes"))
        raw_tags = self.tags
        if not isinstance(raw_tags, (set, frozenset, list, tuple)) or not all(
            isinstance(item, str) for item in raw_tags
        ):
            raise _validation("Invalid audit tags", "tags")
        if isinstance(raw_tags, (list, tuple)) and len(set(raw_tags)) != len(raw_tags):
            raise _validation("Duplicate audit tags", "tags")
        tags = frozenset(cast(str, _token(item, "tag", 128)) for item in raw_tags)
        if len(tags) > 32:
            raise _validation("Too many audit tags", "tags")
        object.__setattr__(self, "tags", tags)

    @classmethod
    def create(
        cls,
        *,
        category: AuditCategory,
        severity: AuditSeverity,
        outcome: AuditOutcome,
        actor: AuditActor,
        action: AuditAction,
        source: TraceSource,
        summary: str,
        context: AuditContext | None = None,
        target: AuditTarget | None = None,
        reason_code: str | None = None,
        attributes: Mapping[str, object] | None = None,
        tags: frozenset[str] | tuple[str, ...] | list[str] = (),
        record_id: AuditRecordId | None = None,
        occurred_at: datetime | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> Self:
        if not callable(clock):
            raise _validation("Invalid audit clock", "clock")
        timestamp = occurred_at if occurred_at is not None else clock()
        resolved_context = (
            context if context is not None else (current_audit_context() or AuditContext())
        )
        return cls(
            record_id or AuditRecordId(),
            timestamp,
            category,
            severity,
            outcome,
            actor,
            action,
            source,
            summary,
            resolved_context,
            target,
            reason_code,
            attributes or {},
            cast(frozenset[str], tags),
        )

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        policy = redaction_policy or default_redaction_policy()
        return {
            "record_id": str(self.record_id),
            "occurred_at": self.occurred_at.isoformat(),
            "category": self.category.value,
            "severity": self.severity.value,
            "outcome": self.outcome.value,
            "actor": self.actor.to_dict(policy),
            "action": self.action.to_dict(policy),
            "source": self.source.to_dict(),
            "summary": policy.redact(self.summary),
            "context": self.context.to_dict(policy),
            "target": self.target.to_dict(policy) if self.target is not None else None,
            "reason_code": self.reason_code,
            "attributes": _render(policy, self.attributes),
            "tags": cast(JsonValue, sorted(self.tags)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            required_mappings = ("actor", "action", "source", "context")
            if (
                not isinstance(value, Mapping)
                or any(not isinstance(value.get(name), Mapping) for name in required_mappings)
                or not isinstance(value.get("record_id"), str)
                or not isinstance(value.get("occurred_at"), str)
                or any(
                    not isinstance(value.get(name), str)
                    for name in ("category", "severity", "outcome", "summary")
                )
                or not isinstance(value.get("attributes", {}), Mapping)
                or not isinstance(value.get("tags", []), (list, tuple))
            ):
                raise ValueError
            target = value.get("target")
            tags = cast(list[object] | tuple[object, ...], value.get("tags", []))
            if (
                target is not None
                and not isinstance(target, Mapping)
                or not all(isinstance(item, str) for item in tags)
            ):
                raise ValueError
            return cls(
                AuditRecordId(cast(str, value["record_id"])),
                datetime.fromisoformat(cast(str, value["occurred_at"])),
                AuditCategory.parse(cast(str, value["category"])),
                AuditSeverity.parse(cast(str, value["severity"])),
                AuditOutcome.parse(cast(str, value["outcome"])),
                AuditActor.from_dict(cast(Mapping[str, object], value["actor"])),
                AuditAction.from_dict(cast(Mapping[str, object], value["action"])),
                TraceSource.from_dict(cast(Mapping[str, object], value["source"])),
                cast(str, value["summary"]),
                AuditContext.from_dict(cast(Mapping[str, object], value["context"])),
                AuditTarget.from_dict(cast(Mapping[str, object], target))
                if target is not None
                else None,
                cast(str | None, value.get("reason_code")),
                cast(Mapping[str, object], value.get("attributes", {})),
                frozenset(cast(list[str] | tuple[str, ...], tags)),
            )
        except (
            TypeError,
            ValueError,
            AuditValidationError,
            AuditContextError,
            AuditSerializationError,
        ) as error:
            raise AuditSerializationError("Invalid serialized audit record", cause=error) from error

    def _integrity_payload(self) -> dict[str, JsonValue]:
        def instant(value: datetime) -> str:
            return value.astimezone(UTC).isoformat()

        return {
            "record_id": str(self.record_id),
            "occurred_at": instant(self.occurred_at),
            "category": self.category.value,
            "severity": self.severity.value,
            "outcome": self.outcome.value,
            "actor": {
                "kind": self.actor.kind.value,
                "identifier": self.actor.identifier,
                "tenant_id": str(self.actor.tenant_id) if self.actor.tenant_id else None,
                "user_id": str(self.actor.user_id) if self.actor.user_id else None,
                "attributes": _thaw(self.actor.attributes),
            },
            "action": {"name": self.action.name, "attributes": _thaw(self.action.attributes)},
            "source": self.source.to_dict(),
            "summary": self.summary,
            "context": {
                **{
                    name: str(getattr(self.context, name))
                    if getattr(self.context, name) is not None
                    else None
                    for name in (
                        "correlation_id",
                        "run_id",
                        "tenant_id",
                        "user_id",
                        "trace_id",
                        "span_id",
                        "parent_span_id",
                    )
                },
                "metadata": _thaw(self.context.metadata),
            },
            "target": {
                "kind": self.target.kind,
                "identifier": self.target.identifier,
                "attributes": _thaw(self.target.attributes),
            }
            if self.target
            else None,
            "reason_code": self.reason_code,
            "attributes": _thaw(self.attributes),
            "tags": cast(JsonValue, sorted(self.tags)),
        }

    def integrity_digest(self) -> str:
        payload = json.dumps(
            self._integrity_payload(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verify_integrity_digest(self, expected: str) -> bool:
        if not isinstance(expected, str) or _LOWER_HEX.fullmatch(expected) is None:
            raise AuditIntegrityError("Invalid audit integrity digest")
        return hmac.compare_digest(self.integrity_digest(), expected)
