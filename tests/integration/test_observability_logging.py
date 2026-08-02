import pytest
from alios_core.ids import CorrelationId, RunId
from alios_observability import (
    InMemoryLogSink,
    LogContext,
    LogDropPolicy,
    LoggerFactory,
    LogLevel,
    StructuredLogger,
    bind_log_context,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(20))
async def test_logging_pipeline_preserves_context_and_order(index: int) -> None:
    sink = InMemoryLogSink(capacity=2, drop_policy=LogDropPolicy.DROP_OLDEST)
    correlation = CorrelationId()
    context = LogContext(correlation_id=correlation, run_id=RunId(), attributes={"index": index})
    logger = StructuredLogger(component="integration", sink=sink, minimum_level=LogLevel.TRACE)
    async with bind_log_context(context):
        first = await logger.info("first", attributes={"api_key": "secret"})
        second = await logger.warning("second")
    records = await sink.list()
    assert first is not None and second is not None
    assert [record.sequence for record in records] == [1, 2]
    assert all(record.context.correlation_id == correlation for record in records)
    assert "secret" not in str(records[0].to_dict())


@pytest.mark.asyncio
async def test_factory_external_sink_remains_open() -> None:
    sink = InMemoryLogSink()
    factory = LoggerFactory(sink)
    await factory.close()
    await sink.flush()


@pytest.mark.asyncio
async def test_factory_owned_sink_closes() -> None:
    factory = LoggerFactory()
    await factory.close()
    await factory.sink.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", range(10))
async def test_closed_sink_keeps_historical_records_available(case: int) -> None:
    sink = InMemoryLogSink(capacity=2)
    logger = StructuredLogger(component="integration", sink=sink, minimum_level=LogLevel.TRACE)
    record = await logger.info(f"record-{case}")
    assert record is not None
    await sink.close()
    assert await sink.get(record.record_id) == record
    assert await sink.count() == 1
    assert (await sink.snapshot()).closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location, secret",
    [
        ("message", "password=alpha"),
        ("message", "api_key=bravo"),
        ("message", "Authorization: Bearer charlie"),
        ("message", "access_token=delta"),
        ("message", "refresh_token=echo"),
        ("message", "cookie=foxtrot"),
        ("message", "private_key=golf"),
        ("message", "client_secret=hotel"),
        ("bound", "india"),
        ("base_context", "juliet"),
        ("base_attributes", "kilo"),
        ("call_context", "lima"),
        ("record_attributes", "mike"),
        ("nested", "november"),
        ("list", "oscar"),
        ("tuple", "papa"),
        ("details", "quebec"),
        ("message", "Bearer romeo"),
    ],
)
async def test_default_redaction_through_logger_never_leaks_secret(
    location: str, secret: str
) -> None:
    sink = InMemoryLogSink()
    bound = LogContext(attributes={"password": secret}) if location == "bound" else LogContext()
    logger = StructuredLogger(
        component="security",
        sink=sink,
        minimum_level=LogLevel.TRACE,
        base_context=LogContext(attributes={"api_key": secret})
        if location == "base_context"
        else None,
        base_attributes={"client_secret": secret} if location == "base_attributes" else None,
    )
    async with bind_log_context(bound):
        record = await logger.info(
            secret if location == "message" else "safe",
            context=LogContext(attributes={"access_token": secret})
            if location == "call_context"
            else None,
            attributes={"refresh_token": secret}
            if location == "record_attributes"
            else {"nested": {"cookie": secret}}
            if location == "nested"
            else {"items": [{"cookie": secret}]}
            if location == "list"
            else {"items": ({"cookie": secret},)}
            if location == "tuple"
            else None,
        )
    assert record is not None
    rendered = record.to_dict()
    assert secret not in str(rendered)
    assert secret not in repr(rendered)
