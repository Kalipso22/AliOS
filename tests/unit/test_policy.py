import pytest
from alios_core.policy import (
    ConditionOperator,
    ConditionSource,
    InMemoryPolicyEvaluator,
    PolicyAction,
    PolicyCondition,
    PolicyResource,
    PolicyRule,
    PolicySubject,
)
from alios_core.types import PolicyDecision


@pytest.mark.asyncio
async def test_default_deny() -> None:
    result = await InMemoryPolicyEvaluator().evaluate(
        PolicySubject("user", "a"), PolicyResource("doc", "b"), PolicyAction("read")
    )
    assert result.decision is PolicyDecision.DENY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operator,expected",
    [
        (ConditionOperator.EQUALS, "x"),
        (ConditionOperator.NOT_EQUALS, "y"),
        (ConditionOperator.IN, ["x", "z"]),
        (ConditionOperator.NOT_IN, ["y"]),
        (ConditionOperator.EXISTS, None),
        (ConditionOperator.STARTS_WITH, "x"),
        (ConditionOperator.ENDS_WITH, "x"),
        (ConditionOperator.CONTAINS, "x"),
    ],
)
async def test_condition_operators(operator: ConditionOperator, expected: object) -> None:
    condition = PolicyCondition(ConditionSource.SUBJECT, "attributes.value", operator, expected)  # type: ignore[arg-type]
    rule = PolicyRule("rule", "allow", PolicyDecision.ALLOW, conditions=(condition,))
    result = await InMemoryPolicyEvaluator((rule,)).evaluate(
        PolicySubject("user", "u", {"value": "x"}), PolicyResource("doc", "d"), PolicyAction("read")
    )
    assert result.decision is PolicyDecision.ALLOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "priority,effect",
    [
        (1, PolicyDecision.ALLOW),
        (2, PolicyDecision.DENY),
        (3, PolicyDecision.ALLOW),
        (4, PolicyDecision.DENY),
    ],
)
async def test_priority(priority: int, effect: PolicyDecision) -> None:
    rule = PolicyRule("r", "rule", effect, priority=priority)
    result = await InMemoryPolicyEvaluator((rule,)).evaluate(
        PolicySubject("*", "u"), PolicyResource("*", "d"), PolicyAction("x")
    )
    assert result.decision is effect


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(24))
async def test_policy_rule_trace_is_deterministic(index: int) -> None:
    rule = PolicyRule(f"rule-{index}", "allow", PolicyDecision.ALLOW)
    result = await InMemoryPolicyEvaluator((rule,)).evaluate(
        PolicySubject("user", f"user-{index}"), PolicyResource("doc", "d"), PolicyAction("read")
    )
    assert result.trace.matched_rule_ids == (rule.rule_id,)
