"""Public write API for `platform` (architecture.md §1.2).

Audit, integrations, reports and analytics.

This is the ONLY module another app may import to mutate `platform`-owned rows.

`record_event` is the audit entry point architecture.md §1.3 mandates: "every sensitive
service mutation calls `platform.services.record_event` (not model signals alone)". Domain
services call it directly. `identity` cannot — the DAG forbids `identity -> platform` — so it
emits signals that `apps/platform/receivers.py` turns into calls to this function.
"""
import logging

from .models import AuditEvent

logger = logging.getLogger(__name__)


def record_event(
    *,
    action: str,
    entity_type: str,
    entity_id=None,
    actor=None,
    old_values: dict | None = None,
    new_values: dict | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditEvent | None:
    """Write one append-only audit row (SRS 5.3, SRS 3.17.3).

    `platform_audit_event` has UPDATE and DELETE revoked at the database-role level, so what
    lands here is permanent by construction.

    **Never raises.** An audit failure must not roll back the business action that triggered
    it — refusing a login because its audit row would not write is worse than the missing
    row. The failure is logged at ERROR so it is still visible, and returns None.
    """
    try:
        return AuditEvent.objects.create(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_user=actor if (actor is not None and actor.is_authenticated) else None,
            old_values=old_values,
            new_values=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:  # noqa: BLE001 - deliberately broad; see the docstring
        logger.exception(
            "Failed to record audit event action=%s entity=%s/%s",
            action,
            entity_type,
            entity_id,
        )
        return None
