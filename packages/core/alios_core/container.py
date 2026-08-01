"""Runtime dependency container."""
from collections.abc import Callable
from typing import Any


class Container:
    """Explicit dependency registry with lazy singleton factories."""

    def __init__(self) -> None:
        self._services: dict[type[Any], Any] = {}
        self._factories: dict[type[Any], Callable[[], Any]] = {}

    def register_instance(self, key: type[Any], instance: Any) -> None:
        self._services[key] = instance

    def register_factory(self, key: type[Any], factory: Callable[[], Any]) -> None:
        self._factories[key] = factory

    def resolve(self, key: type[Any]) -> Any:
        if key not in self._services:
            factory = self._factories.get(key)
            if factory is None:
                raise KeyError(f"No service registered for {key.__name__}")
            self._services[key] = factory()
        return self._services[key]
