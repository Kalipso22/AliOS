"""Immutable, source-aware configuration primitives."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from os import environ
from types import MappingProxyType
from typing import Any, TypeVar, cast

from .errors import ConfigurationError
from .types import JsonValue, utc_now

T = TypeVar("T")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @classmethod
    def parse(cls, value: str) -> Environment:
        try:
            return cls(value.lower())
        except ValueError as error:
            raise ConfigurationError("Invalid environment", {"environment": value}) from error


class ConfigurationSource(IntEnum):
    DEFAULT = 0
    FILE = 1
    ENVIRONMENT = 2
    SECRET_REFERENCE = 3
    OVERRIDE = 4


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    source: ConfigurationSource
    origin: str
    loaded_at: datetime = field(default_factory=utc_now)
    priority: int = 0


@dataclass(frozen=True, slots=True)
class SecretReference:
    provider: str
    key: str
    version: str | None = None

    def __str__(self) -> str:
        return f"SecretReference(provider={self.provider!r}, key='[REDACTED]')"

    __repr__ = __str__

    def to_dict(self) -> dict[str, str | None]:
        return {"provider": self.provider, "key": "[REDACTED]", "version": self.version}


@dataclass(frozen=True, slots=True)
class SensitiveValue:
    value: Any

    def reveal(self) -> Any:
        return self.value

    def __str__(self) -> str:
        return "[REDACTED]"

    __repr__ = __str__

    def to_dict(self) -> str:
        return "[REDACTED]"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _redact(value: Any, key: str = "") -> Any:
    if isinstance(value, (SensitiveValue, SecretReference)):
        return value.to_dict()
    if _sensitive(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_redact(v, key) for v in value]
    return value


SENSITIVE = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "authorization",
    "cookie",
)


def _sensitive(key: str, extras: tuple[str, ...] = ()) -> bool:
    return any(item.lower() in key.lower() for item in (*SENSITIVE, *extras))


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    values: Mapping[str, Any]
    sources: Mapping[str, SourceMetadata]
    version: int
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _freeze(self.values))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))

    def get(
        self, key: str, default: T | None = None, *, expected_type: type[T] | None = None
    ) -> T | Any | None:
        current: Any = self.values
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        if expected_type is not None and not isinstance(current, expected_type):
            raise ConfigurationError("Configuration value has invalid type", {"key": key})
        return current

    def require(self, key: str, *, expected_type: type[T] | None = None) -> T | Any:
        marker = object()
        value = self.get(key, marker, expected_type=expected_type)
        if value is marker:
            raise ConfigurationError("Missing configuration key", {"key": key})
        return value

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _redact(self.values))


def parse_value(value: str, key: str = "configuration") -> JsonValue | timedelta:
    raw = value.strip()
    lower = raw.lower()
    if lower in {"null", "none"}:
        return None
    if lower in {"true", "yes", "on", "1"}:
        return True
    if lower in {"false", "no", "off", "0"}:
        return False
    match = re.fullmatch(r"(\d+)([smhd])", lower)
    if match:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match[2]]
        return timedelta(**cast(dict[str, float], {unit: float(match[1])}))
    if raw.startswith("{") or raw.startswith("["):
        try:
            return cast(JsonValue, json.loads(raw))
        except json.JSONDecodeError as error:
            raise ConfigurationError("Invalid JSON configuration value", {"key": key}) from error
    if re.fullmatch(r"[-+]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", raw):
        return float(raw)
    return [item.strip() for item in raw.split(",")] if "," in raw else raw


def _nested_from_env(values: Mapping[str, str], prefix: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in values.items():
        if key.upper().startswith(prefix.upper()):
            _put(output, key[len(prefix) :].lower().split("__"), parse_value(value, key))
    return output


def _put(target: dict[str, Any], parts: list[str], value: Any) -> None:
    current = target
    for part in parts[:-1]:
        if part in current and not isinstance(current[part], dict):
            raise ConfigurationError(
                "Conflicting configuration structure", {"key": ".".join(parts)}
            )
        current = current.setdefault(part, {})
    if parts and isinstance(current.get(parts[-1]), dict):
        raise ConfigurationError("Conflicting configuration structure", {"key": ".".join(parts)})
    if parts:
        current[parts[-1]] = value


def _merge(lower: Mapping[str, Any], higher: Mapping[str, Any]) -> dict[str, Any]:
    result = {str(k): v for k, v in lower.items()}
    for key, value in higher.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[str(key)] = _merge(result[str(key)], value)
        else:
            result[str(key)] = value
    return result


def _leaf_sources(
    values: Mapping[str, Any], meta: SourceMetadata, prefix: str = ""
) -> dict[str, SourceMetadata]:
    result = {}
    for key, value in values.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        result.update(
            _leaf_sources(value, meta, path) if isinstance(value, Mapping) else {path: meta}
        )
    return result


class ConfigurationLoader:
    def __init__(self, prefix: str = "ALIOS_", *, sensitive_patterns: tuple[str, ...] = ()) -> None:
        self.prefix = prefix
        self._version = 0
        self._lock = threading.Lock()
        self._patterns = sensitive_patterns

    def add_sensitive_pattern(self, pattern: str) -> None:
        self._patterns = (*self._patterns, pattern)

    def load(
        self,
        *,
        defaults: Mapping[str, Any] | None = None,
        file_data: Mapping[str, Any] | None = None,
        environment: Mapping[str, str] | None = None,
        secret_references: Mapping[str, Any] | None = None,
        overrides: Mapping[str, Any] | None = None,
        validators: tuple[Callable[[Mapping[str, Any]], None], ...] = (),
    ) -> SettingsSnapshot:
        layers = (
            (defaults, ConfigurationSource.DEFAULT, "defaults"),
            (file_data, ConfigurationSource.FILE, "file"),
            (
                _nested_from_env(environment or environ, self.prefix),
                ConfigurationSource.ENVIRONMENT,
                "environment",
            ),
            (secret_references, ConfigurationSource.SECRET_REFERENCE, "secrets"),
            (overrides, ConfigurationSource.OVERRIDE, "overrides"),
        )
        merged: dict[str, Any] = {}
        sources: dict[str, SourceMetadata] = {}
        for data, source, origin in layers:
            if data:
                merged = _merge(merged, data)
                sources.update(
                    _leaf_sources(data, SourceMetadata(source, origin, priority=int(source)))
                )
        for validator in validators:
            try:
                validator(_freeze(merged))
            except ConfigurationError:
                raise
            except Exception as error:
                raise ConfigurationError("Configuration validation failed", cause=error) from error
        with self._lock:
            self._version += 1
            version = self._version
        return SettingsSnapshot(merged, sources, version)

    def from_environment(self, values: Mapping[str, str] | None = None) -> SettingsSnapshot:
        return self.load(environment=values)


@dataclass(frozen=True, slots=True)
class AliOSConfig:
    environment: str = "development"
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", Environment.parse(self.environment).value)
        level = self.log_level.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("Invalid log level", {"log_level": level})
        object.__setattr__(self, "log_level", level)

    @classmethod
    def from_environment(cls) -> AliOSConfig:
        return cls(
            environ.get("ALIOS_ENVIRONMENT", "development"), environ.get("ALIOS_LOG_LEVEL", "INFO")
        )

    def snapshot(self) -> SettingsSnapshot:
        return ConfigurationLoader().load(
            overrides={"environment": self.environment, "log_level": self.log_level}
        )

    def to_dict(self) -> dict[str, str]:
        return {"environment": self.environment, "log_level": self.log_level}
