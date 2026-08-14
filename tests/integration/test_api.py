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


def test_a_database_without_wal_is_a_503_with_the_reason(tmp_path, monkeypatch):
    """Codex r6: the WAL refusal reaches API callers as an operational
    unavailability carrying its guidance, not a generic 500."""
    from adapters.storage import sqlite_conn

    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))
    migrate(tmp_path / "open-career.sqlite3")
    monkeypatch.setattr(sqlite_conn, "apply_pragmas", lambda _conn: "delete")
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert "WAL journal mode did not take effect" in response.json()["detail"]
