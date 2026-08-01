import pytest
from alios_core.types import RunOutcome
from alios_runtime import Runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("value", range(50))
async def test_runtime_orchestrates_independent_executions(value: int) -> None:
    runtime = Runtime()
    await runtime.initialize()
    await runtime.start()
    result = await runtime.execute(lambda _: value)
    await runtime.stop()
    await runtime.close()
    assert result.outcome is RunOutcome.SUCCEEDED and result.value == value
