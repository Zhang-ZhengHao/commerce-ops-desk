from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def create_test_app(database_url: str) -> FastAPI:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
    )
    return create_app(settings)


def test_health_reports_process_health_without_touching_the_database(tmp_path: Path) -> None:
    unavailable_database = tmp_path / "missing-parent" / "health.sqlite3"
    app = create_test_app(f"sqlite+pysqlite:///{unavailable_database}")

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "commerce-ops-desk"}


def test_ready_reports_a_reachable_database(tmp_path: Path) -> None:
    database_path = tmp_path / "ready.sqlite3"
    app = create_test_app(f"sqlite+pysqlite:///{database_path}")

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "reachable"}


def test_ready_fails_closed_without_leaking_database_details(tmp_path: Path) -> None:
    unavailable_database = tmp_path / "missing-parent" / "ready.sqlite3"
    app = create_test_app(f"sqlite+pysqlite:///{unavailable_database}")

    with TestClient(app) as client:
        response = client.get("/ready")

    response_body = response.text
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unreachable"}
    assert str(unavailable_database) not in response_body
