from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_static_build_and_client_routes_share_the_api_origin(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<!doctype html><title>CommerceOps Desk</title><main>entry shell</main>",
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("window.__commerceOps = true;", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'static.sqlite3'}",
    )

    app = create_app(settings, static_dir=static_dir)

    with TestClient(app) as client:
        root_response = client.get("/")
        client_route_response = client.get("/cases/demo-case")
        asset_response = client.get("/assets/app.js")

    assert root_response.status_code == 200
    assert "CommerceOps Desk" in root_response.text
    assert client_route_response.status_code == 200
    assert "entry shell" in client_route_response.text
    assert asset_response.status_code == 200
    assert asset_response.text == "window.__commerceOps = true;"


def test_unknown_api_route_never_falls_back_to_the_spa(tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><title>CommerceOps Desk</title>",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'api.sqlite3'}",
    )

    app = create_app(settings, static_dir=static_dir)

    with TestClient(app) as client:
        response = client.get("/api/not-a-real-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


def test_hardened_app_accepts_only_explicit_public_and_health_hosts(
    tmp_path: Path,
) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><main>entry shell</main>",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'host.sqlite3'}",
        session_secret="production-secret-that-is-long-enough-for-hmac",
        allowed_hosts=(
            "commerce-ops-desk.srrsh.aig.rest",
            "127.0.0.1",
            "localhost",
        ),
    )
    app = create_app(settings, static_dir=static_dir)

    with TestClient(
        app,
        base_url="https://commerce-ops-desk.srrsh.aig.rest",
    ) as client:
        public_response = client.get("/")
        rejected_response = client.get("/", headers={"Host": "evil.example"})
        local_health_response = client.get(
            "http://127.0.0.1:8000/health",
        )
        localhost_health_response = client.get(
            "http://localhost:8000/health",
        )

    assert public_response.status_code == 200
    assert rejected_response.status_code == 400
    assert local_health_response.status_code == 200
    assert localhost_health_response.status_code == 200


def test_hardened_host_rejection_never_redirects_to_www(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'www.sqlite3'}",
        session_secret="production-secret-that-is-long-enough-for-hmac",
        allowed_hosts=("www.example.com",),
    )
    app = create_app(settings)

    with TestClient(app, base_url="https://example.com") as client:
        response = client.get("/health", follow_redirects=False)

    assert response.status_code == 400
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "path",
    [
        "/docs",
        "/docs/",
        "/docs/anything",
        "/redoc",
        "/redoc/anything",
        "/openapi.json",
    ],
)
def test_hardened_app_never_exposes_docs_or_spa_fallback(
    tmp_path: Path,
    path: str,
) -> None:
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><main>entry shell</main>",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'docs.sqlite3'}",
        session_secret="production-secret-that-is-long-enough-for-hmac",
        allowed_hosts=("commerce-ops-desk.srrsh.aig.rest",),
    )
    app = create_app(settings, static_dir=static_dir)

    with TestClient(
        app,
        base_url="https://commerce-ops-desk.srrsh.aig.rest",
    ) as client:
        response = client.get(path, follow_redirects=False)

    assert response.status_code == 404
    assert "entry shell" not in response.text


def test_development_keeps_interactive_api_documentation(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="development",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'development-docs.sqlite3'}",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        docs_response = client.get("/docs")
        redoc_response = client.get("/redoc")
        schema_response = client.get("/openapi.json")

    assert docs_response.status_code == 200
    assert docs_response.headers["content-type"].startswith("text/html")
    assert redoc_response.status_code == 200
    assert redoc_response.headers["content-type"].startswith("text/html")
    assert schema_response.status_code == 200
    assert schema_response.headers["content-type"].startswith("application/json")
