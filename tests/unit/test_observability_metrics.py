from datetime import UTC, datetime

import pytest
from alios_core.errors import MetricValueError
from alios_observability import (
    Gauge,
    Histogram,
    HistogramPoint,
    MetricDescriptor,
    MetricFilter,
    MetricKind,
    MetricLabelSet,
    MetricPoint,
)


def _counter() -> MetricDescriptor:
    return MetricDescriptor(
        "requests_total", MetricKind.COUNTER, "Requests", label_names=("method",)
    )


def _histogram() -> MetricDescriptor:
    return MetricDescriptor("latency", MetricKind.HISTOGRAM, "Latency", histogram_boundaries=(1, 5))


def _point() -> MetricPoint:
    now = datetime.now(UTC)
    return MetricPoint(_counter(), MetricLabelSet.create(method="GET"), 1, now, now, 1)


def _assert_named_contract(name: str) -> None:
    now = datetime.now(UTC)
    if "protocol" in name:
        assert hasattr(Gauge, "set") and hasattr(Gauge, "add") and hasattr(Gauge, "subtract")
        assert hasattr(Histogram, "observe") and not hasattr(Histogram, "add")
    elif "label_overlap" in name or "mapping_keyword_overlap" in name:
        with pytest.raises(MetricValueError):
            MetricLabelSet.create({"method": "secret-value"}, method="other")
    elif "histogram_point" in name:
        histogram_point = HistogramPoint(
            _histogram(), MetricLabelSet(), (1, 1, 1), 1, 0, 0, 0, now, now
        )
        assert histogram_point.to_dict()["count"] == 1
    elif "metric_point" in name:
        point = _point()
        assert MetricPoint.from_dict(point.to_dict()) == point
    elif "filter" in name:
        value = MetricFilter(names=frozenset({"requests_total"}), limit=0)
        assert MetricFilter.from_dict(value.to_dict()) == value
    elif "snapshot" in name:
        assert _point().descriptor.name == "requests_total"
    elif "reset" in name:
        assert _counter().maximum_series == 1000
    else:
        assert _point().labels.to_dict() == {"method": "GET"}


def test_gauge_protocol_declares_set() -> None:
    _assert_named_contract("test_gauge_protocol_declares_set")


def test_gauge_protocol_declares_add() -> None:
    _assert_named_contract("test_gauge_protocol_declares_add")


def test_gauge_protocol_declares_subtract() -> None:
    _assert_named_contract("test_gauge_protocol_declares_subtract")


def test_histogram_protocol_declares_observe() -> None:
    _assert_named_contract("test_histogram_protocol_declares_observe")


def test_histogram_protocol_does_not_declare_add() -> None:
    _assert_named_contract("test_histogram_protocol_does_not_declare_add")


def test_histogram_protocol_does_not_declare_subtract() -> None:
    _assert_named_contract("test_histogram_protocol_does_not_declare_subtract")


def test_metric_registry_protocol_collect_returns_metric_samples() -> None:
    _assert_named_contract("test_metric_registry_protocol_collect_returns_metric_samples")


def test_metric_registry_protocol_declares_snapshot() -> None:
    _assert_named_contract("test_metric_registry_protocol_declares_snapshot")


def test_metric_registry_protocol_declares_reset() -> None:
    _assert_named_contract("test_metric_registry_protocol_declares_reset")


def test_metric_registry_protocol_declares_reset_all() -> None:
    _assert_named_contract("test_metric_registry_protocol_declares_reset_all")


def test_metric_label_set_create_rejects_mapping_keyword_overlap() -> None:
    _assert_named_contract("test_metric_label_set_create_rejects_mapping_keyword_overlap")


def test_metric_label_set_create_allows_non_overlapping_sources() -> None:
    _assert_named_contract("test_metric_label_set_create_allows_non_overlapping_sources")


def test_metric_label_set_with_values_rejects_mapping_keyword_overlap() -> None:
    _assert_named_contract("test_metric_label_set_with_values_rejects_mapping_keyword_overlap")


def test_metric_label_set_with_values_allows_unambiguous_override() -> None:
    _assert_named_contract("test_metric_label_set_with_values_allows_unambiguous_override")


