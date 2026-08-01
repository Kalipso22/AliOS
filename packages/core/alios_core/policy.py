"""Deterministic, in-memory authorization policy evaluation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from time import perf_counter
from types import MappingProxyType
from typing import Protocol

from .errors import PolicyError
from .events import PolicyEvaluated
from .ids import CorrelationId, TenantId, UserId
from .types import JsonValue, PolicyDecision, utc_now


def _frozen(value: Mapping[str, JsonValue] | None) -> Mapping[str, JsonValue]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class PolicySubject:
    kind: str
    identifier: str
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _frozen(self.attributes))


@dataclass(frozen=True, slots=True)
class PolicyResource:
    kind: str
    identifier: str
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _frozen(self.attributes))


@dataclass(frozen=True, slots=True)
class PolicyAction:
    name: str
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _frozen(self.attributes))


@dataclass(frozen=True, slots=True)
class PolicyContext:
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    environment: str = "development"
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)
    request_time: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _frozen(self.attributes))


class ConditionSource(StrEnum):
    SUBJECT = "subject"
    RESOURCE = "resource"
    ACTION = "action"
    CONTEXT = "context"


class ConditionOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    CONTAINS = "contains"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"


@dataclass(frozen=True, slots=True)
class PolicyCondition:
    source: ConditionSource
    path: str
    operator: ConditionOperator
    expected_value: JsonValue = None


@dataclass(frozen=True, slots=True)
class PolicyRule:
    rule_id: str
    name: str
    effect: PolicyDecision
    priority: int = 0
    subject_kind_pattern: str = "*"
    subject_identifier_pattern: str = "*"
    resource_kind_pattern: str = "*"
    resource_identifier_pattern: str = "*"
    action_pattern: str = "*"
    conditions: tuple[PolicyCondition, ...] = ()
    reason: str = ""
    enabled: bool = True
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen(self.metadata))


@dataclass(frozen=True, slots=True)
class PolicyEvaluationTrace:
    considered_rule_ids: tuple[str, ...]
    matched_rule_ids: tuple[str, ...]
    rejected_rules: Mapping[str, str]
    winning_rule_id: str | None
    decision: PolicyDecision
    evaluated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "rejected_rules", MappingProxyType(dict(self.rejected_rules)))


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str
    winning_rule_id: str | None
    matched_rule_ids: tuple[str, ...]
    trace: PolicyEvaluationTrace
    duration: timedelta


class PolicyEvaluator(Protocol):
    async def evaluate(
        self,
        subject: PolicySubject,
        resource: PolicyResource,
        action: PolicyAction,
        context: PolicyContext | None = None,
        *,
        correlation_id: CorrelationId | None = None,
    ) -> PolicyResult: ...


def _lookup(source: object, path: str) -> object:
    current: object = source
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part, _MISSING)
        else:
            current = getattr(current, part, _MISSING)
        if current is _MISSING:
            return _MISSING
    return current


_MISSING = object()


def _matches(
    condition: PolicyCondition,
    subject: PolicySubject,
    resource: PolicyResource,
    action: PolicyAction,
    context: PolicyContext,
) -> bool:
    value = _lookup(
        {
            ConditionSource.SUBJECT: subject,
            ConditionSource.RESOURCE: resource,
            ConditionSource.ACTION: action,
            ConditionSource.CONTEXT: context,
        }[condition.source],
        condition.path,
    )
    expected = condition.expected_value
    op = condition.operator
    if op is ConditionOperator.EXISTS:
        return value is not _MISSING
    if op is ConditionOperator.NOT_EXISTS:
        return value is _MISSING
    if value is _MISSING:
        return op is ConditionOperator.NOT_EQUALS
    if op is ConditionOperator.EQUALS:
        return value == expected
    if op is ConditionOperator.NOT_EQUALS:
        return value != expected
    if op in (ConditionOperator.IN, ConditionOperator.NOT_IN):
        if not isinstance(expected, list):
            raise PolicyError("Membership condition requires a list")
        return (value in expected) if op is ConditionOperator.IN else (value not in expected)
    if op in (
        ConditionOperator.STARTS_WITH,
        ConditionOperator.ENDS_WITH,
        ConditionOperator.CONTAINS,
    ):
        if not isinstance(value, str) or not isinstance(expected, str):
            raise PolicyError("String condition requires strings")
        return (
            value.startswith(expected)
            if op is ConditionOperator.STARTS_WITH
            else value.endswith(expected)
            if op is ConditionOperator.ENDS_WITH
            else expected in value
        )
    if not isinstance(value, (int, float)) or not isinstance(expected, (int, float)):
        raise PolicyError("Comparison condition requires numbers")
    return (
        value > expected
        if op is ConditionOperator.GREATER_THAN
        else value >= expected
        if op is ConditionOperator.GREATER_THAN_OR_EQUAL
        else value < expected
        if op is ConditionOperator.LESS_THAN
        else value <= expected
    )


class InMemoryPolicyEvaluator:
    def __init__(
        self,
        rules: tuple[PolicyRule, ...] = (),
        event_publisher: Callable[[PolicyEvaluated], Awaitable[object]] | None = None,
    ) -> None:
        self._rules = tuple(rules)
        self._publisher = event_publisher
        self._lock = asyncio.Lock()
        if len({r.rule_id for r in rules}) != len(rules):
            raise PolicyError("Duplicate policy rule IDs")

    async def list_rules(self) -> tuple[PolicyRule, ...]:
        async with self._lock:
            return self._rules

    async def add_rule(self, rule: PolicyRule) -> None:
        async with self._lock:
            if any(item.rule_id == rule.rule_id for item in self._rules):
                raise PolicyError("Duplicate policy rule ID")
            self._rules = (*self._rules, rule)

    async def remove_rule(self, rule_id: str) -> None:
        async with self._lock:
            self._rules = tuple(item for item in self._rules if item.rule_id != rule_id)

    async def replace_rules(self, rules: tuple[PolicyRule, ...]) -> None:
        if len({r.rule_id for r in rules}) != len(rules):
            raise PolicyError("Duplicate policy rule IDs")
        async with self._lock:
            self._rules = tuple(rules)

    async def evaluate(
        self,
        subject: PolicySubject,
        resource: PolicyResource,
        action: PolicyAction,
        context: PolicyContext | None = None,
        *,
        correlation_id: CorrelationId | None = None,
    ) -> PolicyResult:
        start = perf_counter()
        context = context or PolicyContext()
        async with self._lock:
            rules = self._rules
        considered = []
        matched = []
        rejected: dict[str, str] = {}
        candidates = []
        for rule in rules:
            considered.append(rule.rule_id)
            if not rule.enabled:
                rejected[rule.rule_id] = "disabled"
                continue
            patterns = (
                fnmatchcase(subject.kind, rule.subject_kind_pattern),
                fnmatchcase(subject.identifier, rule.subject_identifier_pattern),
                fnmatchcase(resource.kind, rule.resource_kind_pattern),
                fnmatchcase(resource.identifier, rule.resource_identifier_pattern),
                fnmatchcase(action.name, rule.action_pattern),
            )
            if not all(patterns):
                rejected[rule.rule_id] = "pattern mismatch"
                continue
            if not all(_matches(c, subject, resource, action, context) for c in rule.conditions):
                rejected[rule.rule_id] = "condition mismatch"
                continue
            matched.append(rule.rule_id)
            candidates.append(rule)
        winner = (
            sorted(
                candidates,
                key=lambda r: (-r.priority, 0 if r.effect is PolicyDecision.DENY else 1, r.rule_id),
            )[0]
            if candidates
            else None
        )
        decision = winner.effect if winner else PolicyDecision.DENY
        trace = PolicyEvaluationTrace(
            tuple(considered),
            tuple(matched),
            rejected,
            winner.rule_id if winner else None,
            decision,
            utc_now(),
        )
        result = PolicyResult(
            decision,
            winner.reason or winner.name if winner else "No matching policy rule",
            winner.rule_id if winner else None,
            tuple(matched),
            trace,
            timedelta(seconds=perf_counter() - start),
        )
        if self._publisher:
            await self._publisher(
                PolicyEvaluated(
                    correlation_id=correlation_id or CorrelationId(),
                    decision=decision.value,
                    subject_kind=subject.kind,
                    resource_kind=resource.kind,
                    action_name=action.name,
                    winning_rule_id=result.winning_rule_id,
                    matched_rule_count=len(matched),
                )
            )
        return result
