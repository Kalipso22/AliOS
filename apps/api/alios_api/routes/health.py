"""Health and readiness API routes."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Return process liveness without requiring dependencies."""
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> dict[str, str]:
    """Return readiness after the AliOS runtime has started."""
    runtime = request.app.state.runtime
    return {"status": "ready" if runtime.is_running else "starting"}