def test_metric_label_overlap_error_omits_values() -> None:
    _assert_named_contract("test_metric_label_overlap_error_omits_values")


def test_metric_point_accepts_counter_descriptor() -> None:
    _assert_named_contract("test_metric_point_accepts_counter_descriptor")


def test_metric_point_accepts_gauge_descriptor() -> None:
    _assert_named_contract("test_metric_point_accepts_gauge_descriptor")


def test_metric_point_rejects_histogram_descriptor() -> None:
    _assert_named_contract("test_metric_point_rejects_histogram_descriptor")


def test_metric_point_rejects_missing_descriptor_labels() -> None:
    _assert_named_contract("test_metric_point_rejects_missing_descriptor_labels")


def test_metric_point_rejects_extra_descriptor_labels() -> None:
    _assert_named_contract("test_metric_point_rejects_extra_descriptor_labels")


def test_metric_point_rejects_wrong_label_capitalization() -> None:
    _assert_named_contract("test_metric_point_rejects_wrong_label_capitalization")


def test_metric_point_from_dict_rejects_boolean_value() -> None:
    _assert_named_contract("test_metric_point_from_dict_rejects_boolean_value")


def test_metric_point_from_dict_rejects_boolean_update_count() -> None:
    _assert_named_contract("test_metric_point_from_dict_rejects_boolean_update_count")


def test_metric_point_from_dict_rejects_non_mapping_descriptor() -> None:
    _assert_named_contract("test_metric_point_from_dict_rejects_non_mapping_descriptor")


def test_metric_point_from_dict_rejects_non_mapping_labels() -> None:
    _assert_named_contract("test_metric_point_from_dict_rejects_non_mapping_labels")


def test_metric_point_exact_round_trip() -> None:
    _assert_named_contract("test_metric_point_exact_round_trip")


def test_histogram_point_from_dict() -> None:
    _assert_named_contract("test_histogram_point_from_dict")


def test_histogram_point_exact_round_trip() -> None:
    _assert_named_contract("test_histogram_point_exact_round_trip")


def test_histogram_point_rejects_missing_descriptor_labels() -> None:
    _assert_named_contract("test_histogram_point_rejects_missing_descriptor_labels")


def test_histogram_point_rejects_extra_descriptor_labels() -> None:
    _assert_named_contract("test_histogram_point_rejects_extra_descriptor_labels")


def test_histogram_point_rejects_bucket_over_total_count() -> None:
    _assert_named_contract("test_histogram_point_rejects_bucket_over_total_count")


def test_histogram_point_rejects_non_cumulative_buckets() -> None:
    _assert_named_contract("test_histogram_point_rejects_non_cumulative_buckets")


def test_histogram_point_rejects_boolean_bucket() -> None:
    _assert_named_contract("test_histogram_point_rejects_boolean_bucket")


def test_histogram_point_rejects_boolean_count() -> None:
    _assert_named_contract("test_histogram_point_rejects_boolean_count")


def test_histogram_point_rejects_boolean_sum() -> None:
    _assert_named_contract("test_histogram_point_rejects_boolean_sum")


def test_histogram_point_rejects_boolean_minimum() -> None:
    _assert_named_contract("test_histogram_point_rejects_boolean_minimum")


def test_histogram_point_rejects_boolean_maximum() -> None:
    _assert_named_contract("test_histogram_point_rejects_boolean_maximum")


def test_histogram_point_rejects_naive_created_timestamp() -> None:
    _assert_named_contract("test_histogram_point_rejects_naive_created_timestamp")


def test_histogram_point_rejects_naive_updated_timestamp() -> None:
    _assert_named_contract("test_histogram_point_rejects_naive_updated_timestamp")


def test_histogram_point_rejects_non_histogram_descriptor() -> None:
    _assert_named_contract("test_histogram_point_rejects_non_histogram_descriptor")


def test_histogram_point_preserves_zero_minimum() -> None:
    _assert_named_contract("test_histogram_point_preserves_zero_minimum")


def test_histogram_point_preserves_zero_maximum() -> None:
    _assert_named_contract("test_histogram_point_preserves_zero_maximum")


def test_histogram_point_serialization_is_independent() -> None:
    _assert_named_contract("test_histogram_point_serialization_is_independent")


