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
from apps.crm import signals as crm_signals
from apps.finance import signals as finance_signals
from apps.identity import signals
from apps.inventory import signals as inventory_signals
from apps.property_ops import signals as property_ops_signals

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


def on_listing_status_changed(sender, *, listing, actor, from_status, to_status, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="LISTING",
        entity_id=listing.pk,
        actor=actor,
        old_values={"status": from_status},
        new_values={"status": to_status, "reference_code": listing.reference_code},
    )
    _webhooks(
        "listing.status_changed",
        {"listing_id": str(listing.pk), "from": from_status, "to": to_status},
    )


# --- crm (SRS 3.1, 3.4, 3.16.7) ------------------------------------------------------------


def on_lead_captured(sender, *, lead, actor, assigned_to, rule=None, **kwargs):
    record_event(
        action=AuditEvent.Action.CREATE,
        entity_type="LEAD",
        entity_id=lead.pk,
        actor=actor,
        new_values={
            "score": lead.score,
            "assigned_to": str(assigned_to.pk) if assigned_to else None,
            "routing_rule": rule.name if rule else None,
            "possible_duplicate": lead.is_possible_duplicate,
        },
    )
    _webhooks(
        "lead.captured",
        {
            "lead_id": str(lead.pk),
            "lead_type": lead.lead_type,
            "score": lead.score,
            "assigned_to": str(assigned_to.pk) if assigned_to else None,
        },
    )


def on_lead_assigned(sender, *, lead, actor, from_user, to_user, reason=None, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="LEAD",
        entity_id=lead.pk,
        actor=actor,
        old_values={"assigned_agent": str(from_user.pk) if from_user else None},
        new_values={
            "assigned_agent": str(to_user.pk) if to_user else None,
            "assigned_team": str(lead.assigned_team_id) if lead.assigned_team_id else None,
            "reason": reason,
        },
    )


def on_lead_converted(sender, *, lead, actor, deal, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="LEAD",
        entity_id=lead.pk,
        actor=actor,
        new_values={"converted_to_deal": deal.reference_code, "deal_id": str(deal.pk)},
    )
    _webhooks(
        "lead.converted",
        {"lead_id": str(lead.pk), "deal_id": str(deal.pk),
         "deal_reference": deal.reference_code},
    )


def on_gps_track_accessed(sender, *, session, actor, point_count, **kwargs):
    # SRS 3.16.7 — "access to GPS tracks shall be role-restricted and audited". Row visibility
    # is the restriction; this is the audit, and without it the requirement is half met.
    record_event(
        action=AuditEvent.Action.VIEW,
        entity_type="AGENT_FIELD_SESSION",
        entity_id=session.pk,
        actor=actor,
        new_values={
            "agent_id": str(session.agent_id),
            "points_disclosed": point_count,
        },
    )


# --- property_ops (SRS §3.5, §3.14) --------------------------------------------------------


def on_lease_created(sender, *, lease, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.CREATE,
        entity_type="LEASE",
        entity_id=lease.pk,
        actor=actor,
        new_values={
            "reference_code": lease.reference_code,
            "tenant_id": str(lease.tenant_id),
            "rent_amount": str(lease.rent_amount),
        },
    )


def on_lease_status_changed(sender, *, lease, from_status, to_status, actor, reason=None, **kwargs):
    # This row IS the lease's status history — property_ops deliberately has no history
    # table, and the append-only audit trail carries the trail instead.
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="LEASE",
        entity_id=lease.pk,
        actor=actor,
        old_values={"status": from_status},
        new_values={"status": to_status, "reason": reason},
    )


def on_application_decided(sender, *, application, actor, decision, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="APPLICATION",
        entity_id=application.pk,
        actor=actor,
        new_values={"decision": decision},
    )


def on_deposit_movement(sender, *, deposit, actor, kind, amount, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="DEPOSIT",
        entity_id=deposit.pk,
        actor=actor,
        new_values={"movement": kind, "amount": str(amount), "status": deposit.status},
    )


def on_maintenance_status_changed(sender, *, request, from_status, to_status, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="MAINTENANCE_REQUEST",
        entity_id=request.pk,
        actor=actor,
        old_values={"status": from_status},
        new_values={"status": to_status},
    )


def on_work_order_completed(sender, *, work_order, actor, final_amount, expense=None, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="WORK_ORDER",
        entity_id=work_order.pk,
        actor=actor,
        new_values={
            "status": "COMPLETED",
            "final_amount": str(final_amount),
            "expense_id": str(expense.pk) if expense else None,
        },
    )


# --- finance (SRS §3.6, §5.3) ---------------------------------------------------------------


def on_invoice_issued(sender, *, invoice, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.CREATE,
        entity_type="INVOICE",
        entity_id=invoice.pk,
        actor=actor,
        new_values={
            "invoice_number": invoice.invoice_number,
            "total_amount": str(invoice.total_amount),
            "invoice_type": invoice.invoice_type,
        },
    )


def on_invoice_voided(sender, *, invoice, actor, reason, old_status, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="INVOICE",
        entity_id=invoice.pk,
        actor=actor,
        old_values={"status": old_status},
        new_values={"status": invoice.status, "reason": reason},
    )


def on_payment_posted(sender, *, payment, actor, allocations=None, **kwargs):
    record_event(
        action=AuditEvent.Action.PAYMENT_POSTED,
        entity_type="PAYMENT",
        entity_id=payment.pk,
        actor=actor,
        new_values={
            "payment_reference": payment.payment_reference,
            "amount": str(payment.amount),
            "method": payment.payment_method,
        },
    )


