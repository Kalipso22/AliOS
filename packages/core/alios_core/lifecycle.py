"""Concurrency-safe asynchronous component lifecycle management."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .errors import LifecycleError
from .types import HealthStatus, Metadata, utc_now


class LifecycleState(StrEnum):
    CREATED = "created"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    previous: LifecycleState
    current: LifecycleState
    timestamp: datetime
    reason: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LifecycleHealth:
    component: str
    state: LifecycleState
    status: HealthStatus
    checked_at: datetime
    message: str | None = None
    details: Metadata = field(default_factory=dict)


class LifecycleComponent(Protocol):
    name: str
    dependencies: tuple[str, ...]
    state: LifecycleState

    async def initialize(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def close(self) -> None: ...
    async def health(self) -> LifecycleHealth: ...


class ManagedLifecycleComponent:
    _allowed = {
        LifecycleState.CREATED: {LifecycleState.INITIALIZING},
        LifecycleState.INITIALIZING: {LifecycleState.INITIALIZED, LifecycleState.FAILED},
        LifecycleState.INITIALIZED: {LifecycleState.STARTING, LifecycleState.CLOSING},
        LifecycleState.STARTING: {LifecycleState.RUNNING, LifecycleState.FAILED},
        LifecycleState.RUNNING: {LifecycleState.STOPPING, LifecycleState.FAILED},
        LifecycleState.STOPPING: {LifecycleState.STOPPED, LifecycleState.FAILED},
        LifecycleState.STOPPED: {LifecycleState.STARTING, LifecycleState.CLOSING},
        LifecycleState.FAILED: {LifecycleState.STOPPING, LifecycleState.CLOSING},
        LifecycleState.CLOSING: {LifecycleState.CLOSED, LifecycleState.FAILED},
        LifecycleState.CLOSED: set(),
    }

    def __init__(
        self, name: str, dependencies: tuple[str, ...] = (), timeout: float | None = None
    ) -> None:
        self.name, self.dependencies, self.timeout = name, dependencies, timeout
        self.state = LifecycleState.CREATED
        self.last_failure: Exception | None = None
        self.history: list[LifecycleTransition] = []
        self._lock = asyncio.Lock()

    async def _initialize(self) -> None:
        pass

    async def _start(self) -> None:
        pass

    async def _stop(self) -> None:
        pass

    async def _close(self) -> None:
        pass

    async def _health(self) -> LifecycleHealth:
        return LifecycleHealth(
            self.name,
            self.state,
            HealthStatus.HEALTHY if self.state == LifecycleState.RUNNING else HealthStatus.DEGRADED,
            utc_now(),
        )

    def _transition(
        self, state: LifecycleState, reason: str | None = None, error: Exception | None = None
    ) -> None:
        if state not in self._allowed[self.state]:
            raise LifecycleError("Invalid lifecycle transition", {"from": self.state, "to": state})
        previous = self.state
        self.state = state
        self.history.append(
            LifecycleTransition(previous, state, utc_now(), reason, getattr(error, "code", None))
        )

    async def _invoke(
        self,
        target: LifecycleState,
        success: LifecycleState,
        hook: Callable[[], Awaitable[None]],
    ) -> None:
        self._transition(target)
        try:
            await asyncio.wait_for(hook(), self.timeout) if self.timeout else await hook()
            self._transition(success)
        except Exception as exc:
            self.last_failure = exc
            self._transition(LifecycleState.FAILED, str(exc), exc)
            raise LifecycleError("Lifecycle operation failed", cause=exc) from exc

    async def initialize(self) -> None:
        async with self._lock:
            if self.state == LifecycleState.CREATED:
                await self._invoke(
                    LifecycleState.INITIALIZING, LifecycleState.INITIALIZED, self._initialize
                )

    async def start(self) -> None:
        async with self._lock:
            if self.state == LifecycleState.RUNNING:
                return
            if self.state == LifecycleState.CREATED:
                await self._invoke(
                    LifecycleState.INITIALIZING, LifecycleState.INITIALIZED, self._initialize
                )
            await self._invoke(LifecycleState.STARTING, LifecycleState.RUNNING, self._start)

    async def stop(self) -> None:
        async with self._lock:
            if self.state in {LifecycleState.STOPPED, LifecycleState.CLOSED}:
                return
            if self.state == LifecycleState.RUNNING:
                await self._invoke(LifecycleState.STOPPING, LifecycleState.STOPPED, self._stop)

    async def close(self) -> None:
        await self.stop()
        async with self._lock:
            if self.state == LifecycleState.CLOSED:
                return
            await self._invoke(LifecycleState.CLOSING, LifecycleState.CLOSED, self._close)

    async def health(self) -> LifecycleHealth:
        return await self._health()


class LifecycleManager:
    def __init__(self) -> None:
        self._components: dict[str, LifecycleComponent] = {}

    def register(self, component: LifecycleComponent) -> None:
        if component.name in self._components:
            raise LifecycleError("Duplicate component", {"name": component.name})
        self._components[component.name] = component

    def get(self, name: str) -> LifecycleComponent:
        return self._components[name]

    def _order(self) -> list[LifecycleComponent]:
        result: list[LifecycleComponent] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise LifecycleError("Lifecycle dependency cycle", {"name": name})
            if name in visited:
                return
            if name not in self._components:
                raise LifecycleError("Missing lifecycle dependency", {"name": name})
            visiting.add(name)
            for dep in sorted(self._components[name].dependencies):
                visit(dep)
            visiting.remove(name)
            visited.add(name)
            result.append(self._components[name])

        for name in sorted(self._components):
            visit(name)
        return result

    async def initialize_all(self) -> None:
        for component in self._order():
            await component.initialize()

    async def start_all(self) -> None:
        started: list[LifecycleComponent] = []
        try:
            for component in self._order():
                await component.start()
                started.append(component)
        except Exception as startup_error:
            failures: list[Exception] = []
            for component in reversed(started):
                try:
                    await component.stop()
                except Exception as cleanup_error:
                    failures.append(cleanup_error)
            if failures:
                raise LifecycleError(
                    "Lifecycle startup rollback failed",
                    {"cleanup_failures": len(failures)},
                    startup_error,
                ) from startup_error
            raise

    async def stop_all(self) -> None:
        failures: list[Exception] = []
        for component in reversed(self._order()):
            try:
                await component.stop()
            except Exception as error:
                failures.append(error)
        if failures:
            raise LifecycleError(
                "Lifecycle shutdown failed", {"failures": len(failures)}, failures[0]
            )

    async def close_all(self) -> None:
        failures: list[Exception] = []
        for component in reversed(self._order()):
            try:
                await component.close()
            except Exception as error:
                failures.append(error)
        if failures:
            raise LifecycleError("Lifecycle close failed", {"failures": len(failures)}, failures[0])

    async def health(self) -> tuple[LifecycleHealth, ...]:
        return tuple([await c.health() for c in self._order()])
