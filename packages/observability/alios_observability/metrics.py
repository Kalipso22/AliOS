"""Provider-neutral, in-memory metric contracts and instruments."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, Self, cast

from alios_core.errors import (
    MetricCardinalityError,
    MetricDefinitionError,
    MetricRegistryClosedError,
    MetricValueError,
    ResourceConflictError,
    ResourceNotFoundError,
    ValidationError,
)
from alios_core.ids import Identifier
from alios_core.types import utc_now

_METRIC = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,254}$")
_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_RESERVED = frozenset({"__name__", "__metric__", "__kind__", "__unit__", "__description__"})


class MetricKind(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"

    @classmethod
    def parse(cls, value: str) -> Self:
        try:
            return cls(value)
        except (TypeError, ValueError) as error:
            raise ValidationError("Invalid metric kind") from error


def _name(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _METRIC.fullmatch(value)
        or ".." in value
        or value.endswith(".")
    ):
        raise MetricDefinitionError("Invalid metric name")
    return value


def _label_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _LABEL.fullmatch(value)
        or value.startswith("__")
        or value in _RESERVED
    ):
        raise MetricDefinitionError("Invalid metric label name")
    return value


def _label_value(value: object) -> str:
    if value is None:
        result = ""
    elif isinstance(value, bool):
        result = str(value).lower()
    elif isinstance(value, int):
        result = str(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise MetricValueError("Invalid metric label value")
        result = format(value, ".17g")
    elif isinstance(value, StrEnum):
        result = value.value
    elif isinstance(value, Identifier):
        result = str(value)
    elif isinstance(value, str):
        result = value
    else:
        raise MetricValueError("Invalid metric label value")
    if len(result) > 256 or any(ord(char) < 32 for char in result):
        raise MetricValueError("Invalid metric label value")
    return result


def _number(value: int | float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise MetricValueError("Invalid metric value")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError
    return int(value)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MetricValueError("Metric timestamp must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class MetricLabelSet:
    values: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.values, Mapping):
            raise MetricValueError("Invalid metric labels")
        normalized = {_label_name(name): _label_value(value) for name, value in self.values.items()}
        object.__setattr__(self, "values", MappingProxyType(dict(sorted(normalized.items()))))

    @classmethod
    def create(cls, values: Mapping[str, object] | None = None, **kwargs: object) -> Self:
        return cls({**(dict(values) if values is not None else {}), **kwargs})

    def __hash__(self) -> int:
        return hash(tuple(self.values.items()))

    def to_dict(self) -> dict[str, str]:
        return {name: cast(str, value) for name, value in self.values.items()}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        if not isinstance(value, Mapping):
            raise MetricValueError("Invalid metric labels")
        return cls(value)

    def with_values(self, values: Mapping[str, object] | None = None, **kwargs: object) -> Self:
        return type(self).create({**self.values, **(dict(values) if values else {}), **kwargs})


@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    name: str
    kind: MetricKind
    description: str
    unit: str | None = None
    label_names: tuple[str, ...] = ()
    maximum_series: int = 1_000
    histogram_boundaries: tuple[int | float, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name))
        if not isinstance(self.kind, MetricKind):
            raise MetricDefinitionError("Invalid metric kind")
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or len(self.description.strip()) > 1024
            or "\0" in self.description
            or any(ord(c) < 32 and c not in "\n\r" for c in self.description)
        ):
            raise MetricDefinitionError("Invalid metric description")
        object.__setattr__(self, "description", self.description.strip())
        if self.unit is not None:
            if (
                not isinstance(self.unit, str)
                or not self.unit.strip()
                or len(self.unit.strip()) > 64
                or any(c.isspace() or ord(c) < 32 for c in self.unit.strip())
            ):
                raise MetricDefinitionError("Invalid metric unit")
            object.__setattr__(self, "unit", self.unit.strip())
        labels = tuple(_label_name(item) for item in self.label_names)
        if len(set(labels)) != len(labels):
            raise MetricDefinitionError("Duplicate metric label names")
        if (
            isinstance(self.maximum_series, bool)
            or not isinstance(self.maximum_series, int)
            or not 1 <= self.maximum_series <= 1_000_000
        ):
            raise MetricDefinitionError("Invalid metric series limit")
        object.__setattr__(self, "label_names", labels)
        boundaries = self.histogram_boundaries
        if self.kind is MetricKind.HISTOGRAM:
            if (
                not isinstance(boundaries, tuple | list)
                or not boundaries
                or len(boundaries) > 1_000
            ):
                raise MetricDefinitionError("Invalid histogram boundaries")
            checked: list[int | float] = []
            for boundary in boundaries:
                if (
                    isinstance(boundary, bool)
                    or not isinstance(boundary, (int, float))
                    or not math.isfinite(boundary)
                    or (checked and boundary <= checked[-1])
                ):
                    raise MetricDefinitionError("Invalid histogram boundaries")
                checked.append(boundary)
            object.__setattr__(self, "histogram_boundaries", tuple(checked))
        elif boundaries is not None:
            raise MetricDefinitionError("Histogram boundaries require a histogram")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "description": self.description,
            "unit": self.unit,
            "label_names": list(self.label_names),
            "maximum_series": self.maximum_series,
            "histogram_boundaries": list(self.histogram_boundaries)
            if self.histogram_boundaries
            else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            labels = value.get("label_names", ())
            name = value.get("name")
            kind = value.get("kind")
            description = value.get("description")
            if not isinstance(labels, tuple | list) or not all(isinstance(x, str) for x in labels):
                raise ValueError
            unit = value.get("unit")
            maximum_series = value.get("maximum_series", 1000)
            boundaries = value.get("histogram_boundaries")
            if unit is not None and not isinstance(unit, str):
                raise ValueError
            if (
                not isinstance(name, str)
                or not isinstance(kind, str)
                or not isinstance(description, str)
            ):
                raise ValueError
            if isinstance(maximum_series, bool) or not isinstance(maximum_series, (int, str)):
                raise ValueError
            if boundaries is not None and (
                not isinstance(boundaries, (tuple, list))
                or any(
                    isinstance(item, bool) or not isinstance(item, (int, float))
                    for item in boundaries
                )
            ):
                raise ValueError
            return cls(
                name,
                MetricKind.parse(kind),
                description,
                unit,
                tuple(labels),
                int(maximum_series),
                tuple(boundaries) if boundaries is not None else None,
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise MetricDefinitionError("Invalid metric descriptor") from error


@dataclass(frozen=True, slots=True)
class MetricPoint:
    descriptor: MetricDescriptor
    labels: MetricLabelSet
    value: int | float
    created_at: datetime
    updated_at: datetime
    update_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, MetricDescriptor) or not isinstance(
            self.labels, MetricLabelSet
        ):
            raise MetricValueError("Invalid metric point")
        object.__setattr__(self, "value", _number(self.value))
        _aware(self.created_at)
        _aware(self.updated_at)
        if (
            self.updated_at < self.created_at
            or isinstance(self.update_count, bool)
            or not isinstance(self.update_count, int)
            or self.update_count < 1
        ):
            raise MetricValueError("Invalid metric point")

    def to_dict(self) -> dict[str, object]:
        return {
            "descriptor": self.descriptor.to_dict(),
            "labels": self.labels.to_dict(),
            "value": self.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "update_count": self.update_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        descriptor = value.get("descriptor")
        labels = value.get("labels")
        created_at = value.get("created_at")
        updated_at = value.get("updated_at")
        raw_value = value.get("value")
        update_count = value.get("update_count")
        try:
            if (
                not isinstance(descriptor, Mapping)
                or not isinstance(labels, Mapping)
                or not isinstance(created_at, str)
                or not isinstance(updated_at, str)
                or isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
                or isinstance(update_count, bool)
                or not isinstance(update_count, int)
            ):
                raise ValueError
            return cls(
                MetricDescriptor.from_dict(descriptor),
                MetricLabelSet.from_dict(labels),
                raw_value,
                datetime.fromisoformat(created_at),
                datetime.fromisoformat(updated_at),
                update_count,
            )
        except (TypeError, ValueError, MetricDefinitionError, MetricValueError) as error:
            raise MetricValueError("Invalid metric point") from error


@dataclass(frozen=True, slots=True)
class HistogramPoint:
    descriptor: MetricDescriptor
    labels: MetricLabelSet
    bucket_counts: tuple[int, ...]
    count: int
    sum: int | float
    minimum: int | float | None
    maximum: int | float | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.descriptor, MetricDescriptor)
            or self.descriptor.kind is not MetricKind.HISTOGRAM
            or not isinstance(self.labels, MetricLabelSet)
        ):
            raise MetricValueError("Invalid histogram point")
        boundaries = self.descriptor.histogram_boundaries or ()
        if (
            len(self.bucket_counts) != len(boundaries) + 1
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.bucket_counts
            )
            or any(
                right < left
                for left, right in zip(self.bucket_counts, self.bucket_counts[1:], strict=False)
            )
            or isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 1
            or self.bucket_counts[-1] != self.count
        ):
            raise MetricValueError("Invalid histogram point")
        _number(self.sum)
        _aware(self.created_at)
        _aware(self.updated_at)
        if (
            self.minimum is None
            or self.maximum is None
            or _number(self.minimum) > _number(self.maximum)
            or self.updated_at < self.created_at
        ):
            raise MetricValueError("Invalid histogram point")

    def to_dict(self) -> dict[str, object]:
        return {
            "descriptor": self.descriptor.to_dict(),
            "labels": self.labels.to_dict(),
            "bucket_counts": list(self.bucket_counts),
            "count": self.count,
            "sum": self.sum,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


MetricSample = MetricPoint | HistogramPoint


class MetricInstrument(Protocol):
    @property
    def descriptor(self) -> MetricDescriptor: ...
    async def collect(self) -> tuple[MetricSample, ...]: ...
    async def series_count(self) -> int: ...


class Counter(MetricInstrument, Protocol):
    async def add(
        self,
        amount: int | float = 1,
        *,
        labels: MetricLabelSet | Mapping[str, object] | None = None,
    ) -> MetricPoint: ...


class Gauge(MetricInstrument, Protocol):
    async def set(
        self, value: int | float, *, labels: MetricLabelSet | Mapping[str, object] | None = None
    ) -> MetricPoint: ...


class Histogram(MetricInstrument, Protocol):
    async def observe(
        self, value: int | float, *, labels: MetricLabelSet | Mapping[str, object] | None = None
    ) -> HistogramPoint: ...
    async def add(
        self, amount: int | float, *, labels: MetricLabelSet | Mapping[str, object] | None = None
    ) -> MetricPoint: ...
    async def subtract(
        self, amount: int | float, *, labels: MetricLabelSet | Mapping[str, object] | None = None
    ) -> MetricPoint: ...


class MetricRegistry(Protocol):
    async def register_counter(self, descriptor: MetricDescriptor) -> Counter: ...
    async def register_gauge(self, descriptor: MetricDescriptor) -> Gauge: ...
    async def register_histogram(self, descriptor: MetricDescriptor) -> Histogram: ...
    async def get(self, name: str) -> MetricInstrument: ...
    async def get_optional(self, name: str) -> MetricInstrument | None: ...
    async def list_descriptors(self) -> tuple[MetricDescriptor, ...]: ...
    async def collect(self) -> tuple[MetricPoint, ...]: ...
    async def close(self) -> None: ...


class _Instrument:
    def __init__(self, descriptor: MetricDescriptor, clock: Callable[[], datetime]) -> None:
        self._descriptor = descriptor
        self._clock = clock
        self._series: dict[MetricLabelSet, MetricPoint] = {}
        self._lock = asyncio.Lock()

    @property
    def descriptor(self) -> MetricDescriptor:
        return self._descriptor

    def _labels(self, labels: MetricLabelSet | Mapping[str, object] | None) -> MetricLabelSet:
        result = labels if isinstance(labels, MetricLabelSet) else MetricLabelSet.create(labels)
        if tuple(result.values) != tuple(sorted(self._descriptor.label_names)):
            raise MetricValueError(
                "Metric label schema mismatch",
                {"expected": list(self._descriptor.label_names), "received": list(result.values)},
            )
        return result

    async def series_count(self) -> int:
        async with self._lock:
            return len(self._series)

    async def collect(self) -> tuple[MetricSample, ...]:
        async with self._lock:
            return tuple(
                sorted(self._series.values(), key=lambda point: tuple(point.labels.values.items()))
            )

    async def _update(
        self,
        operation: Callable[[int | float], int | float],
        labels: MetricLabelSet | Mapping[str, object] | None,
    ) -> MetricPoint:
        label_set = self._labels(labels)
        async with self._lock:
            current = self._series.get(label_set)
            if current is None and len(self._series) >= self._descriptor.maximum_series:
                raise MetricCardinalityError(
                    "Metric series limit reached",
                    {
                        "metric": self._descriptor.name,
                        "maximum_series": self._descriptor.maximum_series,
                        "series_count": len(self._series),
                    },
                )
            now = _aware(self._clock())
            prior = 0 if current is None else current.value
            value = _number(operation(prior))
            if current is not None and now < current.created_at:
                raise MetricValueError("Metric clock moved backwards")
            point = MetricPoint(
                self._descriptor,
                label_set,
                value,
                now if current is None else current.created_at,
                now,
                1 if current is None else current.update_count + 1,
            )
            self._series[label_set] = point
            return point


class _Counter(_Instrument):
    async def add(
        self,
        amount: int | float = 1,
        *,
        labels: MetricLabelSet | Mapping[str, object] | None = None,
    ) -> MetricPoint:
        amount = _number(amount)
        if amount < 0:
            raise MetricValueError("Counter amount cannot be negative")
        return await self._update(lambda value: value + amount, labels)


class _Gauge(_Instrument):
    async def set(
        self, value: int | float, *, labels: MetricLabelSet | Mapping[str, object] | None = None
    ) -> MetricPoint:
        return await self._update(lambda _: _number(value), labels)

    async def add(
        self, amount: int | float, *, labels: MetricLabelSet | Mapping[str, object] | None = None
    ) -> MetricPoint:
        amount = _number(amount)
        return await self._update(lambda value: value + amount, labels)

    async def subtract(
        self, amount: int | float, *, labels: MetricLabelSet | Mapping[str, object] | None = None
    ) -> MetricPoint:
        amount = _number(amount)
        return await self._update(lambda value: value - amount, labels)


class _Histogram:
    def __init__(self, descriptor: MetricDescriptor, clock: Callable[[], datetime]) -> None:
        self._descriptor = descriptor
        self._clock = clock
        self._series: dict[MetricLabelSet, HistogramPoint] = {}
        self._lock = asyncio.Lock()

    @property
    def descriptor(self) -> MetricDescriptor:
        return self._descriptor

    def _labels(self, labels: MetricLabelSet | Mapping[str, object] | None) -> MetricLabelSet:
        result = labels if isinstance(labels, MetricLabelSet) else MetricLabelSet.create(labels)
        if tuple(result.values) != tuple(sorted(self._descriptor.label_names)):
            raise MetricValueError("Metric label schema mismatch")
        return result

    async def series_count(self) -> int:
        async with self._lock:
            return len(self._series)

    async def observe(
        self, value: int | float, *, labels: MetricLabelSet | Mapping[str, object] | None = None
    ) -> HistogramPoint:
        value = _number(value)
        label_set = self._labels(labels)
        async with self._lock:
            current = self._series.get(label_set)
            if current is None and len(self._series) >= self._descriptor.maximum_series:
                raise MetricCardinalityError("Metric series limit reached")
            now = _aware(self._clock())
            if current is not None and now < current.updated_at:
                raise MetricValueError("Metric clock moved backwards")
            boundaries = self._descriptor.histogram_boundaries or ()
            buckets = (
                [0] * (len(boundaries) + 1) if current is None else list(current.bucket_counts)
            )
            for index, boundary in enumerate(boundaries):
                if value <= boundary:
                    buckets[index] += 1
            buckets[-1] += 1
            point = HistogramPoint(
                self._descriptor,
                label_set,
                tuple(buckets),
                1 if current is None else current.count + 1,
                value if current is None else _number(current.sum + value),
                value if current is None else min(current.minimum or value, value),
                value if current is None else max(current.maximum or value, value),
                now if current is None else current.created_at,
                now,
            )
            self._series[label_set] = point
            return point

    async def collect(self) -> tuple[HistogramPoint, ...]:
        async with self._lock:
            return tuple(
                sorted(self._series.values(), key=lambda point: tuple(point.labels.values.items()))
            )


@dataclass(frozen=True, slots=True)
class MetricRegistryStatus:
    instrument_count: int
    series_count: int
    maximum_instruments: int
    closed: bool
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in (self.instrument_count, self.series_count)
            )
            or isinstance(self.maximum_instruments, bool)
            or not isinstance(self.maximum_instruments, int)
            or self.maximum_instruments < 1
            or self.instrument_count > self.maximum_instruments
            or not isinstance(self.closed, bool)
        ):
            raise MetricValueError("Invalid metric registry status")
        _aware(self.created_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument_count": self.instrument_count,
            "series_count": self.series_count,
            "maximum_instruments": self.maximum_instruments,
            "closed": self.closed,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            created_at = value.get("created_at")
            closed = value.get("closed")
            if not isinstance(created_at, str) or not isinstance(closed, bool):
                raise ValueError
            return cls(
                _integer(value["instrument_count"]),
                _integer(value["series_count"]),
                _integer(value["maximum_instruments"]),
                closed,
                datetime.fromisoformat(created_at),
            )
        except (KeyError, TypeError, ValueError, MetricValueError) as error:
            raise MetricValueError("Invalid metric registry status") from error


class InMemoryMetricRegistry:
    def __init__(
        self,
        *,
        default_maximum_series: int = 1000,
        maximum_instruments: int = 10000,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if any(
            isinstance(x, bool) or not isinstance(x, int) or x < 1
            for x in (default_maximum_series, maximum_instruments)
        ):
            raise MetricDefinitionError("Invalid registry limit")
        self._default = default_maximum_series
        self._maximum = maximum_instruments
        self._clock = clock
        self._items: dict[str, _Instrument | _Histogram] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._created_at = _aware(clock())

    async def _register(self, d: MetricDescriptor) -> _Instrument | _Histogram:
        async with self._lock:
            if self._closed:
                raise MetricRegistryClosedError("Metric registry is closed")
            if d.name in self._items:
                raise ResourceConflictError("Metric name already registered", {"metric": d.name})
            if len(self._items) >= self._maximum:
                raise MetricCardinalityError(
                    "Metric instrument limit reached",
                    {"maximum_instruments": self._maximum, "instrument_count": len(self._items)},
                )
            if d.kind is MetricKind.COUNTER:
                item: _Instrument | _Histogram = _Counter(d, self._clock)
            elif d.kind is MetricKind.GAUGE:
                item = _Gauge(d, self._clock)
            elif d.kind is MetricKind.HISTOGRAM:
                item = _Histogram(d, self._clock)
            else:
                raise MetricDefinitionError("Invalid metric kind")
            self._items[d.name] = item
            return item

    async def register_counter(self, d: MetricDescriptor) -> Counter:
        if not isinstance(d, MetricDescriptor) or d.kind is not MetricKind.COUNTER:
            raise MetricDefinitionError("Metric kind mismatch")
        return cast(Counter, await self._register(d))

    async def register_gauge(self, d: MetricDescriptor) -> Gauge:
        if not isinstance(d, MetricDescriptor) or d.kind is not MetricKind.GAUGE:
            raise MetricDefinitionError("Metric kind mismatch")
        return cast(Gauge, await self._register(d))

    async def register_histogram(self, d: MetricDescriptor) -> Histogram:
        if not isinstance(d, MetricDescriptor) or d.kind is not MetricKind.HISTOGRAM:
            raise MetricDefinitionError("Metric kind mismatch")
        return cast(Histogram, await self._register(d))

    async def counter(
        self,
        name: str,
        *,
        description: str,
        unit: str | None = None,
        label_names: tuple[str, ...] = (),
        maximum_series: int | None = None,
    ) -> Counter:
        return await self.register_counter(
            MetricDescriptor(
                name,
                MetricKind.COUNTER,
                description,
                unit,
                label_names,
                self._default if maximum_series is None else maximum_series,
            )
        )

    async def gauge(
        self,
        name: str,
        *,
        description: str,
        unit: str | None = None,
        label_names: tuple[str, ...] = (),
        maximum_series: int | None = None,
    ) -> Gauge:
        return await self.register_gauge(
            MetricDescriptor(
                name,
                MetricKind.GAUGE,
                description,
                unit,
                label_names,
                self._default if maximum_series is None else maximum_series,
            )
        )

    async def histogram(
        self,
        name: str,
        *,
        description: str,
        boundaries: tuple[int | float, ...],
        unit: str | None = None,
        label_names: tuple[str, ...] = (),
        maximum_series: int | None = None,
    ) -> Histogram:
        return await self.register_histogram(
            MetricDescriptor(
                name,
                MetricKind.HISTOGRAM,
                description,
                unit,
                label_names,
                self._default if maximum_series is None else maximum_series,
                boundaries,
            )
        )

    async def get_optional(self, name: str) -> MetricInstrument | None:
        _name(name)
        async with self._lock:
            return self._items.get(name)

    async def get(self, name: str) -> MetricInstrument:
        item = await self.get_optional(name)
        if item is None:
            raise ResourceNotFoundError("Metric was not found", {"metric": name})
        return item

    async def get_counter(self, name: str) -> Counter:
        item = await self.get(name)
        if item.descriptor.kind is not MetricKind.COUNTER:
            raise MetricDefinitionError("Metric kind mismatch")
        return cast(Counter, item)

    async def get_gauge(self, name: str) -> Gauge:
        item = await self.get(name)
        if item.descriptor.kind is not MetricKind.GAUGE:
            raise MetricDefinitionError("Metric kind mismatch")
        return cast(Gauge, item)

    async def get_histogram(self, name: str) -> Histogram:
        item = await self.get(name)
        if item.descriptor.kind is not MetricKind.HISTOGRAM:
            raise MetricDefinitionError("Metric kind mismatch")
        return cast(Histogram, item)

    async def list_descriptors(self) -> tuple[MetricDescriptor, ...]:
        async with self._lock:
            return tuple(self._items[name].descriptor for name in sorted(self._items))

    async def collect(self) -> tuple[MetricSample, ...]:
        async with self._lock:
            items = tuple(self._items.items())
        points: list[MetricSample] = [point for _, item in items for point in await item.collect()]
        return tuple(
            sorted(
                points,
                key=lambda point: (point.descriptor.name, tuple(point.labels.values.items())),
            )
        )

    async def status(self) -> MetricRegistryStatus:
        async with self._lock:
            items = tuple(self._items.values())
            closed = self._closed
        series_count = 0
        for item in items:
            series_count += await item.series_count()
        return MetricRegistryStatus(
            len(items),
            series_count,
            self._maximum,
            closed,
            self._created_at,
        )

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
