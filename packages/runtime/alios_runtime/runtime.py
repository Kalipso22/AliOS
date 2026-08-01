"""Asynchronous lifecycle-managed runtime orchestration."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generic, Protocol, TypeVar

from alios_core.errors import ValidationError
from alios_core.events import AsyncEventBus
from alios_core.lifecycle import ManagedLifecycleComponent
from alios_core.types import RunOutcome, utc_now

from .execution_context import ExecutionContext, bind_execution_context
from .recovery import (
    CheckpointKind,
    CheckpointService,
    InMemoryCheckpointRepository,
    RecoveryCoordinator,
)
from .run_manager import RunFailure, RunManager, RunRecord

T = TypeVar("T", covariant=True)


class RuntimeTask(Protocol[T]):
    async def __call__(self, context: ExecutionContext) -> T: ...


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    default_timeout: timedelta | None = None
    shutdown_timeout: timedelta = timedelta(seconds=30)
    cancellation_grace_period: timedelta = timedelta(seconds=2)
    checkpoint_on_start: bool = True
    checkpoint_on_success: bool = True
    checkpoint_on_failure: bool = True
    checkpoint_on_cancellation: bool = True
    checkpoint_on_timeout: bool = True
    drain_on_shutdown: bool = True
    cancel_on_shutdown_timeout: bool = True

    def __post_init__(self) -> None:
        if any(
            value is not None and value < timedelta()
            for value in (
                self.default_timeout,
                self.shutdown_timeout,
                self.cancellation_grace_period,
            )
        ):
            raise ValidationError("Runtime durations cannot be negative")


@dataclass(frozen=True, slots=True)
class RuntimeExecutionResult(Generic[T]):
    run: RunRecord
    outcome: RunOutcome
    value: T | None
    failure: RunFailure | None
    correlation_id: Any
    started_at: datetime
    completed_at: datetime
    duration: timedelta
    checkpoint_ids: tuple[Any, ...] = ()
    recovery_result: Any = None


class Runtime(ManagedLifecycleComponent):
    def __init__(
        self,
        event_bus: AsyncEventBus | None = None,
        run_manager: RunManager | None = None,
        checkpoint_service: CheckpointService | None = None,
        options: RuntimeOptions | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__("runtime")
        self.events = event_bus or AsyncEventBus()
        self._owns_event_bus = event_bus is None
        self.run_manager = run_manager or RunManager(event_publisher=self.events.publish)
        self.checkpoints = checkpoint_service or CheckpointService(
            InMemoryCheckpointRepository(), self.run_manager, self.events.publish
        )
        self.recovery = RecoveryCoordinator(
            self.checkpoints.repository, self.run_manager, self.events.publish, clock
        )
        self.options = options or RuntimeOptions()
        self._clock = clock
        self._active: dict[Any, asyncio.Task[Any]] = {}
        self._active_lock = asyncio.Lock()
        self._accepting = False

    @property
    def is_running(self) -> bool:
        return self._accepting

    async def _start(self) -> None:
        if self._owns_event_bus:
            await self.events.start()
        self._accepting = True

    async def _stop(self) -> None:
        self._accepting = False
        async with self._active_lock:
            tasks = tuple(self._active.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_event_bus:
            await self.events.stop()

    async def execute(
        self,
        task: Callable[[ExecutionContext], T | Awaitable[T]],
        *,
        context: ExecutionContext | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        timeout: timedelta | None = None,
        checkpoint_state: Mapping[str, Any] | None = None,
    ) -> RuntimeExecutionResult[T]:
        if not self._accepting:
            raise ValidationError("Runtime is not running")
        context = context or ExecutionContext.create(metadata=metadata or {})
        run = await self.run_manager.create_run(
            run_id=context.run_id,
            correlation_id=context.correlation_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            idempotency_key=idempotency_key,
            execution_mode=context.execution_mode,
            deadline=context.deadline,
            metadata=context.metadata,
        )
        await self.run_manager.start_run(run.run_id)
        started = self._clock()
        ids = []
        if self.options.checkpoint_on_start:
            ids.append(
                (
                    await self.checkpoints.create_checkpoint(
                        run.run_id, state=checkpoint_state or {}, kind=CheckpointKind.RUN_STARTED
                    )
                ).checkpoint_id
            )

        async def invoke() -> T:
            async with bind_execution_context(context):
                value = task(context)
                return await value if inspect.isawaitable(value) else value

        worker = asyncio.create_task(invoke())
        async with self._active_lock:
            self._active[run.run_id] = worker
        try:
            effective = context.effective_timeout(timeout or self.options.default_timeout)
            value = (
                await asyncio.wait_for(worker, effective.total_seconds())
                if effective
                else await worker
            )
            finished = await self.run_manager.complete_run(run.run_id)
            if self.options.checkpoint_on_success:
                ids.append(
                    (
                        await self.checkpoints.create_checkpoint(
                            run.run_id,
                            state=checkpoint_state or {},
                            kind=CheckpointKind.RUN_SUCCEEDED,
                        )
                    ).checkpoint_id
                )
            return RuntimeExecutionResult(
                finished,
                RunOutcome.SUCCEEDED,
                value,
                None,
                context.correlation_id,
                started,
                self._clock(),
                self._clock() - started,
                tuple(ids),
            )
        except TimeoutError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            finished = await self.run_manager.mark_timed_out(run.run_id)
            return RuntimeExecutionResult(
                finished,
                RunOutcome.TIMED_OUT,
                None,
                RunFailure("timeout", "Execution timed out"),
                context.correlation_id,
                started,
                self._clock(),
                self._clock() - started,
                tuple(ids),
            )
        except Exception as error:
            finished = await self.run_manager.fail_run(run.run_id, error)
            return RuntimeExecutionResult(
                finished,
                RunOutcome.FAILED,
                None,
                finished.failure,
                context.correlation_id,
                started,
                self._clock(),
                self._clock() - started,
                tuple(ids),
            )
        finally:
            async with self._active_lock:
                self._active.pop(run.run_id, None)
