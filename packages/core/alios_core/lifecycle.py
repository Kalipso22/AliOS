"""Service lifecycle contracts."""
from typing import Protocol


class LifecycleService(Protocol):
    """Service with explicit startup and shutdown semantics."""

    def start(self) -> None: ...

    def stop(self) -> None: ...
