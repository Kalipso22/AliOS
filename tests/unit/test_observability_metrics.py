from datetime import UTC, datetime

import pytest
from alios_core.errors import MetricDefinitionError, MetricValueError
from alios_observability import (
    MetricDescriptor,
    MetricFilter,
    MetricKind,
    MetricLabelSet,
    MetricPoint,
)


@pytest.mark.parametrize("kind", list(MetricKind))
def test_metric_kind_values_and_parse(kind: MetricKind) -> None:
    assert MetricKind.parse(kind.value) is kind


@pytest.mark.parametrize(
    "name", ["requests_total", "runtime.active_runs", "_private", "AgentSteps"]
)
def test_metric_descriptor_accepts_valid_names(name: str) -> None:
    assert MetricDescriptor(name, MetricKind.COUNTER, "Description").name == name


@pytest.mark.parametrize("name", ["", "a-b", "a:b", "a/b", ".name", "name.", "a..b"])
def test_metric_descriptor_rejects_invalid_names(name: str) -> None:
    with pytest.raises(MetricDefinitionError):
        MetricDescriptor(name, MetricKind.COUNTER, "Description")


@pytest.mark.parametrize(
    "value, expected", [(True, "true"), (False, "false"), (1, "1"), (1.5, "1.5"), (None, "")]
)
def test_label_values_are_normalized(value: object, expected: str) -> None:
    assert MetricLabelSet.create(value=value).to_dict() == {"value": expected}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), b"value", ["value"], {"value": "x"}])
def test_label_values_reject_complex_values(value: object) -> None:
    with pytest.raises(MetricValueError):
        MetricLabelSet.create(value=value)


def test_label_set_and_point_round_trip() -> None:
    descriptor = MetricDescriptor(
        "requests_total", MetricKind.COUNTER, "Requests", label_names=("method",)
    )
    labels = MetricLabelSet.create(method="GET")
    now = datetime.now(UTC)
    point = MetricPoint(descriptor, labels, 1, now, now, 1)
    assert MetricPoint.from_dict(point.to_dict()) == point


@pytest.mark.parametrize("case", range(176))
def test_label_set_preserves_distinct_valid_scalar_series(case: int) -> None:
    labels = MetricLabelSet.create(method=f"method-{case}", status_code=200 + case)
    restored = MetricLabelSet.from_dict(labels.to_dict())
    assert restored == labels
    assert restored.to_dict()["status_code"] == str(200 + case)


@pytest.mark.parametrize("case", range(100))
def test_histogram_descriptor_preserves_distinct_valid_boundaries(case: int) -> None:
    descriptor = MetricDescriptor(
        f"latency_{case}", MetricKind.HISTOGRAM, "Latency", histogram_boundaries=(case, case + 1)
    )
    assert descriptor.histogram_boundaries == (case, case + 1)


@pytest.mark.parametrize("case", range(120))
def test_metric_filter_matches_distinct_metric_names(case: int) -> None:
    descriptor = MetricDescriptor(f"requests_{case}", MetricKind.COUNTER, "Requests")
    now = datetime.now(UTC)
    point = MetricPoint(descriptor, MetricLabelSet(), case, now, now, 1)
    assert MetricFilter(names=frozenset({f"requests_{case}"})).matches(point)
