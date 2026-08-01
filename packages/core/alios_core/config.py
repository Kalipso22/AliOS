"""Validated configuration contracts."""
from dataclasses import dataclass
from os import getenv


@dataclass(frozen=True, slots=True)
class AliOSConfig:
    """Minimal Sprint 0 runtime configuration."""

    environment: str = "development"
    log_level: str = "INFO"

    @classmethod
    def from_environment(cls) -> "AliOSConfig":
        """Load non-secret runtime settings from the environment."""
        return cls(
            environment=getenv("ALIOS_ENVIRONMENT", "development"),
            log_level=getenv("ALIOS_LOG_LEVEL", "INFO").upper(),
        )
