"""Turn `identity` events into audit rows.

SRS 5.3 requires "a full audit trail for sensitive actions (login, data export, permission
changes, document access, GPS track access)". Registration, role changes, deactivation and
login all happen in `identity` — which the import DAG forbids from writing to `platform`.

So `identity` emits signals and this module records them. `platform -> identity` is the legal
direction (the satellite contract bars `platform` from the *domain* services —
contacts/inventory/crm/property_ops/finance — and deliberately does not list `identity`).

Connected in `apps/platform/apps.py::ready()`.
"""
from apps.identity import signals

from .models import AuditEvent
from .services import record_event

USER = "USER"
USER_ROLE = "USER_ROLE"
PORTAL_PROFILE = "PORTAL_PROFILE"


def on_user_registered(sender, *, user, actor, role_code, **kwargs):
    record_event(
        action=AuditEvent.Action.CREATE,
        entity_type=USER,
        entity_id=user.pk,
        actor=actor,
        new_values={"email": user.email, "role_code": role_code},
    )


def on_role_assigned(sender, *, user, actor, role_code, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type=USER_ROLE,
        entity_id=user.pk,
        actor=actor,
        new_values={"granted": role_code},
    )


def on_role_revoked(sender, *, user, actor, role_code, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type=USER_ROLE,
        entity_id=user.pk,
        actor=actor,
        old_values={"revoked": role_code},
    )


def on_user_deactivated(sender, *, user, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type=USER,
        entity_id=user.pk,
        actor=actor,
        old_values={"is_active": True},
        new_values={"is_active": False},
    )


def on_user_reactivated(sender, *, user, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type=USER,
        entity_id=user.pk,
        actor=actor,
        old_values={"is_active": False},
        new_values={"is_active": True},
    )


def on_portal_access_granted(sender, *, user, actor, portal_profile, **kwargs):
    record_event(
        action=AuditEvent.Action.CREATE,
        entity_type=PORTAL_PROFILE,
        entity_id=portal_profile.pk,
        actor=actor,
        new_values={
            "user_id": str(user.pk),
            "portal_type": portal_profile.portal_type,
            "contract_ref_type": portal_profile.completed_contract_ref_type,
            "contract_ref_id": str(portal_profile.completed_contract_ref_id),
        },
    )


def on_portal_access_revoked(sender, *, user, actor, portal_profile, reason="", **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type=PORTAL_PROFILE,
        entity_id=portal_profile.pk,
        actor=actor,
        new_values={"eligibility_status": portal_profile.eligibility_status, "reason": reason},
    )


def on_user_logged_in(sender, *, user, ip_address=None, user_agent=None, **kwargs):
    record_event(
        action=AuditEvent.Action.LOGIN,
        entity_type=USER,
        entity_id=user.pk,
        actor=user,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def on_user_login_failed(sender, *, email, ip_address=None, user_agent=None, **kwargs):
    # No actor: the point of a failed login is that no identity was established. The attempted
    # address is recorded instead, which is what makes credential-stuffing visible.
    record_event(
        action=AuditEvent.Action.LOGIN_FAILED,
        entity_type=USER,
        actor=None,
        new_values={"attempted_email": email},
        ip_address=ip_address,
        user_agent=user_agent,
    )


def on_password_changed(sender, *, user, actor=None, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type=USER,
        entity_id=user.pk,
        actor=actor,
        new_values={"password_changed": True},
    )


_WIRING = (
    (signals.user_registered, on_user_registered),
    (signals.role_assigned, on_role_assigned),
    (signals.role_revoked, on_role_revoked),
    (signals.user_deactivated, on_user_deactivated),
    (signals.user_reactivated, on_user_reactivated),
    (signals.portal_access_granted, on_portal_access_granted),
    (signals.portal_access_revoked, on_portal_access_revoked),
    (signals.user_logged_in, on_user_logged_in),
    (signals.user_login_failed, on_user_login_failed),
    (signals.password_changed, on_password_changed),
)


def connect():
    for signal, receiver in _WIRING:
        signal.connect(receiver, dispatch_uid=f"platform.{receiver.__name__}")
