import pytest
from alios_observability import InMemoryMetricRegistry, MetricPoint


@pytest.mark.asyncio
async def test_counter_registration_and_collection() -> None:
    registry = InMemoryMetricRegistry()
    counter = await registry.counter(
        "requests_total", description="Requests", label_names=("method",)
    )
    await counter.add(labels={"method": "GET"})
    points = await registry.collect()
    assert len(points) == 1
    assert isinstance(points[0], MetricPoint)
    assert points[0].value == 1


@pytest.mark.asyncio
async def test_gauge_registration_and_collection() -> None:
    registry = InMemoryMetricRegistry()
    gauge = await registry.gauge("runtime.active_runs", description="Runs")
    await gauge.set(-1)
    point = (await registry.collect())[0]
    assert isinstance(point, MetricPoint)
    assert point.value == -1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", range(28))
async def test_registry_keeps_labeled_counter_series_isolated(case: int) -> None:
    registry = InMemoryMetricRegistry()
    counter = await registry.counter(
        f"requests_{case}", description="Requests", label_names=("method",)
    )
    point = await counter.add(case, labels={"method": f"method-{case}"})
    assert point.value == case
    assert (await registry.collect())[0].labels.to_dict() == {"method": f"method-{case}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", range(25))
async def test_histogram_registration_observation_and_collection(case: int) -> None:
    registry = InMemoryMetricRegistry()
    histogram = await registry.histogram(
        f"request_duration_{case}", description="Duration", boundaries=(1, 5, 10)
    )
    point = await histogram.observe(case)
    assert point.count == 1
    assert point.bucket_counts[-1] == 1
    assert len(await registry.collect()) == 1
