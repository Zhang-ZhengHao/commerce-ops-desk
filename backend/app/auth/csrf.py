"""Same-origin and synchronizer-token checks for cookie-authenticated writes."""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from app.auth.session import AuthContext
from app.config import Settings

CSRF_HEADER_NAME = "X-CSRF-Token"


def derive_csrf_token(settings: Settings, raw_session_token: str) -> str:
    """Derive a stable token without storing CSRF material in the database."""
    if settings.session_secret is None:
        raise RuntimeError("session secret is unavailable")
    key = settings.session_secret.get_secret_value().encode()
    digest = hmac.new(
        key,
        f"commerce-ops:csrf:{raw_session_token}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _authority(host: str, scheme: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(f"//{host}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return hostname.rstrip(".").lower(), port or (443 if scheme == "https" else 80)


def require_same_origin(request: Request) -> None:
    """Reject missing, malformed, or Host-mismatched browser Origin headers."""
    origin_value = request.headers.get("origin")
    host_value = request.headers.get("host")
    if not origin_value or not host_value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin rejected")

    try:
        origin = urlsplit(origin_value)
        origin_port = origin.port
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origin rejected",
        ) from None

    if (
        origin.scheme not in {"http", "https"}
        or origin.hostname is None
        or origin.username is not None
        or origin.password is not None
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin rejected")

    origin_authority = (
        origin.hostname.rstrip(".").lower(),
        origin_port or (443 if origin.scheme == "https" else 80),
    )
    if _authority(host_value, origin.scheme) != origin_authority:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin rejected")


def require_csrf(request: Request, context: AuthContext, settings: Settings) -> None:
    supplied_token = request.headers.get(CSRF_HEADER_NAME)
    expected_token = derive_csrf_token(settings, context.raw_token)
    if supplied_token is None or not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF rejected")
