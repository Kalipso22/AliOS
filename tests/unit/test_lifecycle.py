import asyncio
from typing import cast

import pytest
from alios_core.errors import LifecycleError
from alios_core.lifecycle import LifecycleManager, LifecycleState, ManagedLifecycleComponent


class Component(ManagedLifecycleComponent):
    def __init__(self, name: str, events: list[str], dependencies: tuple[str, ...] = ()) -> None:
        super().__init__(name, dependencies)
        self.events = events

    async def _initialize(self) -> None:
        self.events.append(f"initialize:{self.name}")

    async def _start(self) -> None:
        self.events.append(f"start:{self.name}")

    async def _stop(self) -> None:
        self.events.append(f"stop:{self.name}")

    async def _close(self) -> None:
        self.events.append(f"close:{self.name}")


class FailingComponent(Component):
    async def _start(self) -> None:
        raise ValueError("start failed")


def test_component_lifecycle_and_idempotent_shutdown() -> None:
    async def run() -> None:
        component = Component("a", [])
        assert component.state == LifecycleState.CREATED
        await component.start()
        await component.stop()
        await component.stop()
        await component.close()
        await component.close()
        assert cast(LifecycleState, component.state) == LifecycleState.CLOSED
        assert len(component.history) == 8

    asyncio.run(run())


def test_manager_orders_dependencies_and_stops_in_reverse() -> None:
    async def run() -> None:
        events: list[str] = []
        manager = LifecycleManager()
        manager.register(Component("child", events, ("root",)))
        manager.register(Component("root", events))
        await manager.start_all()
        await manager.stop_all()
        assert events == [
            "initialize:root",
            "start:root",
            "initialize:child",
            "start:child",
            "stop:child",
            "stop:root",
        ]

    asyncio.run(run())


def test_manager_rejects_cycle() -> None:
    manager = LifecycleManager()
    manager.register(Component("a", [], ("b",)))
    manager.register(Component("b", [], ("a",)))
    with pytest.raises(LifecycleError):
        asyncio.run(manager.start_all())


def test_startup_failure_rolls_back_started_components() -> None:
    async def run() -> None:
        events: list[str] = []
        manager = LifecycleManager()
        manager.register(Component("first", events))
        manager.register(FailingComponent("second", events, ("first",)))
        with pytest.raises(LifecycleError):
            await manager.start_all()
        assert "stop:first" in events

    asyncio.run(run())


def test_concurrent_start_calls_only_invoke_hook_once() -> None:
    async def run() -> None:
        events: list[str] = []
        component = Component("only", events)
        await asyncio.gather(component.start(), component.start())
        assert events.count("start:only") == 1

    asyncio.run(run())


def test_invalid_transition_and_health_aggregation() -> None:
    async def run() -> None:
        manager = LifecycleManager()
        component = Component("only", [])
        manager.register(component)
        with pytest.raises(LifecycleError):
            await component.stop()
            await component.close()
        await component.initialize()
        health = await manager.health()
        assert health[0].component == "only"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (LifecycleState.CREATED, LifecycleState.RUNNING),
        (LifecycleState.CREATED, LifecycleState.CLOSED),
        (LifecycleState.INITIALIZING, LifecycleState.RUNNING),
        (LifecycleState.INITIALIZED, LifecycleState.RUNNING),
        (LifecycleState.STARTING, LifecycleState.STOPPED),
        (LifecycleState.RUNNING, LifecycleState.CLOSED),
        (LifecycleState.STOPPING, LifecycleState.RUNNING),
        (LifecycleState.STOPPED, LifecycleState.RUNNING),
        (LifecycleState.CLOSING, LifecycleState.RUNNING),
        (LifecycleState.CLOSED, LifecycleState.STARTING),
    ],
)
def test_invalid_transition_categories(initial: LifecycleState, target: LifecycleState) -> None:
    component = Component("only", [])
    component.state = initial
    with pytest.raises(LifecycleError):
        component._transition(target)


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (LifecycleState.CREATED, LifecycleState.INITIALIZING),
        (LifecycleState.INITIALIZING, LifecycleState.INITIALIZED),
        (LifecycleState.INITIALIZING, LifecycleState.FAILED),
        (LifecycleState.INITIALIZED, LifecycleState.STARTING),
        (LifecycleState.STARTING, LifecycleState.RUNNING),
        (LifecycleState.STARTING, LifecycleState.FAILED),
        (LifecycleState.RUNNING, LifecycleState.STOPPING),
        (LifecycleState.RUNNING, LifecycleState.FAILED),
        (LifecycleState.STOPPING, LifecycleState.STOPPED),
        (LifecycleState.STOPPING, LifecycleState.FAILED),
        (LifecycleState.STOPPED, LifecycleState.STARTING),
        (LifecycleState.STOPPED, LifecycleState.CLOSING),
        (LifecycleState.INITIALIZED, LifecycleState.CLOSING),
        (LifecycleState.FAILED, LifecycleState.STOPPING),
        (LifecycleState.FAILED, LifecycleState.CLOSING),
        (LifecycleState.CLOSING, LifecycleState.CLOSED),
        (LifecycleState.CLOSING, LifecycleState.FAILED),
    ],
)
def test_every_allowed_transition(initial: LifecycleState, target: LifecycleState) -> None:
    component = Component("only", [])
    component.state = initial
    component._transition(target)
    assert component.state is target
