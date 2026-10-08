"""Server-owned webhook integration persistence boundary."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Organization, WebhookIntegration


class WebhookIntegrationRequiresDemoOrganization(ValueError):
    """Only synthetic demo organizations may receive an integration."""


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
