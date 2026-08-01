"""Typed asynchronous in-process events."""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from time import perf_counter
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from .errors import EventBusError
from .ids import CorrelationId, EventId, TenantId, UserId
from .types import EventPriority, JsonValue, utc_now

E = TypeVar("E", bound="BaseEvent")
Handler = Callable[[E], Awaitable[None] | None]
Middleware = Callable[[E], Awaitable[E] | E]


def _frozen(values: Mapping[str, JsonValue] | None) -> Mapping[str, JsonValue]:
    return MappingProxyType(dict(values or {}))


@dataclass(frozen=True, slots=True)
class BaseEvent:
    event_id: EventId = field(default_factory=EventId)
    correlation_id: CorrelationId = field(default_factory=CorrelationId)
    occurred_at: datetime = field(default_factory=utc_now)
    source: str = "alios"
    tenant_id: TenantId | None = None
    user_id: UserId | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    priority: EventPriority = EventPriority.NORMAL

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise EventBusError("Event timestamp must be timezone-aware")
        object.__setattr__(self, "metadata", _frozen(self.metadata))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": str(self.event_id),
            "correlation_id": str(self.correlation_id),
            "occurred_at": self.occurred_at.isoformat(),
            "source": self.source,
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "metadata": dict(self.metadata),
            "priority": self.priority.value,
        }


@dataclass(frozen=True, slots=True)
class LifecycleStateChanged(BaseEvent):
    component: str = ""
    previous_state: str = ""
    current_state: str = ""


@dataclass(frozen=True, slots=True)
class RunCreated(BaseEvent):
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class RunStateChanged(BaseEvent):
    run_id: str = ""
    previous_state: str = ""
    current_state: str = ""


@dataclass(frozen=True, slots=True)
class RunCompleted(BaseEvent):
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class RunFailed(BaseEvent):
    run_id: str = ""
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class CheckpointCreated(BaseEvent):
    checkpoint_id: str = ""
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryStarted(BaseEvent):
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryCompleted(BaseEvent):
    run_id: str = ""
    recovery_id: str = ""
    mode: str = ""
    success: bool = False
    failure_code: str | None = None
    checkpoint_id: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyEvaluated(BaseEvent):
    decision: str = "deny"
    subject_kind: str = ""
    resource_kind: str = ""
    action_name: str = ""
    winning_rule_id: str | None = None
    matched_rule_count: int = 0


class DispatchMode(StrEnum):
    SEQUENTIAL = "sequential"
    CONCURRENT = "concurrent"


class HandlerStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class HandlerResult:
    subscription_id: str
    handler_name: str
    status: HandlerStatus
    duration: timedelta
    error_code: str | None = None
    safe_error_message: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationResult:
    event: BaseEvent
    matched_handler_count: int
    successful_handler_count: int
    failed_handler_count: int
    skipped_handler_count: int
    handler_results: tuple[HandlerResult, ...]
    duration: timedelta


@dataclass(slots=True)
class Subscription(Generic[E]):
    subscription_id: str
    event_type: type[E]
    handler: Handler[E]
    predicate: Callable[[E], bool] | None
    handler_name: str
    timeout: float | None
    _bus: AsyncEventBus
    active: bool = True

    async def unsubscribe(self) -> None:
        await self._bus._unsubscribe(self.subscription_id)


