"""Server-owned webhook integration persistence boundary."""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Organization, WebhookIntegration


class WebhookIntegrationRequiresDemoOrganization(ValueError):
    """Only synthetic demo organizations may receive an integration."""


@dataclass(frozen=True)
class ActiveDemoWebhookTarget:
    organization_id: str
    integration_id: str
    key_version: int


def find_active_demo_webhook_target(
    db: Session,
    *,
    integration_id: str,
    now: datetime,
) -> ActiveDemoWebhookTarget | None:
    """Return the minimal signing target for an active synthetic demo integration."""

    row = db.execute(
        select(
            WebhookIntegration.organization_id,
            WebhookIntegration.id,
            WebhookIntegration.key_version,
        )
        .join(
            Organization,
            Organization.id == WebhookIntegration.organization_id,
        )
        .where(
            WebhookIntegration.id == integration_id,
            WebhookIntegration.provider == "synthetic",
            WebhookIntegration.enabled.is_(True),
            Organization.is_demo.is_(True),
            Organization.expires_at > now,
        )
    ).one_or_none()
    if row is None:
        return None
    organization_id, resolved_integration_id, key_version = row
    return ActiveDemoWebhookTarget(
        organization_id=organization_id,
        integration_id=resolved_integration_id,
        key_version=key_version,
    )


def create_demo_webhook_integration(
    db: Session,
    *,
    organization: Organization,
    now: datetime,
) -> WebhookIntegration:
    if not organization.is_demo:
        raise WebhookIntegrationRequiresDemoOrganization

    integration = WebhookIntegration(
        id=str(uuid4()),
        organization_id=organization.id,
        provider="synthetic",
        key_version=1,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(integration)
    db.flush()
    return integration
