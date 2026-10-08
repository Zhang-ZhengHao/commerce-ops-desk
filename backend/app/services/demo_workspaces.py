"""Tenant creation, persistent limits, role lookup, and expiry cleanup."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Any, Protocol, cast
from uuid import uuid4

from fastapi import Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.auth.session import TokenFactory, create_session, keyed_digest
from app.config import Settings
from app.models import (
    CommandReceipt,
    DemoSession,
    Membership,
    Organization,
    RateLimit,
    Role,
    User,
)
from app.repositories.webhook_integrations import create_demo_webhook_integration
from app.services.seeding import seed_demo_order_cases

GLOBAL_CAPACITY_LOCK_DIGEST = hashlib.sha256(b"commerce-ops:demo-capacity-lock:v1").hexdigest()


class DemoRateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("demo workspace source limit exceeded")
        self.retry_after = retry_after


class DemoCapacityExceeded(Exception):
    """The configured number of active public workspaces already exists."""


class DemoRoleWriteLimitExceeded(Exception):
    """The workspace has consumed its bounded role-change history."""


@dataclass(frozen=True)
class CreatedWorkspace:
    organization: Organization
    membership: Membership
    user: User
    session: DemoSession
    raw_session_token: str
    available_roles: tuple[Role, ...]


class ExpiredWorkspaceCleaner(Protocol):
    def cleanup(self, db: Session, *, now: datetime) -> int: ...


class DatabaseExpiredWorkspaceCleaner:
    """Cleanup implementation; scheduling deliberately belongs to I08."""

    def cleanup(self, db: Session, *, now: datetime) -> int:
        result = db.execute(
            delete(Organization).where(
                Organization.is_demo.is_(True),
                Organization.expires_at <= now,
            )
        )
        cursor_result = cast(CursorResult[Any], result)
        return int(cursor_result.rowcount or 0)


def _fixed_hour(now: datetime) -> datetime:
    normalized = now.astimezone(UTC)
    return normalized.replace(minute=0, second=0, microsecond=0)


def _retry_after(now: datetime) -> int:
    window_end = _fixed_hour(now) + timedelta(hours=1)
    return max(1, math.ceil((window_end - now.astimezone(UTC)).total_seconds()))


def _upsert_counter(
    db: Session,
    *,
    source_digest: str,
    window_start: datetime,
    now: datetime,
    maximum: int,
) -> bool:
    values = {
        "source_digest": source_digest,
        "window_start": window_start,
        "count": 1,
        "updated_at": now,
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        sqlite_statement = sqlite_insert(RateLimit).values(**values)
        sqlite_statement = sqlite_statement.on_conflict_do_update(
            index_elements=[RateLimit.source_digest, RateLimit.window_start],
            set_={"count": RateLimit.count + 1, "updated_at": now},
            where=RateLimit.count < maximum,
        )
        result = db.execute(sqlite_statement)
    elif dialect_name == "postgresql":
        postgresql_statement = postgresql_insert(RateLimit).values(**values)
        postgresql_statement = postgresql_statement.on_conflict_do_update(
            index_elements=[RateLimit.source_digest, RateLimit.window_start],
            set_={"count": RateLimit.count + 1, "updated_at": now},
            where=RateLimit.count < maximum,
        )
        persisted_count = db.scalar(postgresql_statement.returning(RateLimit.count))
        return persisted_count is not None
    else:  # Settings currently prevents reaching an unsupported backend.
        raise RuntimeError("unsupported rate-limit database")

    cursor_result = cast(CursorResult[Any], result)
    return bool(cursor_result.rowcount)


def _acquire_capacity_lock(db: Session, *, now: datetime) -> None:
    """Serialize the active-count check across sources and app instances."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    values = {
        "source_digest": GLOBAL_CAPACITY_LOCK_DIGEST,
        "window_start": epoch,
        "count": 0,
        "updated_at": now,
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        sqlite_statement = sqlite_insert(RateLimit).values(**values)
        sqlite_statement = sqlite_statement.on_conflict_do_update(
            index_elements=[RateLimit.source_digest, RateLimit.window_start],
            set_={"updated_at": now},
        )
        db.execute(sqlite_statement)
    elif dialect_name == "postgresql":
        postgresql_statement = postgresql_insert(RateLimit).values(**values)
        postgresql_statement = postgresql_statement.on_conflict_do_update(
            index_elements=[RateLimit.source_digest, RateLimit.window_start],
            set_={"updated_at": now},
        )
        db.execute(postgresql_statement)
    else:
        raise RuntimeError("unsupported capacity database")


def client_source_address(request: Request, settings: Settings) -> str:
    direct_source = request.client.host if request.client is not None else "unknown"
    try:
        direct_ip = ip_address(direct_source)
        normalized_direct = direct_ip.compressed
    except ValueError:
        direct_ip = None
        normalized_direct = direct_source[:255]

    trusted_networks = settings.trusted_proxy_networks
    direct_is_trusted = direct_ip is not None and any(
        direct_ip in network for network in trusted_networks
    )
    if direct_is_trusted:
        forwarded_value = request.headers.get("x-forwarded-for")
        if forwarded_value:
            forwarded_hops = []
            for raw_hop in forwarded_value.split(","):
                try:
                    forwarded_hops.append(ip_address(raw_hop.strip()))
                except ValueError:
                    return normalized_direct

            for forwarded_hop in reversed(forwarded_hops):
                if not any(forwarded_hop in network for network in trusted_networks):
                    return forwarded_hop.compressed
            if forwarded_hops:
                return forwarded_hops[0].compressed
    return normalized_direct


