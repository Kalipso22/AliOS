import asyncio
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from alios_core.errors import (
    LogSerializationError,
    LogSinkError,
    ResourceConflictError,
    ResourceNotFoundError,
    ValidationError,
)
from alios_core.ids import CorrelationId, EventId, LogRecordId, RunId, TenantId, UserId
from alios_observability import (
    InMemoryLogSink,
    LogContext,
    LogDropPolicy,
    LogException,
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
    default_redaction_policy,
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


@pytest.mark.parametrize(
    "value", [math.nan, math.inf, -math.inf, b"secret", bytearray(b"secret"), {"value"}]
)
def test_logging_normalization_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(LogSerializationError):
        LogContext(attributes={"value": value})


def test_logging_normalization_rejects_non_string_mapping_key() -> None:
    with pytest.raises(LogSerializationError):
        LogContext(attributes=cast(dict[str, object], {1: "value"}))


@pytest.mark.parametrize(
    "keyword",
    [
        "password=secret-value",
        "api_key=secret-value",
        "client_secret=secret-value",
        "access_token=secret-value",
        "refresh-token: secret-value",
        "Authorization: Bearer secret-value",
        "Bearer secret-value",
        "cookie=session-secret",
        "session_id=secret-value",
        "private_key=secret-value",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_default_redaction_masks_secret_message(keyword: str) -> None:
    rendered = default_redaction_policy().redact(f"request {keyword}")
    assert "secret-value" not in str(rendered)
    assert "session-secret" not in str(rendered)


@pytest.mark.parametrize(
    "message",
    [
        "token_count=8",
        "password_policy=strong",
        "authorization_result=ok",
        "session_timeout=2",
        "credential_type=oauth",
    ],
)
def test_default_redaction_avoids_documented_false_positives(message: str) -> None:
    assert default_redaction_policy().redact(message) == message


def test_log_context_rejects_wrong_identifier_types() -> None:
    with pytest.raises(ValidationError):
        LogContext(run_id=CorrelationId())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LogContext(parent_log_record_id=EventId())  # type: ignore[arg-type]


def test_log_context_to_dict_deeply_thaws_attributes() -> None:
    context = LogContext(attributes={"nested": {"values": ("one", {"two": 2})}})
    first = context.to_dict()
    second = context.to_dict()
    assert first == second
    assert isinstance(first["attributes"], dict)
    assert json.dumps(first)
    first["attributes"] = {"nested": {"values": ["one", {"two": 3}]}}
    assert context.to_dict()["attributes"]["nested"]["values"][1]["two"] == 2  # type: ignore[index]


def test_log_context_from_dict_rejects_malformed_values() -> None:
    with pytest.raises(LogSerializationError):
        LogContext.from_dict({"run_id": 1})
    with pytest.raises(LogSerializationError):
        LogContext.from_dict({"attributes": []})


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("request.headers.authorization", ("request", "headers", "authorization"), True),
        ("request.*.authorization", ("request", "headers", "authorization"), True),
        ("request.headers.**", ("request", "headers"), True),
        ("request.headers.**", ("request", "headers", "authorization"), True),
        ("**", ("request", "headers", "authorization"), True),
        ("request.*.authorization", ("request", "authorization"), False),
    ],
)
def test_redaction_path_patterns(pattern: str, path: tuple[str, ...], matches: bool) -> None:
    rule = RedactionRule("path", path_patterns=(pattern,))
    assert rule.matches(None, path, "value") is matches


def test_redaction_rule_round_trip_and_compiled_internals_are_private() -> None:
    rule = RedactionRule("rule", value_patterns=("secret",))
    restored = RedactionRule.from_dict(rule.to_dict())
    assert restored == rule
    assert "compiled" not in str(rule.to_dict())
    assert rule._compiled_value_patterns is not restored._compiled_value_patterns


@pytest.mark.parametrize(
    "action, expected",
    [
        (RedactionAction.REMOVE, "[REMOVED]"),
        (RedactionAction.MASK, "mask"),
        (RedactionAction.HASH, "sha256:"),
    ],
)
def test_redaction_actions_for_root_scalars(action: RedactionAction, expected: str) -> None:
    value = RedactionPolicy(
        (RedactionRule("rule", action=action, value_patterns=("secret",), replacement="mask"),),
        include_default_rules=False,
    ).redact("secret")
    assert str(value).startswith(expected)


@pytest.mark.parametrize(
    "error", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(), GeneratorExit()]
)
def test_log_exception_process_control_errors_propagate(error: BaseException) -> None:
    with pytest.raises(type(error)):
        LogException.from_exception(error)


def test_log_exception_round_trip_and_redaction() -> None:
    exception = LogException("Example", "example", "password=secret", {"api_key": "secret"})
    rendered = exception.to_dict()
    assert "secret" not in str(rendered)
    assert (
        LogException.from_dict(exception.to_dict(RedactionPolicy(include_default_rules=False)))
        == exception
    )


@pytest.mark.parametrize(
    "builder",
    [
        lambda: LogRecord(cast(LogLevel, "info"), "message", LogSource("unit")),
        lambda: LogRecord(LogLevel.INFO, "message", cast(LogSource, "source")),
        lambda: LogRecord(LogLevel.INFO, "message", LogSource("unit"), cast(LogContext, "context")),
        lambda: LogRecord(LogLevel.INFO, "message", LogSource("unit"), sequence=True),
        lambda: LogRecord(
            LogLevel.INFO,
            "message",
            LogSource("unit"),
            record_id=cast(LogRecordId, CorrelationId()),
        ),
    ],
)
def test_log_record_runtime_validation(builder: object) -> None:
    with pytest.raises(ValidationError):
        cast(Callable[[], LogRecord], builder)()


def test_log_record_round_trip_preserves_exception() -> None:
    record = _record("message", attributes={"nested": [1, 2]})
    record = LogRecord(
        record.level,
        record.message,
        record.source,
        record.context,
        record.attributes,
        LogException("Error", "code", "safe"),
        record.sequence,
        record.record_id,
        record.timestamp,
    )
    assert (
        LogRecord.from_dict(record.to_dict(RedactionPolicy(include_default_rules=False))) == record
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "one"},
        {"api_key": "two"},
        {"access_token": "three"},
        {"refresh_token": "four"},
        {"cookie": "five"},
        {"private_key": "six"},
        {"client_secret": "seven"},
        {"authorization": "Bearer eight"},
        {"nested": {"password": "nine"}},
        {"items": [{"api_key": "ten"}]},
        {"items": ({"cookie": "eleven"},)},
        {"password": {"nested": "twelve"}},
        {"api-key": "thirteen"},
        {"refresh-token": "fourteen"},
        {"client-secret": "fifteen"},
        {"proxy_authorization": "sixteen"},
        {"set_cookie": "seventeen"},
        {"credential": "eighteen"},
        {"session_id": "nineteen"},
        {"secret": "twenty"},
        {"passwd": "twenty-one"},
        {"passphrase": "twenty-two"},
        {"id_token": "twenty-three"},
        {"credentials": "twenty-four"},
        {"private-key": "twenty-five"},
        {"access-token": "twenty-six"},
        {"session": "twenty-seven"},
        {"apikey": "twenty-eight"},
    ],
)
def test_default_redaction_masks_sensitive_structured_values(payload: dict[str, object]) -> None:
    rendered = default_redaction_policy().redact(payload)
    assert "[REDACTED]" in str(rendered)
