"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.config import Settings
from app.database import build_engine

DEFAULT_STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(
    settings: Settings | None = None,
    *,
    static_dir: Path | None = None,
) -> FastAPI:
    """Create an isolated application instance for runtime or tests."""
    resolved_settings = settings or Settings()
    engine = build_engine(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.database_engine = engine
    application.include_router(health_router)

    resolved_static_dir = static_dir or DEFAULT_STATIC_DIR
    index_file = resolved_static_dir / "index.html"
    assets_dir = resolved_static_dir / "assets"
    if index_file.is_file():
        if assets_dir.is_dir():
            application.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @application.get("/{client_path:path}", include_in_schema=False)
        def serve_spa(client_path: str) -> FileResponse:
            if client_path == "api" or client_path.startswith("api/"):
                raise HTTPException(status_code=404)
            return FileResponse(index_file)

    return application
