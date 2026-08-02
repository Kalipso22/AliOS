import pytest
from alios_core.errors import ValidationError
from alios_core.ids import CorrelationId, RunId
from alios_observability import (
    InMemoryLogSink,
    LogContext,
    LogDropPolicy,
    LogLevel,
    LogRecord,
    LogSource,
    RedactionAction,
    RedactionPolicy,
    RedactionRule,
    StructuredLogger,
    bind_log_context,
    current_log_context,
)


@pytest.mark.parametrize("level", list(LogLevel))
def test_log_level_values_and_order(level: LogLevel) -> None:
    assert LogLevel.parse(level.value) is level
    assert LogLevel.TRACE.severity <= level.severity <= LogLevel.CRITICAL.severity


@pytest.mark.parametrize("value", ["", "verbose", "INFOO", "unknown"])
def test_invalid_log_level(value: str) -> None:
    with pytest.raises(ValidationError):
        LogLevel.parse(value)


@pytest.mark.parametrize("component", ["runtime", " api ", "worker", "agent", "scheduler"])
def test_log_source_normalization(component: str) -> None:
    source = LogSource(component, " module ", " operation ")
    assert source.component == component.strip()
    assert source.module == "module"
    assert source.operation == "operation"


@pytest.mark.parametrize("key", ["password", "api_key", "authorization", "cookie", "client_secret"])
def test_default_policy_redacts_sensitive_keys(key: str) -> None:
    rendered = RedactionPolicy().redact({key: "secret-value"})
    assert isinstance(rendered, dict)
    assert rendered[key] == "[REDACTED]"


@pytest.mark.parametrize("value", [0, 1, "text", True, None, ["one", "two"], {"nested": "value"}])
def test_context_round_trips_safe_values(value: object) -> None:
    context = LogContext(attributes={"value": value})
    restored = LogContext.from_dict(context.to_dict())
    assert restored.to_dict() == context.to_dict()


@pytest.mark.parametrize("action", list(RedactionAction))
def test_redaction_actions_are_deterministic(action: RedactionAction) -> None:
    policy = RedactionPolicy(
        rules=(RedactionRule("custom", action=action, keys=frozenset({"value"})),),
        include_default_rules=False,
    )
    first = policy.redact({"value": {"a": 1}})
    second = policy.redact({"value": {"a": 1}})
    assert first == second


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(150))
async def test_logger_emits_isolated_immutable_records(index: int) -> None:
    sink = InMemoryLogSink()
    logger = StructuredLogger(component="unit", sink=sink, minimum_level=LogLevel.TRACE)
    attributes = {"index": index, "password": f"secret-{index}"}
    record = await logger.info("message", attributes=attributes)
    assert record is not None
    attributes["index"] = -1
    rendered = record.to_dict()
    assert rendered["attributes"] == {"index": index, "password": "[REDACTED]"}
    assert len(await sink.list()) == 1


@pytest.mark.asyncio
async def test_context_binding_restores_after_cancellation() -> None:
    context = LogContext(correlation_id=CorrelationId(), run_id=RunId())
    async with bind_log_context(context):
        assert current_log_context() == context
    assert current_log_context() is None


@pytest.mark.asyncio
async def test_sink_capacity_policies() -> None:
    sink = InMemoryLogSink(capacity=1, drop_policy=LogDropPolicy.DROP_OLDEST)
    source = LogSource("unit")
    await sink.emit(LogRecord(LogLevel.INFO, "one", source))
    await sink.emit(LogRecord(LogLevel.INFO, "two", source))
    assert [item.message for item in await sink.list()] == ["two"]
