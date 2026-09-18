"""Turn domain events into audit rows.

SRS 5.3 requires "a full audit trail for sensitive actions (login, data export, permission
changes, document access, GPS track access)". Registration, role changes, deactivation and
login all happen in `identity` — which the import DAG forbids from writing to `platform`.

So `identity` emits signals and this module records them. `platform -> identity` is the legal
direction (the satellite contract bars `platform` from the *domain* services —
contacts/inventory/crm/property_ops/finance — and deliberately does not list `identity`).

Connected in `apps/platform/apps.py::ready()`.
"""
from apps.contacts import signals as contact_signals
from apps.identity import signals
from apps.inventory import signals as inventory_signals

from .models import AuditEvent
from .services import record_event

USER = "USER"
USER_ROLE = "USER_ROLE"
PORTAL_PROFILE = "PORTAL_PROFILE"
CONTACT = "CONTACT"


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


# --- contacts (architecture.md §1.3, SRS 5.3) ---------------------------------------------
#
# `contacts` cannot call record_event directly either: the DAG forbids contacts -> platform.
# Same inversion, same reason. Only genuinely sensitive mutations are signalled — an audit row
# per contact edit would bury the events that matter, and created_by/updated_by already carry
# that on the row.


def on_contacts_merged(sender, *, survivor, duplicate, actor, moved, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type=CONTACT,
        entity_id=survivor.pk,
        actor=actor,
        old_values={"merged_contact_id": str(duplicate.pk)},
        # `moved` is the per-relation row counts. A merge is irreversible in practice, so what
        # it actually moved is the only way to reconstruct what the record looked like before.
        new_values={"moved": moved},
    )


def on_contact_deleted(sender, *, contact, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.DELETE,
        entity_type=CONTACT,
        entity_id=contact.pk,
        actor=actor,
        old_values={"display_name": str(contact)},
    )


def on_contacts_exported(sender, *, actor, row_count, fields, **kwargs):
    # SRS 5.3 names data export explicitly. A CSV of the contact base is the highest-value
    # thing a departing employee can take, so the size of what left is recorded, not just that
    # an export happened.
    record_event(
        action=AuditEvent.Action.EXPORT,
        entity_type=CONTACT,
        actor=actor,
        new_values={"row_count": row_count, "fields": fields},
    )


def on_contacts_imported(sender, *, actor, created, skipped, invalid, filename, **kwargs):
    record_event(
        action=AuditEvent.Action.CREATE,
        entity_type=CONTACT,
        actor=actor,
        new_values={
            "bulk_import": True,
            "filename": filename,
            "created": created,
            "skipped_as_duplicate": skipped,
            "invalid": invalid,
        },
    )


# --- inventory (SRS 3.3.4) ----------------------------------------------------------------


def on_status_changed(sender, *, target, from_status, to_status, actor, reason=None, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type=target._meta.model_name.upper(),
        entity_id=target.pk,
        actor=actor,
        old_values={"status": from_status},
        new_values={"status": to_status, "reason": reason},
    )


def on_listing_published(sender, *, listing, actor, from_status, to_status, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="LISTING",
        entity_id=listing.pk,
        actor=actor,
        old_values={"status": from_status},
        new_values={"status": to_status, "reference_code": listing.reference_code},
    )


_WIRING = (
    (inventory_signals.status_changed, on_status_changed),
    (inventory_signals.listing_published, on_listing_published),
    (contact_signals.contacts_merged, on_contacts_merged),
    (contact_signals.contact_deleted, on_contact_deleted),
    (contact_signals.contacts_exported, on_contacts_exported),
    (contact_signals.contacts_imported, on_contacts_imported),
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
