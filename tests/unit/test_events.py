from alios_core.events import Event, EventBus


def test_event_bus_delivers_subscribed_events() -> None:
    received: list[Event] = []
    bus = EventBus()
    bus.subscribe("runtime.started", received.append)

    event = bus.publish("runtime.started")

    assert received == [event]
    assert event.name == "runtime.started"
