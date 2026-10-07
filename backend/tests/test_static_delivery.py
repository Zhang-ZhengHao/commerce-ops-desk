from pathlib import Path

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