class AsyncEventBus:
    def __init__(
        self,
        *,
        queue_capacity: int = 100,
        worker_count: int = 1,
        default_dispatch_mode: DispatchMode = DispatchMode.SEQUENTIAL,
        default_handler_timeout: float | None = None,
        publish_timeout: float | None = None,
        history_capacity: int = 100,
        fail_fast: bool = False,
    ) -> None:
        if queue_capacity < 1 or worker_count < 1 or history_capacity < 0:
            raise EventBusError("Queue, worker, and history capacities must be valid")
        self._queue: asyncio.PriorityQueue[
            tuple[int, int, BaseEvent, asyncio.Future[PublicationResult]]
        ] = asyncio.PriorityQueue(queue_capacity)
        self._subscriptions: dict[str, Subscription[Any]] = {}
        self._middleware: list[Middleware[Any]] = []
        self._history: deque[PublicationResult] = deque(maxlen=history_capacity)
        self._workers: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._worker_count = worker_count
        self.default_dispatch_mode = default_dispatch_mode
        self.default_handler_timeout = default_handler_timeout
        self.publish_timeout = publish_timeout
        self.fail_fast = fail_fast
        self._started = False
        self._stopped = False

    async def __aenter__(self) -> AsyncEventBus:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if self._stopped:
                raise EventBusError("Event bus cannot be restarted after stop")
            self._started = True
            self._workers = [asyncio.create_task(self._worker()) for _ in range(self._worker_count)]

    async def stop(self, *, force: bool = False) -> None:
        async with self._lock:
            if self._stopped:
                return
            self._stopped = True
        if not force:
            await self._queue.join()
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def subscribe(
        self,
        event_type: type[E],
        handler: Handler[E],
        *,
        predicate: Callable[[E], bool] | None = None,
        handler_name: str | None = None,
        timeout: float | None = None,
    ) -> Subscription[E]:
        if not issubclass(event_type, BaseEvent):
            raise EventBusError("Subscriptions require BaseEvent types")
        async with self._lock:
            self._sequence += 1
            identifier = f"subscription-{self._sequence}"
            resolved_name = handler_name or getattr(handler, "__name__", identifier)
            item = Subscription(
                identifier,
                event_type,
                handler,
                predicate,
                str(resolved_name),
                timeout,
                self,
            )
            self._subscriptions[identifier] = item
            return item

    async def _unsubscribe(self, identifier: str) -> None:
        async with self._lock:
            subscription = self._subscriptions.pop(identifier, None)
            if subscription:
                subscription.active = False

    def add_middleware(self, middleware: Middleware[Any]) -> None:
        self._middleware.append(middleware)

    async def publish(self, event: BaseEvent) -> PublicationResult:
        if not self._started or self._stopped:
            raise EventBusError("Event bus is not running")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PublicationResult] = loop.create_future()
        async with self._lock:
            self._sequence += 1
            item = (-list(EventPriority).index(event.priority), self._sequence, event, future)
        try:
            await asyncio.wait_for(self._queue.put(item), timeout=self.publish_timeout)
        except TimeoutError as error:
            raise EventBusError("Event publication timed out") from error
        return await future

    async def _worker(self) -> None:
        while True:
            _, _, event, future = await self._queue.get()
            try:
                result = await self.dispatch(event)
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not future.done():
                    future.set_exception(EventBusError("Event dispatch failed", cause=error))
            finally:
                self._queue.task_done()

    async def dispatch(
        self, event: BaseEvent, *, mode: DispatchMode | None = None
    ) -> PublicationResult:
        start = perf_counter()
        transformed = event
        for middleware in tuple(self._middleware):
            value = middleware(transformed)
            transformed = await value if inspect.isawaitable(value) else value
            if not isinstance(transformed, type(event)):
                raise EventBusError("Middleware returned incompatible event")
        async with self._lock:
            subscriptions = tuple(self._subscriptions.values())
        eligible = tuple(
            s
            for s in subscriptions
            if s.active
            and isinstance(transformed, s.event_type)
            and (s.predicate is None or s.predicate(transformed))
        )
        chosen = mode or self.default_dispatch_mode
        if chosen is DispatchMode.CONCURRENT:
            results = await self._concurrent(transformed, eligible)
        else:
            results = await self._sequential(transformed, eligible)
        result = PublicationResult(
            transformed,
            len(eligible),
            sum(r.status is HandlerStatus.SUCCESS for r in results),
            sum(r.status is HandlerStatus.FAILED for r in results),
            sum(r.status is HandlerStatus.SKIPPED for r in results),
            tuple(results),
            timedelta(seconds=perf_counter() - start),
        )
        async with self._lock:
            self._history.append(result)
        return result

    async def _call(self, event: BaseEvent, subscription: Subscription[Any]) -> HandlerResult:
        start = perf_counter()
        try:
            output = subscription.handler(event)
            if inspect.isawaitable(output):
                if subscription.timeout or self.default_handler_timeout:
                    await asyncio.wait_for(
                        output, subscription.timeout or self.default_handler_timeout
                    )
                else:
                    await output
            return HandlerResult(
                subscription.subscription_id,
                subscription.handler_name,
                HandlerStatus.SUCCESS,
                timedelta(seconds=perf_counter() - start),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return HandlerResult(
                subscription.subscription_id,
                subscription.handler_name,
                HandlerStatus.FAILED,
                timedelta(seconds=perf_counter() - start),
                getattr(error, "code", "handler_error"),
                str(error).split("\n", 1)[0],
            )

    async def _sequential(
        self, event: BaseEvent, subscriptions: tuple[Subscription[Any], ...]
    ) -> list[HandlerResult]:
        output = []
        for subscription in subscriptions:
            result = await self._call(event, subscription)
            output.append(result)
            if result.status is HandlerStatus.FAILED and self.fail_fast:
                break
        return output

    async def _concurrent(
        self, event: BaseEvent, subscriptions: tuple[Subscription[Any], ...]
    ) -> list[HandlerResult]:
        tasks = [asyncio.create_task(self._call(event, s)) for s in subscriptions]
        try:
            return list(await asyncio.gather(*tasks))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def history(
        self,
        *,
        event_type: type[BaseEvent] | None = None,
        correlation_id: CorrelationId | None = None,
        source: str | None = None,
    ) -> tuple[PublicationResult, ...]:
        async with self._lock:
            return tuple(
                r
                for r in self._history
                if (event_type is None or isinstance(r.event, event_type))
                and (correlation_id is None or r.event.correlation_id == correlation_id)
                and (source is None or r.event.source == source)
            )

    async def clear_history(self) -> None:
        async with self._lock:
            self._history.clear()


@dataclass(frozen=True, slots=True)
class Event:
    """Deprecated compatibility event for the pre-async runtime seam."""

    name: str
    occurred_at: datetime = field(default_factory=utc_now)


class EventBus:
    """Deprecated compatibility adapter; new code must use ``AsyncEventBus``."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Event], None]]] = {}

    def subscribe(self, name: str, handler: Callable[[Event], None]) -> None:
        self._handlers.setdefault(name, []).append(handler)

    def publish(self, name: str) -> Event:
        event = Event(name)
        for handler in tuple(self._handlers.get(name, ())):
            handler(event)
        return event
