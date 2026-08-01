from alios_api.main import app
from fastapi.testclient import TestClient


def test_health_and_readiness_endpoints() -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}
