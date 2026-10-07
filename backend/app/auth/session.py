"""Opaque cookie session creation and lookup."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DemoSession, Membership, Organization, User

SESSION_COOKIE_NAME = "commerce_ops_session"
SESSION_COOKIE_PATH = "/"
MAX_SESSION_TOKEN_LENGTH = 512
TokenFactory = Callable[[], str]


@dataclass(frozen=True)
class AuthContext:
    """Authenticated tenant context assembled from authoritative rows."""

    session: DemoSession
    membership: Membership
    user: User
    organization: Organization
    raw_token: str


def default_token_factory() -> str:
    """Return a browser token with 384 bits of operating-system entropy."""
    return secrets.token_urlsafe(48)


def keyed_digest(settings: Settings, purpose: str, value: str) -> str:
    """Create a purpose-separated HMAC digest for sensitive lookup material."""
    if settings.session_secret is None:  # Defensive; Settings resolves this normally.
        raise RuntimeError("session secret is unavailable")
    key = settings.session_secret.get_secret_value().encode()
    message = f"commerce-ops:{purpose}:{value}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def hash_session_token(settings: Settings, raw_token: str) -> str:
    return keyed_digest(settings, "session", raw_token)


def derive_bootstrap_session_token(
    settings: Settings,
    *,
    source_digest: str,
    idempotency_key: str,
    payload_digest: str,
) -> str:
    """Derive the retry-stable opaque token for one bootstrap command."""
    if settings.session_secret is None:
        raise RuntimeError("session secret is unavailable")
    message = json.dumps(
        [
            "commerce-ops:bootstrap-session:v1",
            source_digest,
            idempotency_key,
            payload_digest,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    digest = hmac.new(
        settings.session_secret.get_secret_value().encode(),
        message,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def derive_rotated_session_token(
    settings: Settings,
    *,
    previous_token: str,
    command_type: str,
    idempotency_key: str,
    payload_digest: str,
) -> str:
    """Derive one replayable opaque token from a random authenticated token."""
    if settings.session_secret is None:
        raise RuntimeError("session secret is unavailable")
    message = json.dumps(
        [
            "commerce-ops:session-rotation:v1",
            previous_token,
            command_type,
            idempotency_key,
            payload_digest,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    digest = hmac.new(
        settings.session_secret.get_secret_value().encode(),
        message,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive round-trip while keeping UTC authoritative."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def create_session(
    db: Session,
    *,
    organization_id: str,
    membership_id: str,
    expires_at: datetime,
    now: datetime,
    settings: Settings,
    token_factory: TokenFactory,
) -> tuple[DemoSession, str]:
    raw_token = token_factory()
    if not raw_token or len(raw_token) > MAX_SESSION_TOKEN_LENGTH:
        raise RuntimeError("session token factory returned an invalid token")

    record = DemoSession(
        id=str(uuid4()),
        organization_id=organization_id,
        membership_id=membership_id,
        token_hash=hash_session_token(settings, raw_token),
        created_at=now,
        expires_at=expires_at,
        revoked_at=None,
        replaced_by_session_id=None,
    )
    db.add(record)
    db.flush()
    return record, raw_token


def load_auth_context(
    db: Session,
    *,
    raw_token: str,
    settings: Settings,
    now: datetime,
    allow_revoked: bool = False,
) -> AuthContext | None:
    """Resolve a cookie without ever persisting or logging its plaintext value."""
    if not raw_token or len(raw_token) > MAX_SESSION_TOKEN_LENGTH:
        return None

    statement = (
        select(DemoSession, Membership, User, Organization)
        .join(
            Membership,
            and_(
                Membership.id == DemoSession.membership_id,
                Membership.organization_id == DemoSession.organization_id,
            ),
        )
        .join(
            User,
            and_(
                User.id == Membership.user_id,
                User.organization_id == Membership.organization_id,
            ),
        )
        .join(Organization, Organization.id == DemoSession.organization_id)
        .where(DemoSession.token_hash == hash_session_token(settings, raw_token))
    )
    row = db.execute(statement).one_or_none()
    if row is None:
        return None

    session_record, membership, user, organization = row
    current_time = as_utc(now)
    if as_utc(session_record.expires_at) <= current_time:
        return None
    if as_utc(organization.expires_at) <= current_time:
        return None
    if session_record.revoked_at is not None and not allow_revoked:
        return None

    return AuthContext(
        session=session_record,
        membership=membership,
        user=user,
        organization=organization,
        raw_token=raw_token,
    )