def _create_identity(
    db: Session,
    *,
    organization_id: str,
    display_name: str,
    role: Role,
    now: datetime,
) -> tuple[User, Membership]:
    user = User(
        id=str(uuid4()),
        organization_id=organization_id,
        display_name=display_name,
        created_at=now,
    )
    db.add(user)
    db.flush()
    membership = Membership(
        id=str(uuid4()),
        organization_id=organization_id,
        user_id=user.id,
        role=role,
        created_at=now,
    )
    db.add(membership)
    db.flush()
    return user, membership


def create_demo_workspace(
    db: Session,
    *,
    initial_role: Role,
    source_address: str,
    settings: Settings,
    now: datetime,
    token_factory: TokenFactory,
) -> CreatedWorkspace:
    _acquire_capacity_lock(db, now=now)
    active_count = db.scalar(
        select(func.count(Organization.id)).where(
            Organization.is_demo.is_(True),
            Organization.expires_at > now,
        )
    )
    if int(active_count or 0) >= settings.demo_active_workspace_limit:
        raise DemoCapacityExceeded

    source_digest = keyed_digest(settings, "rate-limit-source", source_address)
    if not _upsert_counter(
        db,
        source_digest=source_digest,
        window_start=_fixed_hour(now),
        now=now,
        maximum=settings.demo_source_hourly_limit,
    ):
        raise DemoRateLimitExceeded(_retry_after(now))

    organization_id = str(uuid4())
    expires_at = now + timedelta(hours=settings.demo_workspace_ttl_hours)
    organization = Organization(
        id=organization_id,
        name=f"Demo workspace {organization_id[:8]}",
        is_demo=True,
        created_at=now,
        expires_at=expires_at,
    )
    db.add(organization)
    db.flush()
    create_demo_webhook_integration(
        db,
        organization=organization,
        now=now,
    )
    manager_user, manager_membership = _create_identity(
        db,
        organization_id=organization_id,
        display_name="Demo Manager",
        role="manager",
        now=now,
    )
    agent_user, agent_membership = _create_identity(
        db,
        organization_id=organization_id,
        display_name="Demo Agent",
        role="agent",
        now=now,
    )
    selected_user, selected_membership = (
        (manager_user, manager_membership)
        if initial_role == "manager"
        else (agent_user, agent_membership)
    )
    seed_demo_order_cases(
        db,
        organization=organization,
        agent_membership=agent_membership,
        now=now,
    )
    db.flush()
    session_record, raw_token = create_session(
        db,
        organization_id=organization_id,
        membership_id=selected_membership.id,
        expires_at=expires_at,
        now=now,
        settings=settings,
        token_factory=token_factory,
    )
    return CreatedWorkspace(
        organization=organization,
        membership=selected_membership,
        user=selected_user,
        session=session_record,
        raw_session_token=raw_token,
        available_roles=("manager", "agent"),
    )


def find_membership_for_role(
    db: Session,
    *,
    organization_id: str,
    role: Role,
) -> tuple[Membership, User] | None:
    row = db.execute(
        select(Membership, User)
        .join(
            User,
            (User.id == Membership.user_id) & (User.organization_id == Membership.organization_id),
        )
        .where(
            Membership.organization_id == organization_id,
            Membership.role == role,
        )
        .order_by(Membership.id)
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    membership, user = row
    return membership, user


def find_membership_by_id(
    db: Session,
    *,
    organization_id: str,
    membership_id: str,
) -> tuple[Membership, User] | None:
    row = db.execute(
        select(Membership, User)
        .join(
            User,
            (User.id == Membership.user_id) & (User.organization_id == Membership.organization_id),
        )
        .where(
            Membership.organization_id == organization_id,
            Membership.id == membership_id,
        )
    ).one_or_none()
    if row is None:
        return None
    membership, user = row
    return membership, user


def find_session_by_id(
    db: Session,
    *,
    organization_id: str,
    session_id: str,
) -> DemoSession | None:
    return db.scalar(
        select(DemoSession).where(
            DemoSession.organization_id == organization_id,
            DemoSession.id == session_id,
        )
    )


def claim_session_rotation(
    db: Session,
    *,
    session: DemoSession,
    now: datetime,
) -> bool:
    result = db.execute(
        update(DemoSession)
        .where(
            DemoSession.id == session.id,
            DemoSession.organization_id == session.organization_id,
            DemoSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    cursor_result = cast(CursorResult[Any], result)
    return cursor_result.rowcount == 1


def lock_workspace_role_writes(db: Session, *, organization_id: str) -> None:
    """Serialize role-write admission for one workspace until transaction end."""
    result = db.execute(
        update(Organization)
        .where(Organization.id == organization_id)
        .values(expires_at=Organization.expires_at)
    )
    cursor_result = cast(CursorResult[Any], result)
    if cursor_result.rowcount != 1:
        raise RuntimeError("workspace disappeared while locking role writes")


def enforce_role_write_limit(
    db: Session,
    *,
    organization_id: str,
    command_type: str,
    maximum: int,
) -> None:
    write_count = db.scalar(
        select(func.count(CommandReceipt.id)).where(
            CommandReceipt.organization_id == organization_id,
            CommandReceipt.command_type == command_type,
        )
    )
    if int(write_count or 0) >= maximum:
        raise DemoRoleWriteLimitExceeded


def link_session_replacement(
    db: Session,
    *,
    session: DemoSession,
    replacement: DemoSession,
) -> None:
    result = db.execute(
        update(DemoSession)
        .where(
            DemoSession.id == session.id,
            DemoSession.organization_id == session.organization_id,
            DemoSession.revoked_at.is_not(None),
            DemoSession.replaced_by_session_id.is_(None),
        )
        .values(replaced_by_session_id=replacement.id)
    )
    cursor_result = cast(CursorResult[Any], result)
    if cursor_result.rowcount != 1:
        raise RuntimeError("session replacement link lost its rotation claim")
