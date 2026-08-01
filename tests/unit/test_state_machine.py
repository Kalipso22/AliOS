import asyncio

import pytest
from alios_core.errors import InvalidStateTransitionError, ResourceConflictError
from alios_core.types import RunStatus
from alios_runtime.state_machine import TERMINAL, TRANSITIONS, RunStateMachine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,target",
    [(source, target) for source, targets in TRANSITIONS.items() for target in targets],
)
async def test_every_declared_transition_is_valid(source: RunStatus, target: RunStatus) -> None:
    machine = RunStateMachine(status=source)
    transition = await machine.transition(target)
    assert machine.status is target and transition.version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", list(TERMINAL))
async def test_terminal_states_reject_transitions(terminal: RunStatus) -> None:
    machine = RunStateMachine(status=terminal)
    with pytest.raises(InvalidStateTransitionError):
        await machine.transition(RunStatus.RUNNING)


@pytest.mark.asyncio
async def test_transition_conflict_and_history_are_safe() -> None:
    machine = RunStateMachine()
    await machine.transition(RunStatus.QUEUED, expected_version=0, reason="queued", actor="test")
    with pytest.raises(ResourceConflictError):
        await machine.transition(RunStatus.INITIALIZING, expected_version=0)
    assert machine.transition_count == 1 and machine.last_transition is not None


@pytest.mark.asyncio
async def test_concurrent_same_version_has_one_winner() -> None:
    machine = RunStateMachine()
    results = await asyncio.gather(
        machine.transition(RunStatus.QUEUED, expected_version=0),
        machine.transition(RunStatus.INITIALIZING, expected_version=0),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1


@pytest.mark.asyncio
async def test_state_machine_serialization_round_trip() -> None:
    machine = RunStateMachine()
    await machine.transition(RunStatus.QUEUED)
    await machine.transition(RunStatus.INITIALIZING)
    restored = RunStateMachine.from_dict(machine.to_dict())
    assert restored.status is RunStatus.INITIALIZING and restored.version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [status for status in RunStatus if status not in TERMINAL])
async def test_representative_invalid_transition_is_rejected(source: RunStatus) -> None:
    target = next(
        status for status in RunStatus if status not in TRANSITIONS[source] and status is not source
    )
    machine = RunStateMachine(status=source)
    with pytest.raises(InvalidStateTransitionError):
        await machine.transition(target)


@pytest.mark.parametrize("status", list(RunStatus))
def test_state_machine_queries_are_consistent(status: RunStatus) -> None:
    machine = RunStateMachine(status=status)
    assert machine.is_terminal() is (status in TERMINAL)
    assert machine.allowed_transitions() == TRANSITIONS[status]
