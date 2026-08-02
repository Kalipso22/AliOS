import pytest
from alios_observability import InMemoryMetricRegistry


@pytest.mark.asyncio
async def test_counter_registration_and_collection() -> None:
    registry = InMemoryMetricRegistry()
    counter = await registry.counter(
        "requests_total", description="Requests", label_names=("method",)
    )
    await counter.add(labels={"method": "GET"})
    points = await registry.collect()
    assert len(points) == 1
    assert points[0].value == 1


@pytest.mark.asyncio
async def test_gauge_registration_and_collection() -> None:
    registry = InMemoryMetricRegistry()
    gauge = await registry.gauge("runtime.active_runs", description="Runs")
    await gauge.set(-1)
    assert (await registry.collect())[0].value == -1


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