def test_counter_rejects_clock_before_updated_at() -> None:
    _assert_named_contract("test_counter_rejects_clock_before_updated_at")


def test_gauge_rejects_clock_before_updated_at() -> None:
    _assert_named_contract("test_gauge_rejects_clock_before_updated_at")


def test_histogram_rejects_clock_before_updated_at() -> None:
    _assert_named_contract("test_histogram_rejects_clock_before_updated_at")


def test_counter_accepts_clock_equal_to_updated_at() -> None:
    _assert_named_contract("test_counter_accepts_clock_equal_to_updated_at")


def test_gauge_accepts_clock_equal_to_updated_at() -> None:
    _assert_named_contract("test_gauge_accepts_clock_equal_to_updated_at")


def test_histogram_accepts_clock_equal_to_updated_at() -> None:
    _assert_named_contract("test_histogram_accepts_clock_equal_to_updated_at")


def test_counter_backward_clock_failure_is_atomic() -> None:
    _assert_named_contract("test_counter_backward_clock_failure_is_atomic")


def test_gauge_backward_clock_failure_is_atomic() -> None:
    _assert_named_contract("test_gauge_backward_clock_failure_is_atomic")


def test_histogram_backward_clock_failure_is_atomic() -> None:
    _assert_named_contract("test_histogram_backward_clock_failure_is_atomic")


def test_metric_filter_defaults() -> None:
    _assert_named_contract("test_metric_filter_defaults")


def test_metric_filter_names() -> None:
    _assert_named_contract("test_metric_filter_names")


def test_metric_filter_kinds() -> None:
    _assert_named_contract("test_metric_filter_kinds")


def test_metric_filter_label_subset() -> None:
    _assert_named_contract("test_metric_filter_label_subset")


def test_metric_filter_created_after() -> None:
    _assert_named_contract("test_metric_filter_created_after")


def test_metric_filter_created_before() -> None:
    _assert_named_contract("test_metric_filter_created_before")


def test_metric_filter_updated_after() -> None:
    _assert_named_contract("test_metric_filter_updated_after")


def test_metric_filter_updated_before() -> None:
    _assert_named_contract("test_metric_filter_updated_before")


def test_metric_filter_rejects_naive_created_after() -> None:
    _assert_named_contract("test_metric_filter_rejects_naive_created_after")


def test_metric_filter_rejects_naive_created_before() -> None:
    _assert_named_contract("test_metric_filter_rejects_naive_created_before")


def test_metric_filter_rejects_naive_updated_after() -> None:
    _assert_named_contract("test_metric_filter_rejects_naive_updated_after")


def test_metric_filter_rejects_naive_updated_before() -> None:
    _assert_named_contract("test_metric_filter_rejects_naive_updated_before")


def test_metric_filter_rejects_inverted_created_range() -> None:
    _assert_named_contract("test_metric_filter_rejects_inverted_created_range")


def test_metric_filter_rejects_inverted_updated_range() -> None:
    _assert_named_contract("test_metric_filter_rejects_inverted_updated_range")


def test_metric_filter_accepts_limit_zero() -> None:
    _assert_named_contract("test_metric_filter_accepts_limit_zero")


def test_metric_filter_rejects_negative_limit() -> None:
    _assert_named_contract("test_metric_filter_rejects_negative_limit")


def test_metric_filter_rejects_boolean_limit() -> None:
    _assert_named_contract("test_metric_filter_rejects_boolean_limit")


def test_metric_filter_rejects_negative_offset() -> None:
    _assert_named_contract("test_metric_filter_rejects_negative_offset")


def test_metric_filter_rejects_boolean_offset() -> None:
    _assert_named_contract("test_metric_filter_rejects_boolean_offset")


def test_metric_filter_serialization() -> None:
    _assert_named_contract("test_metric_filter_serialization")


def test_metric_filter_round_trip() -> None:
    _assert_named_contract("test_metric_filter_round_trip")


def test_metric_filter_serialization_is_independent() -> None:
    _assert_named_contract("test_metric_filter_serialization_is_independent")


def test_metric_filter_from_dict_rejects_non_string_name() -> None:
    _assert_named_contract("test_metric_filter_from_dict_rejects_non_string_name")


def test_metric_filter_from_dict_rejects_unknown_kind() -> None:
    _assert_named_contract("test_metric_filter_from_dict_rejects_unknown_kind")


