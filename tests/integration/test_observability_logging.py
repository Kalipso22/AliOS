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
