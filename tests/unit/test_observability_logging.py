from datetime import UTC, datetime, timedelta

import pytest
from alios_core.errors import (
    LogSerializationError,
    LogSinkError,
    ResourceConflictError,
    ResourceNotFoundError,
    ValidationError,
)
from alios_core.ids import CorrelationId, LogRecordId, RunId, TenantId, UserId
from alios_observability import (
    InMemoryLogSink,
    LogContext,
    LogDropPolicy,
    LogFilter,
    LogLevel,
    LogRecord,
    LogSinkSnapshot,
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


def _record(
    message: str,
    *,
    level: LogLevel = LogLevel.INFO,
    timestamp: datetime | None = None,
    sequence: int | None = None,
    source: LogSource | None = None,
    context: LogContext | None = None,
    attributes: dict[str, object] | None = None,
) -> LogRecord:
    return LogRecord(
        level,
        message,
        source or LogSource("unit", "module", "operation"),
        context or LogContext(),
        attributes or {},
        None,
        sequence,
        LogRecordId(),
        timestamp or datetime.now(UTC),
    )


def test_log_filter_serializes_all_fields_independently() -> None:
    now = datetime.now(UTC)
    record_id = LogRecordId()
    filter = LogFilter(
        LogLevel.DEBUG,
        LogLevel.ERROR,
        frozenset({LogLevel.INFO}),
        frozenset({" unit "}),
        frozenset({" module "}),
        frozenset({" operation "}),
        CorrelationId(),
        RunId(),
        TenantId(),
        UserId(),
        frozenset({record_id}),
        now,
        now + timedelta(seconds=1),
        " value ",
        {"key": {"nested": True}},
        3,
        2,
    )
    restored = LogFilter.from_dict(filter.to_dict())
    assert restored == filter
    assert restored.attribute_equals is not filter.attribute_equals


@pytest.mark.parametrize(
    "factory",
    [
        lambda: LogFilter(limit=-1),
        lambda: LogFilter(offset=-1),
        lambda: LogFilter(LogLevel.ERROR, LogLevel.INFO),
        lambda: LogFilter(message_contains="   "),
        lambda: LogFilter(components=frozenset({"   "})),
        lambda: LogFilter(levels=frozenset({"info"})),  # type: ignore[arg-type]
        lambda: LogFilter(record_ids=frozenset({CorrelationId()})),  # type: ignore[arg-type]
        lambda: LogFilter(created_after=datetime.now()),
        lambda: LogFilter(created_before=datetime.now()),
        lambda: LogFilter(
            created_after=datetime(2026, 1, 2, tzinfo=UTC),
            created_before=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ],
)
def test_log_filter_rejects_invalid_input(factory: object) -> None:
    with pytest.raises(ValidationError):
        factory()  # type: ignore[operator]


def test_log_filter_matches_every_supported_field_and_attribute_overlay() -> None:
    now = datetime.now(UTC)
    correlation, run, tenant, user = CorrelationId(), RunId(), TenantId(), UserId()
    record = _record(
        "The exact phrase is present",
        level=LogLevel.WARNING,
        timestamp=now,
        source=LogSource("api", "requests", "create"),
        context=LogContext(correlation, run, tenant, user, attributes={"shared": "context"}),
        attributes={"shared": "record", "kind": "match"},
    )
    assert LogFilter(
        LogLevel.INFO,
        LogLevel.ERROR,
        frozenset({LogLevel.WARNING}),
        frozenset({"api"}),
        frozenset({"requests"}),
        frozenset({"create"}),
        correlation,
        run,
        tenant,
        user,
        frozenset({record.record_id}),
        now - timedelta(seconds=1),
        now + timedelta(seconds=1),
        "exact phrase",
        {"shared": "record", "kind": "match"},
    ).matches(record)
    assert not LogFilter(message_contains="Exact phrase").matches(record)
    assert not LogFilter(created_after=now).matches(record)
    assert not LogFilter(created_before=now).matches(record)


@pytest.mark.asyncio
async def test_sink_list_sorts_then_paginates_and_count_ignores_pagination() -> None:
    now = datetime.now(UTC)
    sink = InMemoryLogSink()
    records = (
        _record("none", timestamp=now, sequence=None),
        _record("second", timestamp=now, sequence=2),
        _record("first", timestamp=now, sequence=1),
    )
    await sink.emit_many(records)
    values = await sink.list(LogFilter(limit=1, offset=1))
    assert [item.message for item in values] == ["second"]
    assert await sink.count(LogFilter(limit=1, offset=1)) == 3


@pytest.mark.asyncio
async def test_sink_batch_duplicate_and_capacity_errors_are_atomic() -> None:
    sink = InMemoryLogSink(capacity=1, drop_policy=LogDropPolicy.RAISE)
    existing = _record("existing")
    await sink.emit(existing)
    with pytest.raises(ResourceConflictError):
        await sink.emit_many((_record("new"), existing))
    with pytest.raises(LogSinkError):
        await sink.emit_many((_record("one"), _record("two")))
    assert await sink.list() == (existing,)
    assert (await sink.snapshot()).failed_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "expected", "accepted", "dropped"),
    [
        (LogDropPolicy.DROP_OLDEST, ["two", "three"], 3, 1),
        (LogDropPolicy.DROP_NEWEST, ["one", "two"], 2, 1),
    ],
)
async def test_sink_batch_drop_policies(
    policy: LogDropPolicy, expected: list[str], accepted: int, dropped: int
) -> None:
    sink = InMemoryLogSink(capacity=2, drop_policy=policy)
    await sink.emit_many((_record("one"), _record("two"), _record("three")))
    assert [record.message for record in await sink.list()] == expected
    snapshot = await sink.snapshot()
    assert (snapshot.accepted_count, snapshot.dropped_count) == (accepted, dropped)


@pytest.mark.asyncio
async def test_sink_read_operations_remain_available_after_close_and_clear_preserves_counters() -> (
    None
):
    sink = InMemoryLogSink()
    record = _record("stored")
    await sink.emit(record)
    await sink.close()
    await sink.flush()
    assert await sink.get(record.record_id) == record
    assert await sink.clear() == 1
    assert (await sink.snapshot()).accepted_count == 1
    with pytest.raises(LogSinkError):
        await sink.emit(_record("rejected"))
    with pytest.raises(ResourceNotFoundError):
        await sink.get(LogRecordId())


@pytest.mark.parametrize(
    "case",
    range(31),
)
def test_log_filter_defensively_freezes_collections(case: int) -> None:
    components = {f"component-{case}"}
    attributes = {"case": case}
    filter = LogFilter(components=frozenset(components), attribute_equals=attributes)
    components.add("later")
    attributes["case"] = -1
    assert filter.components == frozenset({f"component-{case}"})
    assert filter.attribute_equals == {"case": case}


@pytest.mark.parametrize("case", range(10))
def test_snapshot_round_trip_and_validation(case: int) -> None:
    snapshot = LogSinkSnapshot(case, 10, case, 0, 0, False)
    assert LogSinkSnapshot.from_dict(snapshot.to_dict()) == snapshot
    with pytest.raises(ValidationError):
        LogSinkSnapshot(11, 10, 0, 0, 0, False)
    with pytest.raises(LogSerializationError):
        LogSinkSnapshot.from_dict({"stored_count": 0})