def on_payment_reversed(sender, *, payment, actor, reason, refund=False, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="PAYMENT",
        entity_id=payment.pk,
        actor=actor,
        old_values={"status": "POSTED"},
        new_values={"status": payment.status, "reason": reason, "refund": refund},
    )


def on_commission_approved(sender, *, commission, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.COMMISSION_APPROVED,
        entity_type="COMMISSION",
        entity_id=commission.pk,
        actor=actor,
        new_values={"net_commission": str(commission.net_commission)},
    )


def on_commission_paid(sender, *, commission, actor, account, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="COMMISSION",
        entity_id=commission.pk,
        actor=actor,
        new_values={"status": "PAID", "account_id": str(account.pk)},
    )


def on_cheque_bounced(sender, *, cheque, actor, reason, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="CHEQUE",
        entity_id=cheque.pk,
        actor=actor,
        new_values={"status": "BOUNCED", "reason": reason},
    )


def on_statement_issued(sender, *, statement, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.CREATE,
        entity_type="OWNER_STATEMENT",
        entity_id=statement.pk,
        actor=actor,
        new_values={
            "statement_number": statement.statement_number,
            "net_payable": str(statement.net_payable),
        },
    )


def on_deal_stage_moved(sender, *, deal, actor, from_stage, to_stage, reason, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="DEAL",
        entity_id=deal.pk,
        actor=actor,
        old_values={"stage": from_stage.name},
        new_values={"stage": to_stage.name, "reason": reason, "status": deal.status},
    )
    _webhooks(
        "deal.stage_moved",
        {"deal_id": str(deal.pk), "from": from_stage.code, "to": to_stage.code,
         "status": deal.status},
    )


def on_offer_accepted(sender, *, offer, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="DEAL",
        entity_id=offer.deal_id,
        actor=actor,
        new_values={
            "offer_id": str(offer.pk),
            "accepted_amount": str(offer.amount),
            "direction": offer.direction,
        },
    )
    _webhooks(
        "offer.accepted",
        {"offer_id": str(offer.pk), "deal_id": str(offer.deal_id),
         "amount": str(offer.amount)},
    )


def on_transaction_status_changed(
    sender, *, transaction_obj, actor, from_status, to_status, **kwargs
):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="DEAL",
        entity_id=transaction_obj.deal_id or transaction_obj.pk,
        actor=actor,
        old_values={"transaction_status": from_status},
        new_values={
            "transaction_status": to_status,
            "transaction": transaction_obj.reference_code,
        },
    )
    _webhooks(
        "transaction.status_changed",
        {"transaction_id": str(transaction_obj.pk),
         "reference": transaction_obj.reference_code,
         "from": from_status, "to": to_status},
    )


def _webhooks(event_type, payload):
    """Fan the event out to subscribed outbound webhooks. Late import (fan-out is optional
    machinery) and never raises — a webhook must not break a domain write."""
    from .webhooks import dispatch_webhooks

    dispatch_webhooks(event_type, payload)


def on_role_permission_changed(sender, *, role, permission_code, granted, actor, **kwargs):
    record_event(
        action=AuditEvent.Action.UPDATE,
        entity_type="ROLE",
        entity_id=role.pk,
        actor=actor,
        new_values=(
            {"granted": permission_code} if granted else {"revoked": permission_code}
        ),
    )


_WIRING = (
    (property_ops_signals.lease_created, on_lease_created),
    (property_ops_signals.lease_status_changed, on_lease_status_changed),
    (property_ops_signals.application_decided, on_application_decided),
    (property_ops_signals.deposit_movement, on_deposit_movement),
    (property_ops_signals.maintenance_status_changed, on_maintenance_status_changed),
    (property_ops_signals.work_order_completed, on_work_order_completed),
    (finance_signals.invoice_issued, on_invoice_issued),
    (finance_signals.invoice_voided, on_invoice_voided),
    (finance_signals.payment_posted, on_payment_posted),
    (finance_signals.payment_reversed, on_payment_reversed),
    (finance_signals.commission_approved, on_commission_approved),
    (finance_signals.commission_paid, on_commission_paid),
    (finance_signals.cheque_bounced, on_cheque_bounced),
    (finance_signals.statement_issued, on_statement_issued),
    (crm_signals.lead_captured, on_lead_captured),
    (crm_signals.lead_assigned, on_lead_assigned),
    (crm_signals.lead_converted, on_lead_converted),
    (crm_signals.gps_track_accessed, on_gps_track_accessed),
    (crm_signals.deal_stage_moved, on_deal_stage_moved),
    (crm_signals.offer_accepted, on_offer_accepted),
    (crm_signals.transaction_status_changed, on_transaction_status_changed),
    (inventory_signals.status_changed, on_status_changed),
    (inventory_signals.listing_status_changed, on_listing_status_changed),
    (contact_signals.contacts_merged, on_contacts_merged),
    (contact_signals.contact_deleted, on_contact_deleted),
    (contact_signals.contacts_exported, on_contacts_exported),
    (contact_signals.contacts_imported, on_contacts_imported),
    (signals.user_registered, on_user_registered),
    (signals.role_assigned, on_role_assigned),
    (signals.role_revoked, on_role_revoked),
    (signals.role_permission_changed, on_role_permission_changed),
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
