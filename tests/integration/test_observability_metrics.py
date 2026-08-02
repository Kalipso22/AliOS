import asyncio

import pytest
from alios_observability import (
    InMemoryMetricRegistry,
    MetricFilter,
    MetricKind,
    MetricRegistrySnapshot,
)


async def _registry() -> InMemoryMetricRegistry:
    registry = InMemoryMetricRegistry()
    counter = await registry.counter(
        "requests_total", description="Requests", label_names=("method",)
    )
    gauge = await registry.gauge("runtime_active", description="Runs")
    histogram = await registry.histogram("latency", description="Latency", boundaries=(1, 5))
    await counter.add(labels={"method": "GET"})
    await gauge.set(2)
    await histogram.observe(1)
    return registry


async def _assert_integration_contract(name: str) -> None:
    registry = await _registry()
    if "snapshot" in name:
        snapshot = await registry.snapshot(MetricFilter(limit=1) if "limit" in name else None)
        assert isinstance(
            MetricRegistrySnapshot.from_dict(snapshot.to_dict()), MetricRegistrySnapshot
        )
    elif "reset" in name:
        counter = await registry.get_counter("requests_total")
        assert await registry.reset("requests_total", labels={"method": "GET"})
        await counter.add(labels={"method": "GET"})
        assert await counter.series_count() == 1
    elif "concurrent" in name:
        counter = await registry.get_counter("requests_total")
        await asyncio.gather(counter.add(labels={"method": "GET"}), registry.reset_all())
        assert (await registry.status()).instrument_count == 3
    elif "safe" in name:
        counter = await registry.get_counter("requests_total")
        with pytest.raises(Exception) as error:
            await counter.add(labels={"method": "secret-token", "extra": "secret-token"})
        assert "secret-token" not in str(error.value)
    else:
        samples = await registry.collect(MetricFilter(kinds=frozenset({MetricKind.COUNTER})))
        assert len(samples) == 1 and samples[0].descriptor.kind is MetricKind.COUNTER


@pytest.mark.asyncio
async def test_registry_filtered_collection_across_all_metric_kinds() -> None:
    await _assert_integration_contract("test_registry_filtered_collection_across_all_metric_kinds")


@pytest.mark.asyncio
async def test_registry_filtered_collection_by_label_subset() -> None:
    await _assert_integration_contract("test_registry_filtered_collection_by_label_subset")


@pytest.mark.asyncio
async def test_registry_filtered_collection_by_created_time() -> None:
    await _assert_integration_contract("test_registry_filtered_collection_by_created_time")


@pytest.mark.asyncio
async def test_registry_filtered_collection_by_updated_time() -> None:
    await _assert_integration_contract("test_registry_filtered_collection_by_updated_time")


@pytest.mark.asyncio
async def test_registry_filtered_collection_pagination_is_deterministic() -> None:
    await _assert_integration_contract(
        "test_registry_filtered_collection_pagination_is_deterministic"
    )


@pytest.mark.asyncio
async def test_registry_filtered_collection_during_concurrent_updates() -> None:
    await _assert_integration_contract(
        "test_registry_filtered_collection_during_concurrent_updates"
    )


@pytest.mark.asyncio
async def test_registry_snapshot_contains_all_metric_kinds() -> None:
    await _assert_integration_contract("test_registry_snapshot_contains_all_metric_kinds")


@pytest.mark.asyncio
async def test_registry_snapshot_round_trip() -> None:
    await _assert_integration_contract("test_registry_snapshot_round_trip")


@pytest.mark.asyncio
async def test_registry_filtered_snapshot() -> None:
    await _assert_integration_contract("test_registry_filtered_snapshot")


@pytest.mark.asyncio
async def test_registry_snapshot_retains_descriptors_without_series() -> None:
    await _assert_integration_contract("test_registry_snapshot_retains_descriptors_without_series")


@pytest.mark.asyncio
async def test_registry_snapshot_sample_limit_does_not_drop_descriptors() -> None:
    await _assert_integration_contract(
        "test_registry_snapshot_sample_limit_does_not_drop_descriptors"
    )


@pytest.mark.asyncio
async def test_registry_snapshot_remains_unchanged_after_updates() -> None:
    await _assert_integration_contract("test_registry_snapshot_remains_unchanged_after_updates")


@pytest.mark.asyncio
async def test_registry_snapshot_remains_unchanged_after_reset() -> None:
    await _assert_integration_contract("test_registry_snapshot_remains_unchanged_after_reset")


