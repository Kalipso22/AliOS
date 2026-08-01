"""Application startup and shutdown lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from alios_runtime.runtime import Runtime
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create and stop the process-local AliOS runtime."""
    runtime = Runtime()
    runtime.start()
    app.state.runtime = runtime
    try:
        yield
    finally:
        runtime.stop()
