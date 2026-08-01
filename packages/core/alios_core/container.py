"""Explicit asynchronous dependency container."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, TypeVar

from .errors import DependencyError

T = TypeVar("T")
Factory = Callable[["Container"], T | object]


class ServiceLifetime(StrEnum):
    INSTANCE = "instance"
    SINGLETON = "singleton"
    SCOPED = "scoped"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class ServiceKey(Generic[T]):
    service_type: type[T]
    name: str | None = None


@dataclass(slots=True)
class _Registration(Generic[T]):
    lifetime: ServiceLifetime
    factory: Factory[T] | None = None
    instance: T | None = None
    owned: bool = False


class Container:
    def __init__(self, parent: Container | None = None) -> None:
        self._parent = parent
        self._registrations: dict[ServiceKey[Any], _Registration[Any]] = {}
        self._instances: dict[ServiceKey[Any], object] = {}
        self._owned: list[object] = []
        self._locks: dict[ServiceKey[Any], asyncio.Lock] = {}
        self._closed = False
        self._resolving: list[ServiceKey[Any]] = []

    def _key(self, service_type: type[T], name: str | None) -> ServiceKey[T]:
        return ServiceKey(service_type, name)

    def _register(
        self, key: ServiceKey[T], registration: _Registration[T], override: bool = False
    ) -> None:
        if self._closed:
            raise DependencyError("Container is closed")
        if key in self._registrations and not override:
            raise DependencyError("Duplicate registration", {"service": key.service_type.__name__})
        self._registrations[key] = registration

    def register_instance(
        self, service_type: type[T], instance: T, name: str | None = None, owned: bool = False
    ) -> None:
        self._register(
            self._key(service_type, name),
            _Registration(ServiceLifetime.INSTANCE, instance=instance, owned=owned),
        )

    def register_singleton(
        self, service_type: type[T], factory: Factory[T], name: str | None = None
    ) -> None:
        self._register(
            self._key(service_type, name), _Registration(ServiceLifetime.SINGLETON, factory=factory)
        )

    def register_factory(
        self,
        service_type: type[T],
        factory: Factory[T],
        name: str | None = None,
        lifetime: ServiceLifetime = ServiceLifetime.TRANSIENT,
    ) -> None:
        self._register(self._key(service_type, name), _Registration(lifetime, factory=factory))

    def register_async_factory(
        self,
        service_type: type[T],
        factory: Factory[T],
        name: str | None = None,
        lifetime: ServiceLifetime = ServiceLifetime.TRANSIENT,
    ) -> None:
        self.register_factory(service_type, factory, name, lifetime)

    def override(self, service_type: type[T], factory: Factory[T], name: str | None = None) -> None:
        self._register(
            self._key(service_type, name),
            _Registration(ServiceLifetime.TRANSIENT, factory=factory),
            True,
        )

    def create_scope(self) -> Container:
        return Container(self)

    async def resolve(self, service_type: type[T], name: str | None = None) -> T:
        if self._closed:
            raise DependencyError("Container is closed")
        key = self._key(service_type, name)
        owner = self if key in self._registrations else self._parent
        if owner is None:
            raise DependencyError("Missing service", {"service": service_type.__name__})
        registration = owner._registrations[key]
        cache = (
            owner._instances
            if registration.lifetime in {ServiceLifetime.SINGLETON, ServiceLifetime.INSTANCE}
            else self._instances
        )
        if registration.lifetime != ServiceLifetime.TRANSIENT and key in cache:
            return cache[key]  # type: ignore[return-value]
        lock = owner._locks.setdefault(key, asyncio.Lock())
        if key in self._resolving:
            raise DependencyError(
                "Circular dependency",
                {
                    "chain": " -> ".join(
                        item.service_type.__name__ for item in [*self._resolving, key]
                    )
                },
            )
        async with lock:
            if registration.lifetime != ServiceLifetime.TRANSIENT and key in cache:
                return cache[key]  # type: ignore[return-value]
            self._resolving.append(key)
            try:
                if registration.lifetime == ServiceLifetime.INSTANCE:
                    value = registration.instance
                elif registration.factory is None:
                    raise DependencyError("Missing factory")
                else:
                    value = registration.factory(self)
                if inspect.isawaitable(value):
                    value = await value
            except Exception as exc:
                raise DependencyError("Factory failed", cause=exc) from exc
            finally:
                self._resolving.pop()
            if registration.lifetime != ServiceLifetime.TRANSIENT:
                cache[key] = value
            if registration.owned or registration.lifetime != ServiceLifetime.INSTANCE:
                self._owned.append(value)
            return value  # type: ignore[return-value]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors = []
        for resource in reversed(self._owned):
            for method in ("aclose", "close", "stop"):
                callback = getattr(resource, method, None)
                if callback:
                    try:
                        result = callback()
                        if inspect.isawaitable(result):
                            await result
                    except Exception as exc:
                        errors.append(exc)
                    break
        if errors:
            raise DependencyError("Resource cleanup failed", {"count": len(errors)}, errors[0])

    async def __aenter__(self) -> Container:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