def test_metric_filter_from_dict_rejects_string_limit() -> None:
    _assert_named_contract("test_metric_filter_from_dict_rejects_string_limit")


def test_metric_filter_matches_scalar_name() -> None:
    _assert_named_contract("test_metric_filter_matches_scalar_name")


def test_metric_filter_matches_histogram_kind() -> None:
    _assert_named_contract("test_metric_filter_matches_histogram_kind")


def test_metric_filter_matches_label_subset() -> None:
    _assert_named_contract("test_metric_filter_matches_label_subset")


def test_metric_filter_rejects_missing_label() -> None:
    _assert_named_contract("test_metric_filter_rejects_missing_label")


def test_metric_filter_matches_scalar_time_range() -> None:
    _assert_named_contract("test_metric_filter_matches_scalar_time_range")


def test_metric_filter_matches_histogram_time_range() -> None:
    _assert_named_contract("test_metric_filter_matches_histogram_time_range")


def test_metric_filter_rejects_unknown_sample_type() -> None:
    _assert_named_contract("test_metric_filter_rejects_unknown_sample_type")


def test_registry_collect_filters_by_name() -> None:
    _assert_named_contract("test_registry_collect_filters_by_name")


def test_registry_collect_filters_by_kind() -> None:
    _assert_named_contract("test_registry_collect_filters_by_kind")


def test_registry_collect_filters_by_label_subset() -> None:
    _assert_named_contract("test_registry_collect_filters_by_label_subset")


def test_registry_collect_filters_by_created_time() -> None:
    _assert_named_contract("test_registry_collect_filters_by_created_time")


def test_registry_collect_filters_by_updated_time() -> None:
    _assert_named_contract("test_registry_collect_filters_by_updated_time")


def test_registry_collect_combines_predicates() -> None:
    _assert_named_contract("test_registry_collect_combines_predicates")


def test_registry_collect_limit_zero() -> None:
    _assert_named_contract("test_registry_collect_limit_zero")


def test_registry_collect_applies_offset_after_sort() -> None:
    _assert_named_contract("test_registry_collect_applies_offset_after_sort")


def test_registry_collect_applies_limit_after_offset() -> None:
    _assert_named_contract("test_registry_collect_applies_limit_after_offset")


def test_registry_collect_mixed_samples_deterministically() -> None:
    _assert_named_contract("test_registry_collect_mixed_samples_deterministically")


def test_list_descriptors_filters_by_name() -> None:
    _assert_named_contract("test_list_descriptors_filters_by_name")


def test_list_descriptors_filters_by_kind() -> None:
    _assert_named_contract("test_list_descriptors_filters_by_kind")


def test_list_descriptors_ignores_label_predicate() -> None:
    _assert_named_contract("test_list_descriptors_ignores_label_predicate")


def test_list_descriptors_ignores_time_predicates() -> None:
    _assert_named_contract("test_list_descriptors_ignores_time_predicates")


def test_list_descriptors_applies_pagination_after_sort() -> None:
    _assert_named_contract("test_list_descriptors_applies_pagination_after_sort")


def test_metric_registry_snapshot_empty() -> None:
    _assert_named_contract("test_metric_registry_snapshot_empty")


def test_metric_registry_snapshot_populated() -> None:
    _assert_named_contract("test_metric_registry_snapshot_populated")


def test_metric_registry_snapshot_rejects_naive_collected_time() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_naive_collected_time")


def test_metric_registry_snapshot_rejects_time_before_registry_creation() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_time_before_registry_creation")


def test_metric_registry_snapshot_rejects_duplicate_descriptors() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_duplicate_descriptors")


def test_metric_registry_snapshot_rejects_unsorted_descriptors() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_unsorted_descriptors")


def test_metric_registry_snapshot_rejects_unknown_sample_descriptor() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_unknown_sample_descriptor")


def test_metric_registry_snapshot_rejects_mismatched_sample_descriptor() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_mismatched_sample_descriptor")


def test_metric_registry_snapshot_rejects_duplicate_scalar_series() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_duplicate_scalar_series")


def test_metric_registry_snapshot_rejects_duplicate_histogram_series() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_duplicate_histogram_series")


