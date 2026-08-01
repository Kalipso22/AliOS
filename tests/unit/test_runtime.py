from alios_runtime.runtime import Runtime


def test_runtime_emits_lifecycle_events() -> None:
    runtime = Runtime()
    events: list[str] = []
    for name in ("runtime.started", "runtime.stopping", "runtime.stopped"):
        runtime.events.subscribe(name, lambda event: events.append(event.name))

    runtime.start()
    runtime.stop()

    assert events == ["runtime.started", "runtime.stopping", "runtime.stopped"]
    assert not runtime.is_running
