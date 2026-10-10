"""Liveness and dependency-readiness endpoints."""

from typing import Literal, cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.database import probe_database
from app.version import APP_VERSION, SERVICE_ID

router = APIRouter(tags=["system"])


class HealthPayload(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["commerce-ops-desk"] = SERVICE_ID


class BuildPayload(BaseModel):
    service: Literal["commerce-ops-desk"] = SERVICE_ID
    version: Literal["0.2.1"] = APP_VERSION
    source_sha: str | None


class ReadinessPayload(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["reachable", "unreachable"]


@router.get("/health", response_model=HealthPayload)
def health() -> HealthPayload:
    """Report process liveness without depending on external services."""
    return HealthPayload()


@router.get("/api/build", response_model=BuildPayload)
def build_metadata(request: Request, response: Response) -> BuildPayload:
    """Expose only the immutable public build identity without allowing caches."""
    settings = cast(Settings, request.app.state.settings)
    response.headers["Cache-Control"] = "no-store"
    return BuildPayload(source_sha=settings.source_sha)


@router.get(
    "/ready",
    response_model=ReadinessPayload,
    responses={503: {"model": ReadinessPayload, "description": "Database is unavailable"}},
)
def ready(request: Request) -> ReadinessPayload | JSONResponse:
    """Report whether this process can execute a database query."""
    engine = cast(Engine, request.app.state.database_engine)
    try:
        probe_database(engine)
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unreachable"},
        )

    return ReadinessPayload(status="ready", database="reachable")
