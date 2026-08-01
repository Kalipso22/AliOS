import pytest
from alios_core.config import AliOSConfig


def test_configuration_uses_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOS_ENVIRONMENT", "test")
    monkeypatch.setenv("ALIOS_LOG_LEVEL", "debug")

    config = AliOSConfig.from_environment()

    assert config.environment == "test"
    assert config.log_level == "DEBUG"
