from fastapi.testclient import TestClient

from adapters.storage.migrations import migrate
from apps.api.main import app


def test_health_responds(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))
    migrate(tmp_path / "open-career.sqlite3")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "edges": 0}


def test_health_before_init_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 503
