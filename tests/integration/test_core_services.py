import pytest
from alios_core import (
    AsyncEventBus,
    ConfigurationLoader,
    InMemoryPolicyEvaluator,
    PolicyAction,
    PolicyResource,
    PolicyRule,
    PolicySubject,
)
from alios_core.events import PolicyEvaluated
from alios_core.types import PolicyDecision


@pytest.mark.asyncio
async def test_policy_publishes_through_event_bus() -> None:
    async with AsyncEventBus(history_capacity=4) as bus:
        evaluator = InMemoryPolicyEvaluator(
            (PolicyRule("allow", "allow", PolicyDecision.ALLOW),), bus.publish
        )
        result = await evaluator.evaluate(
            PolicySubject("user", "u"), PolicyResource("doc", "d"), PolicyAction("read")
        )
        history = await bus.history(event_type=PolicyEvaluated)
    assert result.decision is PolicyDecision.ALLOW and len(history) == 1


def test_configuration_configures_event_bus() -> None:
    snapshot = ConfigurationLoader().load(overrides={"events": {"queue_capacity": 2}})
    assert snapshot.require("events.queue_capacity", expected_type=int) == 2
