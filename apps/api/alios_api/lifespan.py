"""Application startup and shutdown lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from alios_runtime.runtime import Runtime
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create and stop the process-local AliOS runtime."""
    runtime = Runtime()
    try:
        await runtime.initialize()
        await runtime.start()
        app.state.runtime = runtime
        yield
    finally:
        try:
            await runtime.stop()
        finally:
            await runtime.close()