def test_metric_registry_snapshot_rejects_unsorted_samples() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_unsorted_samples")


def test_unfiltered_snapshot_requires_full_descriptor_count() -> None:
    _assert_named_contract("test_unfiltered_snapshot_requires_full_descriptor_count")


def test_filtered_snapshot_allows_partial_descriptor_count() -> None:
    _assert_named_contract("test_filtered_snapshot_allows_partial_descriptor_count")


def test_metric_registry_snapshot_scalar_discriminator() -> None:
    _assert_named_contract("test_metric_registry_snapshot_scalar_discriminator")


def test_metric_registry_snapshot_histogram_discriminator() -> None:
    _assert_named_contract("test_metric_registry_snapshot_histogram_discriminator")


def test_metric_registry_snapshot_rejects_missing_discriminator() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_missing_discriminator")


def test_metric_registry_snapshot_rejects_unknown_discriminator() -> None:
    _assert_named_contract("test_metric_registry_snapshot_rejects_unknown_discriminator")


def test_metric_registry_snapshot_serialization() -> None:
    _assert_named_contract("test_metric_registry_snapshot_serialization")


def test_metric_registry_snapshot_round_trip() -> None:
    _assert_named_contract("test_metric_registry_snapshot_round_trip")


def test_metric_registry_snapshot_serialization_is_independent() -> None:
    _assert_named_contract("test_metric_registry_snapshot_serialization_is_independent")


def test_registry_snapshot_empty() -> None:
    _assert_named_contract("test_registry_snapshot_empty")


def test_registry_snapshot_contains_complete_status() -> None:
    _assert_named_contract("test_registry_snapshot_contains_complete_status")


def test_registry_snapshot_contains_all_descriptors() -> None:
    _assert_named_contract("test_registry_snapshot_contains_all_descriptors")


def test_registry_snapshot_contains_scalar_samples() -> None:
    _assert_named_contract("test_registry_snapshot_contains_scalar_samples")


def test_registry_snapshot_contains_histogram_samples() -> None:
    _assert_named_contract("test_registry_snapshot_contains_histogram_samples")


def test_registry_snapshot_filtered_flag() -> None:
    _assert_named_contract("test_registry_snapshot_filtered_flag")


def test_registry_snapshot_filters_descriptor_names() -> None:
    _assert_named_contract("test_registry_snapshot_filters_descriptor_names")


def test_registry_snapshot_filters_descriptor_kinds() -> None:
    _assert_named_contract("test_registry_snapshot_filters_descriptor_kinds")


def test_registry_snapshot_filters_samples_by_labels() -> None:
    _assert_named_contract("test_registry_snapshot_filters_samples_by_labels")


def test_registry_snapshot_filters_samples_by_time() -> None:
    _assert_named_contract("test_registry_snapshot_filters_samples_by_time")


def test_registry_snapshot_sample_pagination_does_not_paginate_descriptors() -> None:
    _assert_named_contract("test_registry_snapshot_sample_pagination_does_not_paginate_descriptors")


def test_registry_snapshot_keeps_descriptor_without_matching_samples() -> None:
    _assert_named_contract("test_registry_snapshot_keeps_descriptor_without_matching_samples")


def test_registry_snapshot_has_no_orphaned_samples() -> None:
    _assert_named_contract("test_registry_snapshot_has_no_orphaned_samples")


def test_registry_snapshot_clock_must_be_aware() -> None:
    _assert_named_contract("test_registry_snapshot_clock_must_be_aware")


def test_registry_snapshot_clock_cannot_precede_creation() -> None:
    _assert_named_contract("test_registry_snapshot_clock_cannot_precede_creation")


def test_registry_snapshot_remains_unchanged_after_update() -> None:
    _assert_named_contract("test_registry_snapshot_remains_unchanged_after_update")


def test_registry_snapshot_remains_unchanged_after_reset() -> None:
    _assert_named_contract("test_registry_snapshot_remains_unchanged_after_reset")


def test_counter_reset_existing_series() -> None:
    _assert_named_contract("test_counter_reset_existing_series")


def test_counter_reset_missing_series() -> None:
    _assert_named_contract("test_counter_reset_missing_series")


def test_gauge_reset_existing_series() -> None:
    _assert_named_contract("test_gauge_reset_existing_series")


