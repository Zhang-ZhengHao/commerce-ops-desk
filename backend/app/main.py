"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
from app.services.maintenance import (
    MaintenanceCycle,
    MaintenanceWait,
    run_maintenance_cycle,
    run_maintenance_scheduler,
    wait_for_maintenance_stop,
)
from app.version import APP_VERSION

DEFAULT_STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"
LOGGER = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    static_dir: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    token_factory: TokenFactory | None = None,
    maintenance_cycle: MaintenanceCycle | None = None,
    maintenance_wait: MaintenanceWait | None = None,
) -> FastAPI:
    """Create an isolated application instance for runtime or tests."""
    resolved_settings = settings or Settings()
    engine = build_engine(resolved_settings)
    session_factory = build_session_factory(engine)
    resolved_clock = clock or (lambda: datetime.now(UTC))
    resolved_maintenance_cycle = maintenance_cycle or run_maintenance_cycle
    resolved_maintenance_wait = maintenance_wait or wait_for_maintenance_stop
    hardened_environment = resolved_settings.environment in {"demo", "production"}
    maintenance_enabled = hardened_environment and bool(
        resolved_settings.demo_mode or resolved_settings.webhook_enabled
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop_event: asyncio.Event | None = None
        maintenance_task: asyncio.Task[None] | None = None
        if maintenance_enabled:
            stop_event = asyncio.Event()
            maintenance_task = asyncio.create_task(
                run_maintenance_scheduler(
                    session_factory=session_factory,
                    clock=resolved_clock,
                    cycle=resolved_maintenance_cycle,
                    stop_event=stop_event,
                    wait=resolved_maintenance_wait,
                ),
                name="commerce-ops-maintenance",
            )
        try:
            yield
        finally:
            try:
                if stop_event is not None and maintenance_task is not None:
                    stop_event.set()
                    try:
                        await asyncio.wait_for(
                            maintenance_task,
                            timeout=resolved_settings.maintenance_shutdown_timeout_seconds,
                        )
                    except TimeoutError:
                        # wait_for cancels the scheduler coroutine. A synchronous cycle
                        # already dispatched through asyncio.to_thread cannot be killed;
                        # its Session context retains and closes its checked-out database
                        # resources when that cycle eventually returns.
                        LOGGER.warning(
                            "maintenance_shutdown_timed_out",
                            extra={
                                "timeout_seconds": (
                                    resolved_settings.maintenance_shutdown_timeout_seconds
                                )
                            },
                        )
            finally:
                engine.dispose()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url=None if hardened_environment else "/docs",
        redoc_url=None if hardened_environment else "/redoc",
        openapi_url=None if hardened_environment else "/openapi.json",
    )
    application.state.settings = resolved_settings
    application.state.database_engine = engine
    application.state.session_factory = session_factory
    application.state.clock = resolved_clock
    application.state.token_factory = token_factory or default_token_factory
    if resolved_settings.allowed_hosts:
        application.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=resolved_settings.allowed_hosts,
            www_redirect=False,
        )
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
    if resolved_settings.webhook_enabled:
        from app.api.webhooks import router as webhooks_router

        application.include_router(webhooks_router)
    if resolved_settings.demo_mode:
        from app.api.demo import router as demo_router

        application.include_router(demo_router)
    if (
        resolved_settings.environment != "production"
        and resolved_settings.demo_mode
        and resolved_settings.webhook_enabled
    ):
        from app.api.webhook_demo import router as webhook_demo_router

        application.include_router(webhook_demo_router)

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
            if hardened_environment and (
                client_path == "docs"
                or client_path.startswith("docs/")
                or client_path == "redoc"
                or client_path.startswith("redoc/")
                or client_path == "openapi.json"
            ):
                raise HTTPException(status_code=404)
            return FileResponse(index_file)

    return application
