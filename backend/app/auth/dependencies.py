"""FastAPI dependencies for database and authenticated tenant context."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime
from typing import Annotated, Protocol, cast

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session, sessionmaker

from app.auth.session import SESSION_COOKIE_NAME, AuthContext, load_auth_context
from app.config import Settings


def get_database(request: Request) -> Generator[Session, None, None]:
    factory = cast(sessionmaker[Session], request.app.state.session_factory)
    with factory() as database:
        yield database


def get_current_auth(
    request: Request,
    database: Annotated[Session, Depends(get_database)],
) -> AuthContext:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    settings = cast(Settings, request.app.state.settings)
    clock = cast("CallableClock", request.app.state.clock)
    context = load_auth_context(
        database,
        raw_token=raw_token,
        settings=settings,
        now=clock(),
    )
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return context


class CallableClock(Protocol):
    def __call__(self) -> datetime: ...
