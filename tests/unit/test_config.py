from datetime import timedelta

import pytest
from alios_core.config import (
    AliOSConfig,
    ConfigurationLoader,
    ConfigurationSource,
    Environment,
    SecretReference,
    SensitiveValue,
    parse_value,
)
from alios_core.errors import ConfigurationError


def test_configuration_uses_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOS_ENVIRONMENT", "test")
    monkeypatch.setenv("ALIOS_LOG_LEVEL", "debug")

    config = AliOSConfig.from_environment()

    assert config.environment == "test"
    assert config.log_level == "DEBUG"


@pytest.mark.parametrize("value", ["development", "TEST", "staging", "PRODUCTION"])
def test_environment_parser(value: str) -> None:
    assert Environment.parse(value).value == value.lower()


def test_invalid_environment_rejected() -> None:
    with pytest.raises(ConfigurationError):
        Environment.parse("invalid")


def test_loader_parses_nested_environment_mapping() -> None:
    snapshot = ConfigurationLoader().from_environment(
        {"ALIOS_DATABASE__HOST": "localhost", "ALIOS_RUNTIME__ENABLED": "true"}
    )
    assert snapshot.get("database.host") == "localhost"
    assert snapshot.get("runtime.enabled") is True


def test_sensitive_value_is_redacted() -> None:
    value = SensitiveValue("private")
    assert str(value) == "[REDACTED]"
    assert value.reveal() == "private"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("42", 42),
        ("1.5", 1.5),
        ("30s", 30),
        ("5m", 300),
        ("2h", 7200),
        ("1d", 86400),
        ("a,b", ["a", "b"]),
        ("null", None),
        ('{"a":1}', {"a": 1}),
        ("[1,2]", [1, 2]),
    ],
)
def test_value_parsing(raw: str, expected: object) -> None:
    value = parse_value(raw)
    if isinstance(value, timedelta):
        assert value.total_seconds() == expected
    else:
        assert value == expected


@pytest.mark.parametrize("index", range(25))
def test_loader_versions_are_monotonic(index: int) -> None:
    loader = ConfigurationLoader()
    first = loader.load(overrides={"value": index})
    second = loader.load(overrides={"value": index + 1})
    assert first.version < second.version and second.require("value") == index + 1


def test_loader_precedence_sources_and_redaction() -> None:
    snapshot = ConfigurationLoader().load(
        defaults={"db": {"host": "a", "password": "x"}},
        file_data={"db": {"host": "b"}},
        overrides={"db": {"host": "c"}},
    )
    assert snapshot.require("db.host") == "c"
    assert snapshot.sources["db.host"].source is ConfigurationSource.OVERRIDE
    assert snapshot.to_dict()["db"]["password"] == "[REDACTED]"


def test_secret_reference_is_safe() -> None:
    assert "[REDACTED]" in str(SecretReference("env", "DATABASE_PASSWORD"))