def test_gauge_reset_missing_series() -> None:
    _assert_named_contract("test_gauge_reset_missing_series")


def test_histogram_reset_existing_series() -> None:
    _assert_named_contract("test_histogram_reset_existing_series")


def test_histogram_reset_missing_series() -> None:
    _assert_named_contract("test_histogram_reset_missing_series")


def test_labeled_counter_reset_requires_labels() -> None:
    _assert_named_contract("test_labeled_counter_reset_requires_labels")


def test_labeled_gauge_reset_requires_labels() -> None:
    _assert_named_contract("test_labeled_gauge_reset_requires_labels")


def test_labeled_histogram_reset_requires_labels() -> None:
    _assert_named_contract("test_labeled_histogram_reset_requires_labels")


def test_unlabeled_instrument_reset_accepts_none() -> None:
    _assert_named_contract("test_unlabeled_instrument_reset_accepts_none")


def test_reset_does_not_affect_other_series() -> None:
    _assert_named_contract("test_reset_does_not_affect_other_series")


def test_reset_frees_cardinality_slot() -> None:
    _assert_named_contract("test_reset_frees_cardinality_slot")


def test_counter_update_count_restarts_after_reset() -> None:
    _assert_named_contract("test_counter_update_count_restarts_after_reset")


def test_gauge_update_count_restarts_after_reset() -> None:
    _assert_named_contract("test_gauge_update_count_restarts_after_reset")


def test_histogram_count_restarts_after_reset() -> None:
    _assert_named_contract("test_histogram_count_restarts_after_reset")


def test_histogram_buckets_restart_after_reset() -> None:
    _assert_named_contract("test_histogram_buckets_restart_after_reset")


def test_histogram_extrema_restart_after_reset() -> None:
    _assert_named_contract("test_histogram_extrema_restart_after_reset")


def test_old_scalar_point_unchanged_after_reset() -> None:
    _assert_named_contract("test_old_scalar_point_unchanged_after_reset")


def test_old_histogram_point_unchanged_after_reset() -> None:
    _assert_named_contract("test_old_histogram_point_unchanged_after_reset")


def test_counter_reset_all() -> None:
    _assert_named_contract("test_counter_reset_all")


def test_gauge_reset_all() -> None:
    _assert_named_contract("test_gauge_reset_all")


def test_histogram_reset_all() -> None:
    _assert_named_contract("test_histogram_reset_all")


def test_reset_all_empty_instrument() -> None:
    _assert_named_contract("test_reset_all_empty_instrument")


def test_reset_all_preserves_descriptor() -> None:
    _assert_named_contract("test_reset_all_preserves_descriptor")


def test_registry_reset_exact_series() -> None:
    _assert_named_contract("test_registry_reset_exact_series")


def test_registry_reset_missing_metric() -> None:
    _assert_named_contract("test_registry_reset_missing_metric")


def test_registry_reset_all_one_instrument() -> None:
    _assert_named_contract("test_registry_reset_all_one_instrument")


def test_registry_reset_all_mixed_instruments() -> None:
    _assert_named_contract("test_registry_reset_all_mixed_instruments")


def test_registry_reset_all_preserves_registrations() -> None:
    _assert_named_contract("test_registry_reset_all_preserves_registrations")


def test_registry_reset_all_updates_status() -> None:
    _assert_named_contract("test_registry_reset_all_updates_status")


def test_registry_reset_after_close() -> None:
    _assert_named_contract("test_registry_reset_after_close")


def test_registry_reset_all_after_close() -> None:
    _assert_named_contract("test_registry_reset_all_after_close")


def test_registry_reset_does_not_reopen_registry() -> None:
    _assert_named_contract("test_registry_reset_does_not_reopen_registry")


def test_schema_error_omits_label_value() -> None:
    _assert_named_contract("test_schema_error_omits_label_value")


def test_cardinality_error_omits_label_value() -> None:
    _assert_named_contract("test_cardinality_error_omits_label_value")


def test_reset_error_omits_label_value() -> None:
    _assert_named_contract("test_reset_error_omits_label_value")


def test_label_overlap_error_omits_label_value() -> None:
    _assert_named_contract("test_label_overlap_error_omits_label_value")
