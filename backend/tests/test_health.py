from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.version import APP_VERSION, SERVICE_ID

VALID_SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


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


def test_build_metadata_returns_the_exact_uncached_local_identity(tmp_path: Path) -> None:
    database_path = tmp_path / "build.sqlite3"
    app = create_test_app(f"sqlite+pysqlite:///{database_path}")

    with TestClient(app) as client:
        response = client.get("/api/build")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "service": SERVICE_ID,
        "version": APP_VERSION,
        "source_sha": None,
    }
    assert app.version == APP_VERSION


def test_build_metadata_returns_the_configured_full_source_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "identified-build.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+pysqlite:///{database_path}",
        source_sha=VALID_SOURCE_SHA,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/api/build")

    assert response.status_code == 200
    assert set(response.json()) == {"service", "version", "source_sha"}
    assert response.json()["source_sha"] == VALID_SOURCE_SHA
    assert response.headers["cache-control"] == "no-store"


def test_hardened_demo_exposes_build_identity_but_not_api_docs(tmp_path: Path) -> None:
    database_path = tmp_path / "hardened-build.sqlite3"
    settings = Settings(
        _env_file=None,
        environment="demo",
        database_url=f"sqlite+pysqlite:///{database_path}",
        session_secret="demo-secret-that-is-long-enough-for-hmac-only",
        allowed_hosts=("commerceops.test",),
        source_sha=VALID_SOURCE_SHA,
    )
    app = create_app(settings)

    with TestClient(app, base_url="http://commerceops.test") as client:
        build = client.get("/api/build")
        docs = client.get("/docs")
        redoc = client.get("/redoc")
        openapi = client.get("/openapi.json")

    assert build.status_code == 200
    assert build.json()["source_sha"] == VALID_SOURCE_SHA
    assert docs.status_code == 404
    assert redoc.status_code == 404
    assert openapi.status_code == 404


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