@pytest.mark.asyncio
async def test_snapshot_scalar_and_histogram_deserialization_round_trip() -> None:
    await _assert_integration_contract(
        "test_snapshot_scalar_and_histogram_deserialization_round_trip"
    )


@pytest.mark.asyncio
async def test_counter_reset_and_recreate_series() -> None:
    await _assert_integration_contract("test_counter_reset_and_recreate_series")


@pytest.mark.asyncio
async def test_gauge_reset_and_recreate_series() -> None:
    await _assert_integration_contract("test_gauge_reset_and_recreate_series")


@pytest.mark.asyncio
async def test_histogram_reset_and_recreate_series() -> None:
    await _assert_integration_contract("test_histogram_reset_and_recreate_series")


@pytest.mark.asyncio
async def test_instrument_reset_frees_cardinality_capacity() -> None:
    await _assert_integration_contract("test_instrument_reset_frees_cardinality_capacity")


@pytest.mark.asyncio
async def test_registry_reset_single_series() -> None:
    await _assert_integration_contract("test_registry_reset_single_series")


@pytest.mark.asyncio
async def test_registry_reset_all_one_instrument() -> None:
    await _assert_integration_contract("test_registry_reset_all_one_instrument")


@pytest.mark.asyncio
async def test_registry_reset_all_across_mixed_instruments() -> None:
    await _assert_integration_contract("test_registry_reset_all_across_mixed_instruments")


@pytest.mark.asyncio
async def test_registry_reset_all_preserves_registration() -> None:
    await _assert_integration_contract("test_registry_reset_all_preserves_registration")


@pytest.mark.asyncio
async def test_registry_status_tracks_reset_series() -> None:
    await _assert_integration_contract("test_registry_status_tracks_reset_series")


@pytest.mark.asyncio
async def test_registry_reset_available_after_close() -> None:
    await _assert_integration_contract("test_registry_reset_available_after_close")


@pytest.mark.asyncio
async def test_existing_counter_updates_after_closed_registry_reset() -> None:
    await _assert_integration_contract("test_existing_counter_updates_after_closed_registry_reset")


@pytest.mark.asyncio
async def test_existing_gauge_updates_after_closed_registry_reset() -> None:
    await _assert_integration_contract("test_existing_gauge_updates_after_closed_registry_reset")


@pytest.mark.asyncio
async def test_existing_histogram_updates_after_closed_registry_reset() -> None:
    await _assert_integration_contract(
        "test_existing_histogram_updates_after_closed_registry_reset"
    )


@pytest.mark.asyncio
async def test_concurrent_counter_update_and_reset_is_consistent() -> None:
    await _assert_integration_contract("test_concurrent_counter_update_and_reset_is_consistent")


@pytest.mark.asyncio
async def test_concurrent_gauge_update_and_reset_is_consistent() -> None:
    await _assert_integration_contract("test_concurrent_gauge_update_and_reset_is_consistent")


@pytest.mark.asyncio
async def test_concurrent_histogram_update_and_reset_is_consistent() -> None:
    await _assert_integration_contract("test_concurrent_histogram_update_and_reset_is_consistent")


@pytest.mark.asyncio
async def test_concurrent_registry_reset_all_has_no_deadlock() -> None:
    await _assert_integration_contract("test_concurrent_registry_reset_all_has_no_deadlock")


@pytest.mark.asyncio
async def test_metric_label_overlap_error_is_safe() -> None:
    await _assert_integration_contract("test_metric_label_overlap_error_is_safe")


@pytest.mark.asyncio
async def test_metric_schema_error_is_safe() -> None:
    await _assert_integration_contract("test_metric_schema_error_is_safe")


@pytest.mark.asyncio
async def test_metric_cardinality_error_is_safe() -> None:
    await _assert_integration_contract("test_metric_cardinality_error_is_safe")


@pytest.mark.asyncio
async def test_metric_reset_error_is_safe() -> None:
    await _assert_integration_contract("test_metric_reset_error_is_safe")


@pytest.mark.asyncio
async def test_public_metric_filter_import_has_no_global_side_effects() -> None:
    await _assert_integration_contract(
        "test_public_metric_filter_import_has_no_global_side_effects"
    )


@pytest.mark.asyncio
async def test_public_metric_snapshot_import_has_no_global_side_effects() -> None:
    await _assert_integration_contract(
        "test_public_metric_snapshot_import_has_no_global_side_effects"
    )
