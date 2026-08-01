"""FastAPI application composition root."""
from fastapi import FastAPI

from alios_api.lifespan import lifespan
from alios_api.routes.health import router as health_router

app = FastAPI(title="AliOS API", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
