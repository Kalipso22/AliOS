"""Asynchronous lifecycle-managed runtime orchestration."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Generic, Protocol, TypeVar

from alios_core.errors import (
    PermissionDeniedError,
    ResourceConflictError,
    ValidationError,
)
from alios_core.events import AsyncEventBus
from alios_core.ids import RunId
from alios_core.lifecycle import LifecycleHealth, LifecycleState, ManagedLifecycleComponent
from alios_core.policy import (
    InMemoryPolicyEvaluator,
    PolicyAction,
    PolicyContext,
    PolicyEvaluator,
    PolicyResource,
    PolicySubject,
)
from alios_core.types import HealthStatus, RunOutcome, RunStatus, utc_now

from .execution_context import CancellationToken, ExecutionContext, bind_execution_context
from .recovery import (
    CheckpointKind,
    CheckpointService,
    InMemoryCheckpointRepository,
    RecoveryCoordinator,
    RecoveryMode,
    RecoveryPlan,
    RecoveryResult,
)
from .run_manager import RunFailure, RunManager, RunRecord

T = TypeVar("T", covariant=True)


class RuntimeTask(Protocol[T]):
    async def __call__(self, context: ExecutionContext) -> T: ...


@dataclass(frozen=True, slots=True)
class RuntimePolicyRequest:
    subject: PolicySubject
    resource: PolicyResource
    action: PolicyAction
    context: PolicyContext = field(default_factory=PolicyContext)


@dataclass(slots=True)
class _ActiveExecution:
    run_id: object
    task: asyncio.Task[object]
    context: ExecutionContext
    cancellation_token: CancellationToken
    registered_at: datetime


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
    correlation_id: object
    started_at: datetime
    completed_at: datetime
    duration: timedelta
    checkpoint_ids: tuple[object, ...] = ()
    recovery_result: object | None = None


class Runtime(ManagedLifecycleComponent):
    def __init__(
        self,
        event_bus: AsyncEventBus | None = None,
        run_manager: RunManager | None = None,
        checkpoint_service: CheckpointService | None = None,
        policy_evaluator: PolicyEvaluator | None = None,
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
        self.policy_evaluator = policy_evaluator or InMemoryPolicyEvaluator()
        self._clock = clock
        self._active: dict[object, _ActiveExecution] = {}
        self._active_lock = asyncio.Lock()
        self._accepting = False

    @property
    def is_running(self) -> bool:
        return self._accepting

    async def active_count(self) -> int:
        async with self._active_lock:
            return len(self._active)

    async def _health(self) -> LifecycleHealth:
        active = await self.active_count()
        healthy = self.state is LifecycleState.RUNNING and self._accepting
        status = HealthStatus.HEALTHY if healthy else HealthStatus.DEGRADED
        if self.state is LifecycleState.FAILED:
            status = HealthStatus.UNHEALTHY
        return LifecycleHealth(
            "runtime",
            self.state,
            status,
            self._clock(),
            details={
                "accepting_executions": self._accepting,
                "active_execution_count": active,
                "event_bus_available": not self.events._stopped,
                "checkpoint_repository_available": True,
                "run_repository_available": True,
                "policy_evaluator_available": self.policy_evaluator is not None,
            },
        )

    async def _start(self) -> None:
        if self._owns_event_bus:
            await self.events.start()
        self._accepting = True

    async def _stop(self) -> None:
        self._accepting = False
        async with self._active_lock:
            tasks = tuple(item.task for item in self._active.values())
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
        policy_request: RuntimePolicyRequest | None = None,
        _existing_run: RunRecord | None = None,
    ) -> RuntimeExecutionResult[T]:
        if not self._accepting:
            raise ValidationError("Runtime is not running")
        context = context or ExecutionContext.create(metadata=metadata or {})
        run = _existing_run or await self.run_manager.create_run(
            run_id=context.run_id,
            correlation_id=context.correlation_id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            idempotency_key=idempotency_key,
            execution_mode=context.execution_mode,
            deadline=context.deadline,
            metadata=context.metadata,
        )
        if policy_request is not None:
            try:
                decision = await self.policy_evaluator.evaluate(
                    policy_request.subject,
                    policy_request.resource,
                    policy_request.action,
                    policy_request.context,
                    correlation_id=context.correlation_id,
                )
            except Exception as error:
                failed = await self.run_manager.fail_run(run.run_id, error)
                return RuntimeExecutionResult(
                    failed,
                    RunOutcome.FAILED,
                    None,
                    failed.failure,
                    context.correlation_id,
                    self._clock(),
                    self._clock(),
                    timedelta(),
                )
            if decision.decision.value != "allow":
                denial = PermissionDeniedError("Runtime policy denied execution")
                failed = await self.run_manager.fail_run(run.run_id, denial)
                return RuntimeExecutionResult(
                    failed,
                    RunOutcome.FAILED,
                    None,
                    failed.failure,
                    context.correlation_id,
                    self._clock(),
                    self._clock(),
                    timedelta(),
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
            if run.run_id in self._active:
                raise ResourceConflictError("Run already has an active execution")
            self._active[run.run_id] = _ActiveExecution(
                run.run_id, worker, context, context.cancellation_token, started
            )
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
            context.cancellation_token.cancel("timeout")
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            finished = await self.run_manager.mark_timed_out(run.run_id)
            if self.options.checkpoint_on_timeout:
                ids.append(
                    (
                        await self.checkpoints.create_checkpoint(
                            run.run_id,
                            state=checkpoint_state or {},
                            kind=CheckpointKind.RUN_TIMED_OUT,
                        )
                    ).checkpoint_id
                )
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
        except asyncio.CancelledError:
            finished = await self.run_manager.cancel_run(run.run_id)
            if self.options.checkpoint_on_cancellation:
                ids.append(
                    (
                        await self.checkpoints.create_checkpoint(
                            run.run_id,
                            state=checkpoint_state or {},
                            kind=CheckpointKind.RUN_CANCELLED,
                        )
                    ).checkpoint_id
                )
            return RuntimeExecutionResult(
                finished,
                RunOutcome.CANCELLED,
                None,
                RunFailure("cancelled", "Execution cancelled"),
                context.correlation_id,
                started,
                self._clock(),
                self._clock() - started,
                tuple(ids),
            )
        except Exception as error:
            finished = await self.run_manager.fail_run(run.run_id, error)
            if self.options.checkpoint_on_failure:
                ids.append(
                    (
                        await self.checkpoints.create_checkpoint(
                            run.run_id, state=checkpoint_state or {}, kind=CheckpointKind.RUN_FAILED
                        )
                    ).checkpoint_id
                )
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

    async def cancel_run(self, run_id: RunId, reason: str = "user_requested") -> RunRecord:
        run = await self.run_manager.get_run(run_id)
        if run.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }:
            if run.status is RunStatus.CANCELLED:
                return run
            raise ValidationError("Terminal run cannot be cancelled")
        async with self._active_lock:
            active = self._active.get(run_id)
        if active is not None:
            active.cancellation_token.cancel(reason)
        if run.status is not RunStatus.CANCELLING:
            await self.run_manager.request_cancellation(run_id)
        if active is not None:
            active.task.cancel()
            await asyncio.gather(active.task, return_exceptions=True)
            while True:
                async with self._active_lock:
                    still_active = run_id in self._active
                if not still_active:
                    break
                await asyncio.sleep(0)
        current = await self.run_manager.get_run(run_id)
        finished = (
            current
            if current.status is RunStatus.CANCELLED
            else await self.run_manager.cancel_run(run_id)
        )
        if self.options.checkpoint_on_cancellation:
            await self.checkpoints.create_checkpoint(
                run_id, state={}, kind=CheckpointKind.RUN_CANCELLED, metadata={"reason": reason}
            )
        return finished

    async def execute_existing(
        self,
        run_id: RunId,
        task: Callable[[ExecutionContext], T | Awaitable[T]],
        *,
        context: ExecutionContext | None = None,
        timeout: timedelta | None = None,
        checkpoint_state: Mapping[str, Any] | None = None,
        policy_request: RuntimePolicyRequest | None = None,
    ) -> RuntimeExecutionResult[T]:
        run = await self.run_manager.get_run(run_id)
        if run.status not in {RunStatus.CREATED, RunStatus.QUEUED}:
            raise ResourceConflictError("Existing run is not executable")
        current = context or ExecutionContext.create(
            run_id=run.run_id,
            correlation_id=run.correlation_id,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            execution_mode=run.execution_mode,
            deadline=run.deadline,
            metadata=run.metadata,
        )
        if current.run_id != run.run_id or current.correlation_id != run.correlation_id:
            raise ValidationError("Execution context does not match existing run")
        if current.tenant_id != run.tenant_id or current.user_id != run.user_id:
            raise ValidationError("Execution context scope does not match existing run")
        return await self.execute(
            task,
            context=current,
            timeout=timeout,
            checkpoint_state=checkpoint_state,
            policy_request=policy_request,
            _existing_run=run,
        )

    async def create_recovery_plan(
        self, run_id: RunId, *, mode: RecoveryMode = RecoveryMode.RESUME
    ) -> RecoveryPlan:
        return await self.recovery.create_plan(run_id, mode=mode)

    async def recover(
        self, plan: RecoveryPlan, restore: Callable[[object, RecoveryPlan], object]
    ) -> RecoveryResult:
        return await self.recovery.recover(plan, restore)
