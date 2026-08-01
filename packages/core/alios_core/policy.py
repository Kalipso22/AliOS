"""Deterministic in-memory policy evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Protocol

from .types import Metadata, PolicyDecision


@dataclass(frozen=True, slots=True)
class PolicySubject:
    kind: str
    identifier: str


@dataclass(frozen=True, slots=True)
class PolicyResource:
    kind: str
    identifier: str


@dataclass(frozen=True, slots=True)
class PolicyContext:
    attributes: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyRule:
    name: str
    decision: PolicyDecision
    subject: str = "*"
    resource: str = "*"
    action: str = "*"
    priority: int = 0
    reason: str = ""

    def matches(self, subject: PolicySubject, resource: PolicyResource, action: str) -> bool:
        return all(
            (
                fnmatchcase(subject.identifier, self.subject),
                fnmatchcase(resource.identifier, self.resource),
                fnmatchcase(action, self.action),
            )
        )


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str
    rule_name: str | None
    audit: Metadata = field(default_factory=dict)


class PolicyEvaluator(Protocol):
    async def evaluate(
        self,
        subject: PolicySubject,
        resource: PolicyResource,
        action: str,
        context: PolicyContext | None = None,
    ) -> PolicyResult: ...


class InMemoryPolicyEvaluator:
    def __init__(self, rules: tuple[PolicyRule, ...] = ()) -> None:
        self._rules = rules

    async def evaluate(
        self,
        subject: PolicySubject,
        resource: PolicyResource,
        action: str,
        context: PolicyContext | None = None,
    ) -> PolicyResult:
        del context
        matches = sorted(
            (rule for rule in self._rules if rule.matches(subject, resource, action)),
            key=lambda rule: (rule.priority, rule.decision == PolicyDecision.DENY),
            reverse=True,
        )
        if not matches:
            return PolicyResult(PolicyDecision.DENY, "No matching policy rule", None)
        rule = matches[0]
        return PolicyResult(rule.decision, rule.reason or rule.name, rule.name)
