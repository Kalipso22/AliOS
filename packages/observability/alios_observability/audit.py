"""Immutable, provider-neutral audit identity and record contracts.

Integrity digests are fingerprints, not digital signatures: they do not authenticate a
producer or create a tamper-proof ledger. Hash chaining and append-only storage belong to C2B.
"""

from __future__ import annotations

import asyncio
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
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Protocol, Self, cast

from alios_core.errors import (
    AuditContextError,
    AuditIntegrityError,
    AuditLedgerCapacityError,
    AuditLedgerClosedError,
    AuditLedgerError,
    AuditLedgerVerificationError,
    AuditSerializationError,
    AuditValidationError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from alios_core.ids import (
    AuditLedgerId,
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
_SHA256_HEX_LENGTH = 64
_GENESIS_DIGEST = "0" * _SHA256_HEX_LENGTH
_MAX_LEDGER_ENTRIES = 1_000_000


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
    if isinstance(value, Enum):
        raise AuditSerializationError("Audit data type is unsupported")
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
        if metadata is not None and not isinstance(metadata, Mapping):
            raise AuditContextError("Invalid audit context metadata")
        try:
            return cls(
                trace_context.correlation_id,
                trace_context.run_id,
                trace_context.tenant_id,
                trace_context.user_id,
                trace_context.trace_id,
                trace_context.span_id,
                trace_context.parent_span_id,
                {} if metadata is None else metadata,
            )
        except (AuditValidationError, AuditSerializationError) as error:
            raise AuditContextError("Invalid audit context metadata", cause=error) from error

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
            record_id if record_id is not None else AuditRecordId(),
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
            {} if attributes is None else attributes,
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
                cast(frozenset[str], tags),
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
                "tenant_id": str(self.actor.tenant_id)
                if self.actor.tenant_id is not None
                else None,
                "user_id": str(self.actor.user_id) if self.actor.user_id is not None else None,
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
            if self.target is not None
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


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _LOWER_HEX.fullmatch(value) is None:
        raise AuditLedgerVerificationError(
            "Invalid audit ledger digest", details={"field_name": field_name}
        )
    return value


def _entry_digest(
    ledger_id: AuditLedgerId,
    sequence: int,
    record_digest: str,
    previous_digest: str,
    appended_at: datetime,
) -> str:
    payload = json.dumps(
        {
            "ledger_id": str(ledger_id),
            "sequence": sequence,
            "record_digest": record_digest,
            "previous_digest": previous_digest,
            "appended_at": appended_at.astimezone(UTC).isoformat(),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _positive_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise AuditLedgerError("Invalid audit ledger integer", details={"field_name": field_name})
    return value


def _nonnegative_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise AuditLedgerError("Invalid audit ledger integer", details={"field_name": field_name})
    return value


def _ledger_aware(value: object, field_name: str) -> datetime:
    try:
        return _aware(value, field_name)
    except AuditValidationError as error:
        raise AuditLedgerError(
            "Invalid audit ledger timestamp", details={"field_name": field_name}, cause=error
        ) from error


@dataclass(frozen=True, slots=True)
class AuditLedgerEntry:
    ledger_id: AuditLedgerId
    sequence: int
    record: AuditRecord
    appended_at: datetime
    previous_digest: str
    record_digest: str
    entry_digest: str

    def __post_init__(self) -> None:
        if type(self.ledger_id) is not AuditLedgerId:
            raise AuditLedgerError("Invalid audit ledger identifier")
        _positive_integer(self.sequence, "sequence")
        if type(self.record) is not AuditRecord:
            raise AuditLedgerError("Invalid audit ledger record")
        _ledger_aware(self.appended_at, "appended_at")
        previous = _digest(self.previous_digest, "previous_digest")
        record_digest = _digest(self.record_digest, "record_digest")
        entry_digest = _digest(self.entry_digest, "entry_digest")
        if self.sequence == 1 and previous != _GENESIS_DIGEST:
            raise AuditLedgerVerificationError("Invalid audit ledger genesis digest")
        if not hmac.compare_digest(record_digest, self.record.integrity_digest()):
            raise AuditLedgerVerificationError("Invalid audit ledger record digest")
        expected = _entry_digest(
            self.ledger_id, self.sequence, record_digest, previous, self.appended_at
        )
        if not hmac.compare_digest(entry_digest, expected):
            raise AuditLedgerVerificationError("Invalid audit ledger entry digest")

    @classmethod
    def create(
        cls,
        *,
        ledger_id: AuditLedgerId,
        sequence: int,
        record: AuditRecord,
        appended_at: datetime,
        previous_digest: str,
    ) -> Self:
        if type(ledger_id) is not AuditLedgerId:
            raise AuditLedgerError("Invalid audit ledger identifier")
        _positive_integer(sequence, "sequence")
        if type(record) is not AuditRecord:
            raise AuditLedgerError("Invalid audit ledger record")
        _ledger_aware(appended_at, "appended_at")
        previous = _digest(previous_digest, "previous_digest")
        record_digest = record.integrity_digest()
        entry_digest = _entry_digest(ledger_id, sequence, record_digest, previous, appended_at)
        return cls(
            ledger_id,
            sequence,
            record,
            appended_at,
            previous,
            record_digest,
            entry_digest,
        )

    def to_dict(self, redaction_policy: RedactionPolicy | None = None) -> dict[str, JsonValue]:
        return {
            "ledger_id": str(self.ledger_id),
            "sequence": self.sequence,
            "record": self.record.to_dict(redaction_policy),
            "appended_at": self.appended_at.isoformat(),
            "previous_digest": self.previous_digest,
            "record_digest": self.record_digest,
            "entry_digest": self.entry_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("ledger_id"), str)
                or type(value.get("sequence")) is not int
                or not isinstance(value.get("record"), Mapping)
                or not isinstance(value.get("appended_at"), str)
                or any(
                    not isinstance(value.get(name), str)
                    for name in ("previous_digest", "record_digest", "entry_digest")
                )
            ):
                raise ValueError
            return cls(
                AuditLedgerId(cast(str, value["ledger_id"])),
                cast(int, value["sequence"]),
                AuditRecord.from_dict(cast(Mapping[str, object], value["record"])),
                datetime.fromisoformat(cast(str, value["appended_at"])),
                cast(str, value["previous_digest"]),
                cast(str, value["record_digest"]),
                cast(str, value["entry_digest"]),
            )
        except (
            TypeError,
            ValueError,
            AuditValidationError,
            AuditSerializationError,
            AuditLedgerError,
            AuditLedgerVerificationError,
        ) as error:
            raise AuditSerializationError(
                "Invalid serialized audit ledger entry", cause=error
            ) from error


class AuditLedgerVerificationFailure(_AuditEnum):
    LEDGER_ID_MISMATCH = "ledger_id_mismatch"
    GENESIS_MISMATCH = "genesis_mismatch"
    SEQUENCE_MISMATCH = "sequence_mismatch"
    PREVIOUS_DIGEST_MISMATCH = "previous_digest_mismatch"
    RECORD_DIGEST_MISMATCH = "record_digest_mismatch"
    ENTRY_DIGEST_MISMATCH = "entry_digest_mismatch"
    APPENDED_TIME_REGRESSION = "appended_time_regression"
    DUPLICATE_RECORD_ID = "duplicate_record_id"
    HEAD_DIGEST_MISMATCH = "head_digest_mismatch"
    ENTRY_COUNT_MISMATCH = "entry_count_mismatch"


@dataclass(frozen=True, slots=True)
class AuditLedgerVerificationResult:
    valid: bool
    checked_entry_count: int
    ledger_id: AuditLedgerId
    first_sequence: int | None
    last_sequence: int | None
    head_digest: str | None
    failure: AuditLedgerVerificationFailure | None = None
    failure_sequence: int | None = None

    def __post_init__(self) -> None:
        if type(self.valid) is not bool or type(self.ledger_id) is not AuditLedgerId:
            raise AuditLedgerError("Invalid audit ledger verification result")
        _nonnegative_integer(self.checked_entry_count, "checked_entry_count")
        for name in ("first_sequence", "last_sequence", "failure_sequence"):
            item = getattr(self, name)
            if item is not None:
                _positive_integer(item, name)
        if self.head_digest is not None:
            _digest(self.head_digest, "head_digest")
        if self.failure is not None and type(self.failure) is not AuditLedgerVerificationFailure:
            raise AuditLedgerError("Invalid audit ledger verification failure")
        if self.valid:
            if self.failure is not None or self.failure_sequence is not None:
                raise AuditLedgerError("Inconsistent audit ledger verification result")
            if self.checked_entry_count == 0:
                if any(
                    item is not None
                    for item in (self.first_sequence, self.last_sequence, self.head_digest)
                ):
                    raise AuditLedgerError("Inconsistent empty audit ledger verification result")
            elif (
                self.first_sequence != 1
                or self.last_sequence != self.checked_entry_count
                or self.head_digest is None
            ):
                raise AuditLedgerError("Inconsistent valid audit ledger verification result")
        elif self.failure is None:
            raise AuditLedgerError("Invalid verification result requires failure")

    def require_valid(self) -> None:
        if not self.valid:
            details: dict[str, JsonValue] = {
                "failure": cast(AuditLedgerVerificationFailure, self.failure).value
            }
            if self.failure_sequence is not None:
                details["failure_sequence"] = self.failure_sequence
            raise AuditLedgerVerificationError("Audit ledger verification failed", details=details)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "valid": self.valid,
            "checked_entry_count": self.checked_entry_count,
            "ledger_id": str(self.ledger_id),
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "head_digest": self.head_digest,
            "failure": self.failure.value if self.failure is not None else None,
            "failure_sequence": self.failure_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or type(value.get("valid")) is not bool
                or type(value.get("checked_entry_count")) is not int
                or not isinstance(value.get("ledger_id"), str)
                or value.get("first_sequence") is not None
                and type(value.get("first_sequence")) is not int
                or value.get("last_sequence") is not None
                and type(value.get("last_sequence")) is not int
                or value.get("head_digest") is not None
                and not isinstance(value.get("head_digest"), str)
                or value.get("failure") is not None
                and not isinstance(value.get("failure"), str)
                or value.get("failure_sequence") is not None
                and type(value.get("failure_sequence")) is not int
            ):
                raise ValueError
            failure = value.get("failure")
            return cls(
                cast(bool, value["valid"]),
                cast(int, value["checked_entry_count"]),
                AuditLedgerId(cast(str, value["ledger_id"])),
                cast(int | None, value.get("first_sequence")),
                cast(int | None, value.get("last_sequence")),
                cast(str | None, value.get("head_digest")),
                AuditLedgerVerificationFailure.parse(cast(str, failure))
                if failure is not None
                else None,
                cast(int | None, value.get("failure_sequence")),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            AuditValidationError,
            AuditLedgerError,
            AuditLedgerVerificationError,
        ) as error:
            raise AuditSerializationError(
                "Invalid serialized audit ledger verification result", cause=error
            ) from error


@dataclass(frozen=True, slots=True)
class AuditLedgerStatus:
    ledger_id: AuditLedgerId
    entry_count: int
    maximum_entries: int
    next_sequence: int
    head_digest: str | None
    closed: bool
    created_at: datetime
    last_appended_at: datetime | None

    def __post_init__(self) -> None:
        if type(self.ledger_id) is not AuditLedgerId or type(self.closed) is not bool:
            raise AuditLedgerError("Invalid audit ledger status")
        count = _nonnegative_integer(self.entry_count, "entry_count")
        capacity = _positive_integer(self.maximum_entries, "maximum_entries")
        next_sequence = _positive_integer(self.next_sequence, "next_sequence")
        if count > capacity or next_sequence != count + 1:
            raise AuditLedgerError("Inconsistent audit ledger status")
        created = _ledger_aware(self.created_at, "created_at")
        if self.last_appended_at is not None:
            appended = _ledger_aware(self.last_appended_at, "last_appended_at")
            if appended < created:
                raise AuditLedgerError("Audit ledger append time precedes creation")
        if count == 0:
            if self.head_digest is not None or self.last_appended_at is not None:
                raise AuditLedgerError("Inconsistent empty audit ledger status")
        elif self.head_digest is None or self.last_appended_at is None:
            raise AuditLedgerError("Inconsistent populated audit ledger status")
        else:
            _digest(self.head_digest, "head_digest")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "ledger_id": str(self.ledger_id),
            "entry_count": self.entry_count,
            "maximum_entries": self.maximum_entries,
            "next_sequence": self.next_sequence,
            "head_digest": self.head_digest,
            "closed": self.closed,
            "created_at": self.created_at.isoformat(),
            "last_appended_at": self.last_appended_at.isoformat()
            if self.last_appended_at is not None
            else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("ledger_id"), str)
                or type(value.get("entry_count")) is not int
                or type(value.get("maximum_entries")) is not int
                or type(value.get("next_sequence")) is not int
                or value.get("head_digest") is not None
                and not isinstance(value.get("head_digest"), str)
                or type(value.get("closed")) is not bool
                or not isinstance(value.get("created_at"), str)
                or value.get("last_appended_at") is not None
                and not isinstance(value.get("last_appended_at"), str)
            ):
                raise ValueError
            last = value.get("last_appended_at")
            return cls(
                AuditLedgerId(cast(str, value["ledger_id"])),
                cast(int, value["entry_count"]),
                cast(int, value["maximum_entries"]),
                cast(int, value["next_sequence"]),
                cast(str | None, value.get("head_digest")),
                cast(bool, value["closed"]),
                datetime.fromisoformat(cast(str, value["created_at"])),
                datetime.fromisoformat(last) if isinstance(last, str) else None,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            AuditLedgerError,
            AuditLedgerVerificationError,
        ) as error:
            raise AuditSerializationError(
                "Invalid serialized audit ledger status", cause=error
            ) from error


