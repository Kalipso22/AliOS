"""Runtime orchestration facade."""
from alios_core.config import AliOSConfig
from alios_core.events import EventBus


class Runtime:
    """Owns the Sprint 0 runtime lifecycle and foundational events."""

    def __init__(self, config: AliOSConfig | None = None) -> None:
        self.config = config or AliOSConfig.from_environment()
        self.events = EventBus()
        self.is_running = False

    def start(self) -> None:
        if not self.is_running:
            self.is_running = True
            self.events.publish("runtime.started")

    def stop(self) -> None:
        if self.is_running:
            self.events.publish("runtime.stopping")
            self.is_running = False
            self.events.publish("runtime.stopped")
