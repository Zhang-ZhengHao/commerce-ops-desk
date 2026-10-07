"""FastAPI application factory."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.audit import router as audit_router
from app.api.cases import router as cases_router
from app.api.dashboard import router as dashboard_router
from app.api.health import router as health_router
from app.auth.session import TokenFactory, default_token_factory
from app.config import Settings
from app.database import build_engine, build_session_factory
from app.request_guard import (
    VALIDATION_FAILED_RESPONSE,
    ApiRequestBodyLimitMiddleware,
)

DEFAULT_STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(
    settings: Settings | None = None,
    *,
    static_dir: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    token_factory: TokenFactory | None = None,
) -> FastAPI:
    """Create an isolated application instance for runtime or tests."""
    resolved_settings = settings or Settings()
    engine = build_engine(resolved_settings)
    session_factory = build_session_factory(engine)

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
    application.state.session_factory = session_factory
    application.state.clock = clock or (lambda: datetime.now(UTC))
    application.state.token_factory = token_factory or default_token_factory
    application.add_middleware(
        ApiRequestBodyLimitMiddleware,
        max_body_bytes=resolved_settings.api_max_request_body_bytes,
    )

    @application.exception_handler(RequestValidationError)
    async def safe_request_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del request, error
        return JSONResponse(VALIDATION_FAILED_RESPONSE, status_code=422)

    application.include_router(health_router)
    application.include_router(dashboard_router)
    application.include_router(cases_router)
    application.include_router(audit_router)
    if resolved_settings.demo_mode:
        from app.api.demo import router as demo_router

        application.include_router(demo_router)

    @application.api_route(
        "/api/{api_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def unknown_api(api_path: str) -> None:
        del api_path
        raise HTTPException(status_code=404)

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
