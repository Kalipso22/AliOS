import asyncio

import pytest
from alios_core.container import Container, ServiceLifetime
from alios_core.errors import DependencyError


class Service:
    pass


class Closeable:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name

    async def aclose(self) -> None:
        self.events.append(self.name)


def test_singleton_scoped_transient_and_named_resolution() -> None:
    async def run() -> None:
        container = Container()
        container.register_singleton(Service, lambda _: Service())
        container.register_factory(str, lambda _: "scoped", lifetime=ServiceLifetime.SCOPED)
        container.register_factory(int, lambda _: object())
        container.register_instance(bytes, b"named", name="primary")
        assert await container.resolve(Service) is await container.resolve(Service)
        async with container.create_scope() as scope:
            assert await scope.resolve(str) is await scope.resolve(str)
            assert await scope.resolve(int) is not await scope.resolve(int)
            assert await scope.resolve(bytes, "primary") == b"named"
        await container.close()

    asyncio.run(run())


def test_duplicate_missing_and_circular_dependencies() -> None:
    async def run() -> None:
        container = Container()
        container.register_instance(str, "one")
        with pytest.raises(DependencyError):
            container.register_instance(str, "two")
        with pytest.raises(DependencyError):
            await container.resolve(int)
        container.register_singleton(int, lambda scope: scope.resolve(float))
        container.register_singleton(float, lambda scope: scope.resolve(int))
        with pytest.raises(DependencyError):
            await container.resolve(int)

    asyncio.run(run())


def test_owned_resources_close_in_reverse_order() -> None:
    async def run() -> None:
        events: list[str] = []
        container = Container()
        container.register_instance(str, Closeable(events, "first"), owned=True)
        container.register_instance(bytes, Closeable(events, "second"), owned=True)
        await container.resolve(str)
        await container.resolve(bytes)
        await container.close()
        assert events == ["second", "first"]

    asyncio.run(run())


def test_scope_isolation_and_closed_scope_rejection() -> None:
    async def run() -> None:
        container = Container()
        container.register_factory(str, lambda _: object(), lifetime=ServiceLifetime.SCOPED)
        scope = container.create_scope()
        first = await scope.resolve(str)
        async with container.create_scope() as second_scope:
            assert first is not await second_scope.resolve(str)
        with pytest.raises(DependencyError):
            await second_scope.resolve(str)
        await container.close()

    asyncio.run(run())


def test_async_factory_and_explicit_override() -> None:
    async def factory(_: Container) -> str:
        return "original"

    async def run() -> None:
        container = Container()
        container.register_async_factory(str, factory, lifetime=ServiceLifetime.SINGLETON)
        assert await container.resolve(str) == "original"
        container.override(str, lambda _: "replacement")
        assert await container.resolve(str) == "replacement"
        await container.close()

    asyncio.run(run())


@pytest.mark.parametrize("lifetime", list(ServiceLifetime))
def test_each_lifetime_resolves(lifetime: ServiceLifetime) -> None:
    async def run() -> None:
        container = Container()
        container.register_factory(str, lambda _: object(), lifetime=lifetime)
        first = await container.resolve(str)
        second = await container.resolve(str)
        if lifetime is ServiceLifetime.TRANSIENT:
            assert first is not second
        else:
            assert first is second
        await container.close()

    asyncio.run(run())


@pytest.mark.parametrize("name", [None, "primary", "secondary", "test", "isolated"])
def test_named_service_keys_are_independent(name: str | None) -> None:
    async def run() -> None:
        container = Container()
        container.register_instance(str, "value", name=name)
        assert await container.resolve(str, name=name) == "value"
        await container.close()

    asyncio.run(run())


@pytest.mark.parametrize("owned", [True, False, True])
def test_instance_ownership_controls_cleanup(owned: bool) -> None:
    async def run() -> None:
        events: list[str] = []
        container = Container()
        resource = Closeable(events, "resource")
        container.register_instance(str, resource, owned=owned)
        await container.resolve(str)
        await container.close()
        assert events == (["resource"] if owned else [])

    asyncio.run(run())
