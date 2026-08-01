import pytest
from alios_core.types import RunOutcome
from alios_runtime import Runtime


@pytest.mark.asyncio
async def test_runtime_executes_async_task() -> None:
    runtime = Runtime()
    await runtime.start()
    result = await runtime.execute(lambda _: "done")
    await runtime.stop()
    await runtime.close()
    assert result.outcome is RunOutcome.SUCCEEDED and result.value == "done"