class AuditLedger(Protocol):
    async def append(self, record: AuditRecord) -> AuditLedgerEntry: ...

    async def get(self, sequence: int) -> AuditLedgerEntry: ...

    async def get_optional(self, sequence: int) -> AuditLedgerEntry | None: ...

    async def get_by_record_id(self, record_id: AuditRecordId) -> AuditLedgerEntry: ...

    async def get_optional_by_record_id(
        self, record_id: AuditRecordId
    ) -> AuditLedgerEntry | None: ...

    async def entries(self) -> tuple[AuditLedgerEntry, ...]: ...

    async def verify(self) -> AuditLedgerVerificationResult: ...

    async def status(self) -> AuditLedgerStatus: ...

    async def close(self) -> None: ...


class InMemoryAuditLedger:
    def __init__(
        self,
        *,
        ledger_id: AuditLedgerId | None = None,
        maximum_entries: int = 100_000,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if ledger_id is not None and type(ledger_id) is not AuditLedgerId:
            raise AuditLedgerError("Invalid audit ledger identifier")
        capacity = _positive_integer(maximum_entries, "maximum_entries")
        if capacity > _MAX_LEDGER_ENTRIES:
            raise AuditLedgerCapacityError(
                "Audit ledger capacity exceeds maximum",
                details={"maximum_entries": _MAX_LEDGER_ENTRIES},
            )
        if not callable(clock):
            raise AuditLedgerError("Invalid audit ledger clock")
        try:
            created_at = _aware(clock(), "created_at")
        except AuditValidationError as error:
            raise AuditLedgerError("Invalid audit ledger clock", cause=error) from error
        except Exception as error:
            raise AuditLedgerError("Audit ledger clock failed") from error
        self._ledger_id = ledger_id if ledger_id is not None else AuditLedgerId()
        self._maximum_entries = capacity
        self._clock = clock
        self._created_at = created_at
        self._entries: list[AuditLedgerEntry] = []
        self._sequence_index: dict[int, AuditLedgerEntry] = {}
        self._record_index: dict[AuditRecordId, AuditLedgerEntry] = {}
        self._head_digest: str | None = None
        self._last_appended_at: datetime | None = None
        self._entry_count = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def append(self, record: AuditRecord) -> AuditLedgerEntry:
        if type(record) is not AuditRecord:
            raise AuditLedgerError("Invalid audit ledger record")
        async with self._lock:
            if self._closed:
                raise AuditLedgerClosedError(
                    "Audit ledger is closed", details={"ledger_id": str(self._ledger_id)}
                )
            if record.record_id in self._record_index:
                raise ResourceConflictError(
                    "Audit record already exists", details={"record_id": str(record.record_id)}
                )
            if self._entry_count >= self._maximum_entries:
                raise AuditLedgerCapacityError(
                    "Audit ledger capacity reached",
                    details={
                        "entry_count": self._entry_count,
                        "maximum_entries": self._maximum_entries,
                    },
                )
            try:
                appended_at = _aware(self._clock(), "appended_at")
            except AuditValidationError as error:
                raise AuditLedgerError("Invalid audit ledger clock", cause=error) from error
            except Exception as error:
                raise AuditLedgerError("Audit ledger clock failed") from error
            floor = self._last_appended_at or self._created_at
            if appended_at < floor:
                raise AuditLedgerError("Audit ledger clock regressed")
            sequence = self._entry_count + 1
            previous = self._head_digest or _GENESIS_DIGEST
            entry = AuditLedgerEntry.create(
                ledger_id=self._ledger_id,
                sequence=sequence,
                record=record,
                appended_at=appended_at,
                previous_digest=previous,
            )
            self._entries.append(entry)
            self._sequence_index[sequence] = entry
            self._record_index[record.record_id] = entry
            self._head_digest = entry.entry_digest
            self._last_appended_at = appended_at
            self._entry_count = sequence
            return entry

    async def get(self, sequence: int) -> AuditLedgerEntry:
        _positive_integer(sequence, "sequence")
        async with self._lock:
            try:
                return self._sequence_index[sequence]
            except KeyError as error:
                raise ResourceNotFoundError(
                    "Audit ledger sequence not found", details={"sequence": sequence}
                ) from error

    async def get_optional(self, sequence: int) -> AuditLedgerEntry | None:
        _positive_integer(sequence, "sequence")
        async with self._lock:
            return self._sequence_index.get(sequence)

    async def get_by_record_id(self, record_id: AuditRecordId) -> AuditLedgerEntry:
        if type(record_id) is not AuditRecordId:
            raise AuditLedgerError("Invalid audit record identifier")
        async with self._lock:
            try:
                return self._record_index[record_id]
            except KeyError as error:
                raise ResourceNotFoundError(
                    "Audit record not found", details={"record_id": str(record_id)}
                ) from error

    async def get_optional_by_record_id(self, record_id: AuditRecordId) -> AuditLedgerEntry | None:
        if type(record_id) is not AuditRecordId:
            raise AuditLedgerError("Invalid audit record identifier")
        async with self._lock:
            return self._record_index.get(record_id)

    async def entries(self) -> tuple[AuditLedgerEntry, ...]:
        async with self._lock:
            return tuple(self._entries)

    async def status(self) -> AuditLedgerStatus:
        async with self._lock:
            return AuditLedgerStatus(
                self._ledger_id,
                self._entry_count,
                self._maximum_entries,
                self._entry_count + 1,
                self._head_digest,
                self._closed,
                self._created_at,
                self._last_appended_at,
            )

    async def verify(self) -> AuditLedgerVerificationResult:
        async with self._lock:
            entries = tuple(self._entries)
            ledger_id = self._ledger_id
            head_digest = self._head_digest
            stored_count = self._entry_count
        return _verify_chain(entries, ledger_id, head_digest, stored_count)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True


def _invalid_verification(
    ledger_id: AuditLedgerId,
    checked_count: int,
    failure: AuditLedgerVerificationFailure,
    sequence: int | None,
    entries: tuple[AuditLedgerEntry, ...],
    head_digest: str | None,
) -> AuditLedgerVerificationResult:
    return AuditLedgerVerificationResult(
        False,
        checked_count,
        ledger_id,
        entries[0].sequence if entries else None,
        entries[-1].sequence if entries else None,
        head_digest,
        failure,
        sequence,
    )


def _verify_chain(
    entries: tuple[AuditLedgerEntry, ...],
    ledger_id: AuditLedgerId,
    head_digest: str | None,
    stored_count: int,
) -> AuditLedgerVerificationResult:
    previous_digest = _GENESIS_DIGEST
    previous_time: datetime | None = None
    record_ids: set[AuditRecordId] = set()
    for index, entry in enumerate(entries, start=1):
        if entry.ledger_id != ledger_id:
            return _invalid_verification(
                ledger_id,
                index,
                AuditLedgerVerificationFailure.LEDGER_ID_MISMATCH,
                index,
                entries,
                head_digest,
            )
        if entry.sequence != index:
            return _invalid_verification(
                ledger_id,
                index,
                AuditLedgerVerificationFailure.SEQUENCE_MISMATCH,
                index,
                entries,
                head_digest,
            )
        expected_previous_failure = (
            AuditLedgerVerificationFailure.GENESIS_MISMATCH
            if index == 1
            else AuditLedgerVerificationFailure.PREVIOUS_DIGEST_MISMATCH
        )
        if entry.previous_digest != previous_digest:
            return _invalid_verification(
                ledger_id, index, expected_previous_failure, index, entries, head_digest
            )
        if entry.record.record_id in record_ids:
            return _invalid_verification(
                ledger_id,
                index,
                AuditLedgerVerificationFailure.DUPLICATE_RECORD_ID,
                index,
                entries,
                head_digest,
            )
        record_ids.add(entry.record.record_id)
        if entry.record_digest != entry.record.integrity_digest():
            return _invalid_verification(
                ledger_id,
                index,
                AuditLedgerVerificationFailure.RECORD_DIGEST_MISMATCH,
                index,
                entries,
                head_digest,
            )
        expected_entry_digest = _entry_digest(
            entry.ledger_id,
            entry.sequence,
            entry.record_digest,
            entry.previous_digest,
            entry.appended_at,
        )
        if entry.entry_digest != expected_entry_digest:
            return _invalid_verification(
                ledger_id,
                index,
                AuditLedgerVerificationFailure.ENTRY_DIGEST_MISMATCH,
                index,
                entries,
                head_digest,
            )
        if previous_time is not None and entry.appended_at < previous_time:
            return _invalid_verification(
                ledger_id,
                index,
                AuditLedgerVerificationFailure.APPENDED_TIME_REGRESSION,
                index,
                entries,
                head_digest,
            )
        previous_digest = entry.entry_digest
        previous_time = entry.appended_at
    if entries and head_digest != entries[-1].entry_digest:
        return _invalid_verification(
            ledger_id,
            len(entries),
            AuditLedgerVerificationFailure.HEAD_DIGEST_MISMATCH,
            len(entries),
            entries,
            head_digest,
        )
    if len(entries) != stored_count:
        return _invalid_verification(
            ledger_id,
            len(entries),
            AuditLedgerVerificationFailure.ENTRY_COUNT_MISMATCH,
            None,
            entries,
            head_digest,
        )
    if not entries:
        return AuditLedgerVerificationResult(True, 0, ledger_id, None, None, None)
    return AuditLedgerVerificationResult(
        True,
        len(entries),
        ledger_id,
        1,
        len(entries),
        cast(str, head_digest),
    )
