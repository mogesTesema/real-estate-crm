"""Public write API for `property_ops` (architecture.md §1.2, §10, §14).

Leasing, deposits, inspections, applications, renewals, maintenance, vendors, work orders.

This is the ONLY module another app may import to mutate `property_ops`-owned rows. Two
cross-app truths shape it: `crm` calls `create_lease_from_deal` (never writes a lease row
itself), and money always leaves through `finance.services` — the one sanctioned inbound
write is finance stamping `RentSchedule.invoice/status` when it invoices a period.

There is deliberately no LeaseStatusHistory table. The append-only audit trail (via
`signals.lease_status_changed` → platform) is the history: sent inside the mutating
transaction, `record_event`'s own savepoint keeping an audit hiccup from poisoning the
money path.
"""
import logging
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.services import next_reference

from . import signals
from .models import (
    Application,
    Deposit,
    Inspection,
    Lease,
    LeaseParty,
    MaintenanceRequest,
    Renewal,
    RentSchedule,
    Vendor,
    WorkOrder,
)

logger = logging.getLogger(__name__)


def _reject_unknown(fields, allowed, what):
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError(
            {name: f"This field cannot be set through the {what} service." for name in unknown}
        )


# --- The lease state machine (§10, SRS 3.5) -------------------------------------------------

LEASE_TRANSITIONS = {
    # TERMINATED from DRAFT = an abandoned draft: Lease has no soft delete and PROTECT FKs
    # point at it, so "delete" is a terminal status, not a row removal.
    Lease.Status.DRAFT: {
        Lease.Status.PENDING_SIGNATURE,
        Lease.Status.ACTIVE,
        Lease.Status.TERMINATED,
    },
    # Back to DRAFT = pulled for edits; leaving the occupying set releases the unit.
    Lease.Status.PENDING_SIGNATURE: {
        Lease.Status.ACTIVE,
        Lease.Status.DRAFT,
        Lease.Status.TERMINATED,
    },
    Lease.Status.ACTIVE: {
        Lease.Status.EXPIRING,
        Lease.Status.RENEWED,
        Lease.Status.TERMINATED,
        Lease.Status.EXPIRED,
    },
    # ACTIVE from EXPIRING = the end date was pushed out; the alert is rescinded.
    Lease.Status.EXPIRING: {
        Lease.Status.ACTIVE,
        Lease.Status.RENEWED,
        Lease.Status.TERMINATED,
        Lease.Status.EXPIRED,
    },
    # A RENEWED lease still runs to its own end date; only closure remains.
    Lease.Status.RENEWED: {Lease.Status.EXPIRED, Lease.Status.TERMINATED},
    Lease.Status.TERMINATED: set(),
    Lease.Status.EXPIRED: set(),
}


def _save_guarding_overlap(lease, update_fields=None):
    """Save a lease that may enter the occupying set, translating the GiST refusal.

    The exclusion constraint (`property_ops_lease_no_overlap`, raw SQL in migration 0002)
    fires at statement time when an occupying lease's inclusive `[start, end]` range collides
    on the same unit (or whole property). Without this savepoint-and-translate, a perfectly
    ordinary double-letting attempt surfaces as a 500.
    """
    try:
        with transaction.atomic():
            lease.save(update_fields=update_fields)
    except IntegrityError as exc:
        if "property_ops_lease_no_overlap" in str(exc):
            raise ValidationError(
                {
                    "start_date": (
                        "Another occupying lease already covers this unit/property for "
                        "part of this period. Note that end dates are inclusive — "
                        "back-to-back leases must not share a day."
                    )
                }
            ) from exc
        raise


LEASE_WRITABLE = frozenset(
    {
        "property", "unit", "tenant", "landlord", "property_manager", "lease_type",
        "start_date", "end_date", "rent_amount", "billing_frequency", "security_deposit",
        "management_fee", "notice_period_days", "terms",
    }
)

#: Terms that define the money and the occupancy — frozen once the lease leaves drafting.
_CORE_TERMS = frozenset(
    {"property", "unit", "tenant", "landlord", "start_date", "end_date", "rent_amount",
     "billing_frequency", "security_deposit"}
)


@transaction.atomic
def create_lease(*, actor, reference_code=None, status=None, parties=None, **fields):
    """Create a lease (SRS 3.5.1). Defaults to DRAFT — activation is a separate, deliberate
    act because it starts the money machinery."""
    _reject_unknown(fields, LEASE_WRITABLE, "lease")
    _validate_lease_fields(fields)

    lease = Lease(
        reference_code=reference_code or next_reference("lease", prefix="LSE"),
        status=status or Lease.Status.DRAFT,
        created_by=actor,
        **fields,
    )
    if lease.status in Lease.OCCUPYING_STATUSES:
        _save_guarding_overlap(lease)
    else:
        lease.save()

    for party in parties or ():
        LeaseParty.objects.create(lease=lease, **party)

    signals.lease_created.send_robust(sender=None, lease=lease, actor=actor)
    return lease


def _as_date(value):
    """Serializers hand real dates; shells and tests hand ISO strings. Coerce once here so
    the period arithmetic downstream never meets a str."""
    import datetime

    if isinstance(value, str):
        return datetime.date.fromisoformat(value)
    return value


def _validate_lease_fields(fields, *, existing=None):
    for name in ("start_date", "end_date"):
        if name in fields:
            fields[name] = _as_date(fields[name])

    def value(name):
        return fields[name] if name in fields else getattr(existing, name, None)

    start, end = value("start_date"), value("end_date")
    if start and end and end <= start:
        raise ValidationError({"end_date": "A lease must end after it starts."})
    tenant, landlord = value("tenant"), value("landlord")
    if tenant and landlord and tenant.pk == landlord.pk:
        raise ValidationError({"landlord": "Tenant and landlord cannot be the same contact."})
    prop, unit = value("property"), value("unit")
    if unit and prop and unit.property_id != prop.pk:
        raise ValidationError({"unit": "That unit belongs to a different property."})
    from decimal import Decimal as _D

    for name in ("rent_amount", "security_deposit", "management_fee"):
        if name in fields and fields[name] is not None:
            fields[name] = _D(str(fields[name]))
    rent = value("rent_amount")
    if rent is not None and rent <= 0:
        raise ValidationError({"rent_amount": "Rent must be positive."})


@transaction.atomic
def update_lease(lease, *, actor, **fields):
    """Patch a lease. Core terms only while drafting; `status` has its own service."""
    _reject_unknown(fields, LEASE_WRITABLE, "lease")
    if lease.status not in (Lease.Status.DRAFT, Lease.Status.PENDING_SIGNATURE):
        frozen = set(fields) & _CORE_TERMS
        if frozen:
            raise ValidationError(
                {
                    name: (
                        "This term is frozen once the lease is live. Renew or terminate "
                        "instead — a live contract's numbers are not editable."
                    )
                    for name in frozen
                }
            )
    _validate_lease_fields(fields, existing=lease)
    for name, value in fields.items():
        setattr(lease, name, value)
    if lease.status in Lease.OCCUPYING_STATUSES:
        _save_guarding_overlap(lease)
    else:
        lease.save()
    return lease


@transaction.atomic
def create_lease_from_deal(*, deal, actor, transaction=None, start_date, end_date,
                           rent_amount, billing_frequency, security_deposit,
                           lease_type=Lease.LeaseType.RESIDENTIAL, unit=None, **fields):
    """§1.2: "Letting deal won → property_ops.services.create_lease_from_deal". The ONE way
    a won crm deal becomes a lease — crm calls this and never writes a property_ops row.

    Everything derivable is derived (property from the primary link, tenant from the deal,
    landlord from the property's owner record, manager from the property); everything
    commercial is a required kwarg — a lease must not silently inherit an estimate. Lands as
    DRAFT: the property manager reviews and activates.
    """
    primary = (
        deal.deal_properties.order_by("-is_primary", "created_at")
        .select_related("property")
        .first()
    )
    if primary is None:
        raise ValidationError({"deal": "This deal has no linked property to lease."})
    prop = primary.property

    owner_record = (
        prop.owners.filter(is_primary_owner=True).first() or prop.owners.first()
    )
    if owner_record is None:
        raise ValidationError(
            {"deal": "The property has no owner record; add one before creating the lease."}
        )

    lease = create_lease(
        actor=actor,
        property=prop,
        unit=unit or primary.unit,
        tenant=deal.primary_contact,
        landlord=owner_record.contact,
        property_manager=prop.managed_by or deal.owner,
        lease_type=lease_type,
        start_date=start_date,
        end_date=end_date,
        rent_amount=rent_amount,
        billing_frequency=billing_frequency,
        security_deposit=security_deposit,
        **fields,
    )
    if transaction is not None:
        lease.transaction = transaction
        lease.save(update_fields=["transaction"])
    return lease


@transaction.atomic
def set_lease_parties(lease, parties, *, actor):
    """Replace the additional-party set (co-tenants, guarantors) — SRS 3.5.1."""
    for party in parties:
        if party.get("party_type") not in set(LeaseParty.PartyType.values):
            raise ValidationError({"party_type": "Unknown party type."})
    lease.parties.all().delete()
    return [LeaseParty.objects.create(lease=lease, **party) for party in parties]


@transaction.atomic
def change_lease_status(lease, new_status, *, actor, reason=None):
    if new_status not in LEASE_TRANSITIONS:
        raise ValidationError({"status": f"Unknown lease status {new_status!r}."})
    current = lease.status
    if current == new_status:
        return lease
    allowed = LEASE_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValidationError(
            {
                "status": (
                    f"Cannot move a lease from {current} to {new_status}."
                    + (f" Allowed: {sorted(allowed)}." if allowed else f" {current} is terminal.")
                )
            }
        )

    lease.status = new_status
    entering_occupancy = (
        new_status in Lease.OCCUPYING_STATUSES and current not in Lease.OCCUPYING_STATUSES
    )
    if entering_occupancy:
        _save_guarding_overlap(lease, update_fields=["status", "updated_at"])
    else:
        lease.save(update_fields=["status", "updated_at"])

    signals.lease_status_changed.send_robust(
        sender=None, lease=lease, from_status=current, to_status=new_status,
        actor=actor, reason=reason,
    )
    return lease


# --- Rent schedule (SRS 3.5.3) --------------------------------------------------------------


@transaction.atomic
def generate_rent_schedule(lease, *, actor, periods=None):
    """Materialize what is owed and when. Idempotent: a lease with schedule rows is done.

    `rent_amount` is the per-billing-period amount (matching `Listing.rent_amount`); the last
    period is clipped to `end_date` without proration — v1's documented simplification, the
    same trade every property manager here makes by hand.
    """
    existing = list(lease.rent_schedules.all())
    if existing:
        return existing

    if lease.billing_frequency == Lease.BillingFrequency.CUSTOM:
        if not periods:
            raise ValidationError(
                {"periods": "A CUSTOM billing frequency needs explicit periods."}
            )
        rows = _custom_periods(lease, periods)
    else:
        rows = _regular_periods(lease)

    return RentSchedule.objects.bulk_create(rows)


_FREQUENCY_STEP = {
    Lease.BillingFrequency.MONTHLY: relativedelta(months=1),
    Lease.BillingFrequency.QUARTERLY: relativedelta(months=3),
    Lease.BillingFrequency.YEARLY: relativedelta(years=1),
}


def _regular_periods(lease):
    step = _FREQUENCY_STEP[lease.billing_frequency]
    rows, cursor = [], lease.start_date
    while cursor <= lease.end_date:
        period_end = min(cursor + step - relativedelta(days=1), lease.end_date)
        rows.append(
            RentSchedule(
                lease=lease,
                period_start=cursor,
                period_end=period_end,
                due_date=cursor,
                amount=lease.rent_amount,
            )
        )
        cursor = period_end + relativedelta(days=1)
    return rows


def _custom_periods(lease, periods):
    rows, cursor = [], lease.start_date
    for entry in sorted(periods, key=lambda e: e["period_start"]):
        if entry["period_start"] != cursor:
            raise ValidationError(
                {"periods": "Custom periods must tile the lease term without gaps or overlap."}
            )
        if entry["period_end"] < entry["period_start"]:
            raise ValidationError({"periods": "A period must end on or after its start."})
        rows.append(
            RentSchedule(
                lease=lease,
                period_start=entry["period_start"],
                period_end=entry["period_end"],
                due_date=entry.get("due_date", entry["period_start"]),
                amount=entry["amount"],
            )
        )
        cursor = entry["period_end"] + relativedelta(days=1)
    if cursor != lease.end_date + relativedelta(days=1):
        raise ValidationError({"periods": "Custom periods must cover the whole lease term."})
    return rows


@transaction.atomic
def waive_rent_period(schedule, *, actor, reason):
    """Forgive a period. Only an uninvoiced one — an invoiced period is a money artefact,
    and forgiving it means voiding the invoice first (finance's call, not this module's)."""
    if schedule.status not in (RentSchedule.Status.SCHEDULED, RentSchedule.Status.OVERDUE):
        raise ValidationError(
            {"status": "Only an uninvoiced period can be waived; void its invoice first."}
        )
    if schedule.invoice_id:
        raise ValidationError(
            {"status": "This period is invoiced; void the invoice first."}
        )
    schedule.status = RentSchedule.Status.WAIVED
    schedule.save(update_fields=["status"])
    return schedule


# --- Activation — THE §1.2 orchestration ----------------------------------------------------


@transaction.atomic
def activate_lease(lease, *, actor, periods=None, invoice_horizon_days=None):
    """Make a lease live: occupy the unit, materialize the schedule, expect the deposit,
    invoice what is already due. One transaction — a failure anywhere leaves nothing.

    §1.2: "Activate lease: `property_ops.services.activate_lease` →
    `finance.services.generate_rent_schedule_invoices`."
    """
    from apps.finance import services as finance_services

    change_lease_status(lease, Lease.Status.ACTIVE, actor=actor)
    generate_rent_schedule(lease, actor=actor, periods=periods)

    if lease.security_deposit and lease.security_deposit > 0:
        if not lease.deposits.exists():
            # The *expectation* of a deposit. Receipt is stamped separately
            # (record_deposit_received) because "we hold it" and "the contract says we
            # should" are different facts (SRS 3.5.4).
            Deposit.objects.create(
                lease=lease,
                amount=lease.security_deposit,
                held_amount=Decimal("0"),
                status=Deposit.Status.HELD,
            )

    finance_services.generate_rent_schedule_invoices(
        actor=actor, lease=lease, horizon_days=invoice_horizon_days
    )

    from apps.collaboration import services as collaboration_services

    collaboration_services.notify(
        recipient=lease.property_manager,
        type="SYSTEM",
        title=f"Lease activated: {lease.reference_code}",
        entity_type="LEASE",
        entity_id=lease.pk,
        actor=actor,
    )
    lease.refresh_from_db()
    return lease


@transaction.atomic
def terminate_lease(lease, *, actor, reason=None):
    """One-click termination (SRS 3.5.5). Uninvoiced periods are cancelled; invoiced ones
    are money artefacts finance voids explicitly — never silently destroyed."""
    change_lease_status(lease, Lease.Status.TERMINATED, actor=actor, reason=reason)
    lease.rent_schedules.filter(status=RentSchedule.Status.SCHEDULED).update(
        status=RentSchedule.Status.CANCELLED
    )
    return lease


# --- Deposits (SRS 3.5.4, 3.6.8) ------------------------------------------------------------


def _deposit_move(deposit, *, actor, kind, amount):
    signals.deposit_movement.send_robust(
        sender=None, deposit=deposit, actor=actor, kind=kind, amount=amount
    )


@transaction.atomic
def record_deposit_received(lease, *, actor, amount=None, received_date=None, notes=None):
    deposit = lease.deposits.first()
    if deposit is None:
        deposit = Deposit(lease=lease, amount=amount or lease.security_deposit,
                          held_amount=Decimal("0"), status=Deposit.Status.HELD)
    received = amount if amount is not None else deposit.amount
    deposit.held_amount = received
    deposit.received_date = received_date or timezone.localdate()
    if notes:
        deposit.notes = notes
    deposit.save()
    _deposit_move(deposit, actor=actor, kind="RECEIVED", amount=received)
    return deposit


@transaction.atomic
def refund_deposit(deposit, *, actor, amount, refunded_date=None, notes=None):
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError({"amount": "A refund must be positive."})
    if deposit.refunded_amount + deposit.deducted_amount + amount > deposit.amount:
        raise ValidationError(
            {"amount": "Refund plus deductions would exceed the deposit held."}
        )
    deposit.refunded_amount += amount
    deposit.refunded_date = refunded_date or timezone.localdate()
    if notes:
        deposit.notes = (deposit.notes or "") + f"\n{notes}"
    settled = deposit.refunded_amount + deposit.deducted_amount >= deposit.amount
    deposit.status = (
        Deposit.Status.FULLY_REFUNDED if settled else Deposit.Status.PARTIALLY_REFUNDED
    )
    deposit.save()
    _deposit_move(deposit, actor=actor, kind="REFUND", amount=amount)
    return deposit


@transaction.atomic
def deduct_from_deposit(deposit, *, actor, amount, reason):
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError({"amount": "A deduction must be positive."})
    if not (reason or "").strip():
        raise ValidationError({"reason": "Taking a client's money needs a reason."})
    if deposit.refunded_amount + deposit.deducted_amount + amount > deposit.amount:
        raise ValidationError({"amount": "Deduction would exceed the deposit held."})
    deposit.deducted_amount += amount
    deposit.notes = (deposit.notes or "") + f"\nDeducted {amount}: {reason}"
    deposit.save()
    _deposit_move(deposit, actor=actor, kind="DEDUCTION", amount=amount)
    return deposit


@transaction.atomic
def forfeit_deposit(deposit, *, actor, reason):
    if not (reason or "").strip():
        raise ValidationError({"reason": "Forfeiting a deposit needs a reason."})
    remaining = deposit.amount - deposit.refunded_amount - deposit.deducted_amount
    deposit.deducted_amount += remaining
    deposit.status = Deposit.Status.FORFEITED
    deposit.notes = (deposit.notes or "") + f"\nForfeited: {reason}"
    deposit.save()
    _deposit_move(deposit, actor=actor, kind="FORFEIT", amount=remaining)
    return deposit


def _company_notice_days() -> int:
    """The default lease-expiry notice window (SRS 3.5.5), a company setting."""
    from apps.identity.models import Company

    company = Company.objects.first()
    if company is not None and isinstance(company.settings, dict):
        value = company.settings.get("lease_expiry_notice_days")
        if value is not None:
            return int(value)
    from django.conf import settings as django_settings

    return int(getattr(django_settings, "LEASE_EXPIRY_NOTICE_DAYS", 90))


# --- Applications / screening (SRS 3.5.2) ---------------------------------------------------

APPLICATION_TRANSITIONS = {
    Application.Status.SUBMITTED: {
        Application.Status.UNDER_REVIEW,
        Application.Status.REJECTED,
        Application.Status.WITHDRAWN,
    },
    Application.Status.UNDER_REVIEW: {
        Application.Status.APPROVED,
        Application.Status.REJECTED,
        Application.Status.WITHDRAWN,
    },
    Application.Status.APPROVED: {
        Application.Status.CONVERTED,
        Application.Status.WITHDRAWN,
    },
    Application.Status.REJECTED: set(),
    Application.Status.WITHDRAWN: set(),
    Application.Status.CONVERTED: set(),
}

APPLICATION_WRITABLE = frozenset(
    {"property", "unit", "applicant_contact", "assigned_to", "proposed_rent",
     "stated_income", "desired_move_in", "employment_status"}
)


@transaction.atomic
def submit_application(*, actor, property, applicant_contact, **fields):
    _reject_unknown(fields, APPLICATION_WRITABLE - {"property", "applicant_contact"},
                    "application")
    unit = fields.get("unit")
    if unit and unit.property_id != property.pk:
        raise ValidationError({"unit": "That unit belongs to a different property."})
    return Application.objects.create(
        property=property, applicant_contact=applicant_contact,
        created_by=actor, updated_by=actor, **fields,
    )


@transaction.atomic
def update_screening(application, *, actor, background_check_status=None,
                     credit_check_status=None, screening_result=None):
    """Record screening outcomes (SRS 3.5.2). `screening_result` JSON is merged, not
    replaced — a credit report must not erase the background check that arrived first."""
    if application.status not in (
        Application.Status.SUBMITTED, Application.Status.UNDER_REVIEW
    ):
        raise ValidationError({"status": "Screening only applies to an open application."})
    fields = ["updated_by", "updated_at"]
    if background_check_status is not None:
        application.background_check_status = background_check_status
        fields.append("background_check_status")
    if credit_check_status is not None:
        application.credit_check_status = credit_check_status
        fields.append("credit_check_status")
    if screening_result:
        application.screening_result = {
            **(application.screening_result or {}), **screening_result
        }
        fields.append("screening_result")
    application.updated_by = actor
    application.save(update_fields=fields)
    return application


@transaction.atomic
def decide_application(application, decision, *, actor, reason=None):
    if decision not in APPLICATION_TRANSITIONS:
        raise ValidationError({"status": f"Unknown application status {decision!r}."})
    allowed = APPLICATION_TRANSITIONS.get(application.status, set())
    if decision not in allowed:
        raise ValidationError(
            {"status": f"Cannot move an application from {application.status} to {decision}."}
        )
    application.status = decision
    fields = ["status", "updated_by", "updated_at"]
    if decision in (Application.Status.APPROVED, Application.Status.REJECTED):
        application.decided_by = actor
        application.decided_at = timezone.now()
        fields += ["decided_by", "decided_at"]
    if reason:
        # Application carries no notes column; the decision reason rides in the screening
        # JSON, next to the checks it summarizes.
        application.screening_result = {
            **(application.screening_result or {}), "decision_reason": reason
        }
        fields.append("screening_result")
    application.updated_by = actor
    application.save(update_fields=fields)
    signals.application_decided.send_robust(
        sender=None, application=application, actor=actor, decision=decision
    )
    return application


@transaction.atomic
def convert_application(application, *, actor, landlord=None, **lease_overrides):
    """APPROVED → CONVERTED + a DRAFT lease built from the application (SRS 3.5.2).

    The application has no FK to the lease it produced (and the schema stays as shipped),
    so the link travels in `screening_result["lease_id"]` and the lease is returned inline —
    a queryable back-pointer without a migration.
    """
    if application.status != Application.Status.APPROVED:
        raise ValidationError({"status": "Only an approved application converts to a lease."})

    if landlord is None:
        primary = application.property.owners.filter(is_primary_owner=True).first() \
            or application.property.owners.first()
        if primary is None:
            raise ValidationError(
                {"landlord": "This property has no owner record; name the landlord."}
            )
        landlord = primary.contact

    defaults = {
        "property": application.property,
        "unit": application.unit,
        "tenant": application.applicant_contact,
        "landlord": landlord,
        "property_manager": application.property.managed_by,
        "lease_type": Lease.LeaseType.RESIDENTIAL,
        "rent_amount": application.proposed_rent,
        "start_date": application.desired_move_in,
        "billing_frequency": Lease.BillingFrequency.MONTHLY,
        "security_deposit": 0,
    }
    defaults.update(lease_overrides)
    if not defaults.get("rent_amount"):
        raise ValidationError({"rent_amount": "The application states no rent; provide one."})
    if not defaults.get("start_date"):
        raise ValidationError({"start_date": "No move-in date; provide a lease start."})
    if not defaults.get("end_date"):
        raise ValidationError({"end_date": "A lease needs an end date."})

    lease = create_lease(actor=actor, **defaults)
    application.status = Application.Status.CONVERTED
    application.screening_result = {
        **(application.screening_result or {}), "lease_id": str(lease.pk)
    }
    application.updated_by = actor
    application.save(update_fields=["status", "screening_result", "updated_by", "updated_at"])
    return lease


# --- Renewals (SRS 3.5.5) -------------------------------------------------------------------

RENEWAL_TRANSITIONS = {
    Renewal.Status.PROPOSED: {
        Renewal.Status.NEGOTIATING, Renewal.Status.ACCEPTED,
        Renewal.Status.DECLINED, Renewal.Status.EXPIRED,
    },
    Renewal.Status.NEGOTIATING: {
        Renewal.Status.ACCEPTED, Renewal.Status.DECLINED, Renewal.Status.EXPIRED,
    },
    Renewal.Status.ACCEPTED: set(),
    Renewal.Status.DECLINED: set(),
    Renewal.Status.EXPIRED: set(),
}


@transaction.atomic
def propose_renewal(lease, *, actor, escalation_type=Renewal.EscalationType.NONE,
                    escalation_value=None, proposed_start_date=None,
                    proposed_end_date=None, proposed_rent=None, send_notice=True):
    """One-click renewal proposal (SRS 3.5.5): defaults derived from the running lease,
    escalation math done here so the proposal is a number, not a formula."""
    if lease.status not in (Lease.Status.ACTIVE, Lease.Status.EXPIRING):
        raise ValidationError({"status": "Only a running lease can be renewed."})

    start = _as_date(proposed_start_date) or lease.end_date + relativedelta(days=1)
    end = _as_date(proposed_end_date) or start + (lease.end_date - lease.start_date)
    current = lease.rent_amount

    if escalation_type == Renewal.EscalationType.FIXED_AMOUNT:
        if escalation_value is None:
            raise ValidationError({"escalation_value": "A fixed escalation needs an amount."})
        rent = current + Decimal(str(escalation_value))
    elif escalation_type == Renewal.EscalationType.PERCENTAGE:
        if escalation_value is None:
            raise ValidationError({"escalation_value": "A percentage escalation needs a rate."})
        rent = (current * (1 + Decimal(str(escalation_value)) / 100)).quantize(
            Decimal("0.01")
        )
    else:
        rent = Decimal(str(proposed_rent)) if proposed_rent is not None else current

    renewal = Renewal.objects.create(
        original_lease=lease,
        current_rent=current,
        proposed_rent=rent,
        proposed_start_date=start,
        proposed_end_date=end,
        escalation_type=escalation_type,
        escalation_value=escalation_value,
        notice_sent_at=timezone.now() if send_notice else None,
        created_by=actor,
        updated_by=actor,
    )
    if send_notice:
        _notify_renewal_proposed(renewal, actor)
    return renewal


def _notify_renewal_proposed(renewal, actor):
    """SRS 3.5.5's expiry/renewal notice, to the tenant's portal login when one exists."""
    from apps.collaboration import services as collaboration_services
    from apps.identity.models import PortalProfile

    profile = PortalProfile.objects.filter(
        contact=renewal.original_lease.tenant,
        eligibility_status=PortalProfile.EligibilityStatus.ACTIVE,
    ).first()
    if profile:
        collaboration_services.notify(
            recipient=profile.user,
            type="LEASE_EXPIRING",
            title=f"Renewal offer: {renewal.original_lease.reference_code}",
            body=(f"Proposed rent {renewal.proposed_rent} from "
                  f"{renewal.proposed_start_date} to {renewal.proposed_end_date}."),
            entity_type="RENEWAL",
            entity_id=renewal.pk,
            actor=actor,
        )


@transaction.atomic
def update_renewal(renewal, *, actor, **fields):
    allowed = frozenset({"proposed_rent", "proposed_start_date", "proposed_end_date"})
    _reject_unknown(fields, allowed, "renewal")
    if renewal.status not in (Renewal.Status.PROPOSED, Renewal.Status.NEGOTIATING):
        raise ValidationError({"status": "This renewal is settled."})
    for name, value in fields.items():
        setattr(renewal, name, value)
    if renewal.status == Renewal.Status.PROPOSED:
        renewal.status = Renewal.Status.NEGOTIATING
    renewal.updated_by = actor
    renewal.save()
    return renewal


@transaction.atomic
def decline_renewal(renewal, *, actor, reason=None):
    if renewal.status not in (Renewal.Status.PROPOSED, Renewal.Status.NEGOTIATING):
        raise ValidationError({"status": "This renewal is settled."})
    renewal.status = Renewal.Status.DECLINED
    renewal.decided_at = timezone.now()
    renewal.updated_by = actor
    renewal.save(update_fields=["status", "decided_at", "updated_by", "updated_at"])
    return renewal


@transaction.atomic
def accept_renewal(renewal, *, actor, **lease_overrides):
    """Accept → successor lease + original → RENEWED, one atomic block.

    Ordered so the GiST exclusion cannot false-positive: the successor starts strictly after
    the original's inclusive end date (validated with a friendly message before the
    constraint gets a chance), and enters as PENDING_SIGNATURE — occupying, but the ranges
    do not touch.
    """
    if renewal.status not in (Renewal.Status.PROPOSED, Renewal.Status.NEGOTIATING):
        raise ValidationError({"status": "This renewal is settled."})
    original = renewal.original_lease
    if original.status in Lease.OCCUPYING_STATUSES and (
        renewal.proposed_start_date <= original.end_date
    ):
        raise ValidationError(
            {"proposed_start_date": (
                "The renewal must start after the original lease ends — end dates are "
                "inclusive."
            )}
        )

    fields = {
        "property": original.property,
        "unit": original.unit,
        "tenant": original.tenant,
        "landlord": original.landlord,
        "property_manager": original.property_manager,
        "lease_type": original.lease_type,
        "start_date": renewal.proposed_start_date,
        "end_date": renewal.proposed_end_date,
        "rent_amount": renewal.proposed_rent,
        "billing_frequency": original.billing_frequency,
        "security_deposit": original.security_deposit,
        "terms": original.terms,
    }
    fields.update(lease_overrides)
    successor = create_lease(
        actor=actor, status=Lease.Status.PENDING_SIGNATURE, **fields
    )
    # Lineage carried, not re-derived: the renewal chain hangs off the original transaction.
    if original.transaction_id:
        successor.transaction_id = original.transaction_id
        successor.save(update_fields=["transaction"])

    renewal.new_lease = successor
    renewal.status = Renewal.Status.ACCEPTED
    renewal.decided_at = timezone.now()
    renewal.updated_by = actor
    renewal.save(
        update_fields=["new_lease", "status", "decided_at", "updated_by", "updated_at"]
    )

    change_lease_status(original, Lease.Status.RENEWED, actor=actor,
                        reason=f"Renewed as {successor.reference_code}.")
    return successor


# --- Inspections (SRS 3.5.4) — the §13 calendar twin of viewings ----------------------------

INSPECTION_TRANSITIONS = {
    Inspection.Status.SCHEDULED: {
        Inspection.Status.IN_PROGRESS, Inspection.Status.COMPLETED,
        Inspection.Status.CANCELLED,
    },
    Inspection.Status.IN_PROGRESS: {
        Inspection.Status.COMPLETED, Inspection.Status.CANCELLED,
    },
    Inspection.Status.COMPLETED: set(),
    Inspection.Status.CANCELLED: set(),
}


def _sync_inspection_activity(inspection, *, actor):
    """§1.2: schedule/reschedule MUST upsert the INSPECTION calendar activity — same rule,
    same function, same partial unique index as viewings."""
    from apps.collaboration import services as collaboration_services
    from apps.collaboration.models import Activity

    collaboration_services.upsert_activity_for_source(
        source_type=Activity.SourceType.INSPECTION,
        source_id=inspection.pk,
        activity_type=Activity.ActivityType.INSPECTION,
        subject=f"{inspection.get_inspection_type_display()} inspection — "
                f"{inspection.lease.reference_code}",
        assigned_to=inspection.performed_by,
        actor=actor,
        start_at=inspection.scheduled_date,
        property=inspection.property,
        lease=inspection.lease,
    )


@transaction.atomic
def schedule_inspection(*, actor, lease, inspection_type, scheduled_date, performed_by):
    inspection = Inspection.objects.create(
        lease=lease,
        property=lease.property,
        inspection_type=inspection_type,
        scheduled_date=scheduled_date,
        performed_by=performed_by,
    )
    _sync_inspection_activity(inspection, actor=actor)
    return inspection


@transaction.atomic
def reschedule_inspection(inspection, *, actor, scheduled_date, performed_by=None):
    if inspection.status not in (Inspection.Status.SCHEDULED, Inspection.Status.IN_PROGRESS):
        raise ValidationError({"status": "A closed inspection cannot move."})
    inspection.scheduled_date = scheduled_date
    fields = ["scheduled_date"]
    if performed_by is not None:
        inspection.performed_by = performed_by
        fields.append("performed_by")
    inspection.save(update_fields=fields)
    _sync_inspection_activity(inspection, actor=actor)
    return inspection


@transaction.atomic
def complete_inspection(inspection, *, actor, condition_summary=None, meter_readings=None,
                        completed_date=None):
    """Close out with the condition report and meter readings (SRS 3.5.4)."""
    from apps.collaboration import services as collaboration_services
    from apps.collaboration.models import Activity

    allowed = INSPECTION_TRANSITIONS.get(inspection.status, set())
    if Inspection.Status.COMPLETED not in allowed:
        raise ValidationError({"status": "This inspection is already closed."})
    inspection.status = Inspection.Status.COMPLETED
    inspection.completed_date = completed_date or timezone.localdate()
    fields = ["status", "completed_date"]
    if condition_summary is not None:
        inspection.condition_summary = condition_summary
        fields.append("condition_summary")
    if meter_readings:
        inspection.meter_readings = {
            **(inspection.meter_readings or {}), **meter_readings
        }
        fields.append("meter_readings")
    inspection.save(update_fields=fields)
    collaboration_services.close_activity_for_source(
        source_type=Activity.SourceType.INSPECTION,
        source_id=inspection.pk,
        status=Activity.Status.COMPLETED,
        completed_at=timezone.now(),
    )
    return inspection


@transaction.atomic
def cancel_inspection(inspection, *, actor, reason=None):
    from apps.collaboration import services as collaboration_services
    from apps.collaboration.models import Activity

    allowed = INSPECTION_TRANSITIONS.get(inspection.status, set())
    if Inspection.Status.CANCELLED not in allowed:
        raise ValidationError({"status": "This inspection is already closed."})
    inspection.status = Inspection.Status.CANCELLED
    if reason:
        inspection.condition_summary = (
            (inspection.condition_summary or "") + f"\nCancelled: {reason}"
        ).strip()
    inspection.save(update_fields=["status", "condition_summary"])
    collaboration_services.close_activity_for_source(
        source_type=Activity.SourceType.INSPECTION,
        source_id=inspection.pk,
        status=Activity.Status.CANCELLED,
        completed_at=timezone.now(),
    )
    return inspection


# --- Maintenance (SRS §3.14) ----------------------------------------------------------------

MAINTENANCE_TRANSITIONS = {
    MaintenanceRequest.Status.NEW: {
        MaintenanceRequest.Status.ASSIGNED, MaintenanceRequest.Status.CANCELLED,
    },
    MaintenanceRequest.Status.ASSIGNED: {
        MaintenanceRequest.Status.IN_PROGRESS, MaintenanceRequest.Status.NEW,
        MaintenanceRequest.Status.CANCELLED,
    },
    MaintenanceRequest.Status.IN_PROGRESS: {
        MaintenanceRequest.Status.WAITING_FOR_PARTS, MaintenanceRequest.Status.COMPLETED,
        MaintenanceRequest.Status.CANCELLED,
    },
    MaintenanceRequest.Status.WAITING_FOR_PARTS: {
        MaintenanceRequest.Status.IN_PROGRESS, MaintenanceRequest.Status.CANCELLED,
    },
    MaintenanceRequest.Status.COMPLETED: set(),
    MaintenanceRequest.Status.CANCELLED: set(),
}


@transaction.atomic
def create_maintenance_request(*, actor, property, title, description, unit=None, lease=None,
                               priority=MaintenanceRequest.Priority.MEDIUM,
                               estimated_cost=None):
    """Log an issue (SRS 3.14.1). Staff report as themselves; a portal client is FORCED to
    report as their own contact against their own tenancy — the view's carve-out lets them
    through StaffWrite, and this guard is the defense in depth behind it."""
    from apps.identity.scoping import portal_contact_id

    if unit and unit.property_id != property.pk:
        raise ValidationError({"unit": "That unit belongs to a different property."})

    portal_cid = portal_contact_id(actor)
    if portal_cid is not None:
        reported_by_contact_id = portal_cid
        target = lease
        if target is None:
            target = (
                Lease.objects.filter(
                    property=property, status__in=Lease.OCCUPYING_STATUSES
                )
                .filter(
                    _tenancy_q(portal_cid)
                )
                .first()
            )
            lease = target
        if target is None or not _lease_belongs_to_contact(target, portal_cid):
            raise ValidationError(
                {"lease": "You can only report issues for your own tenancy."}
            )
        request = MaintenanceRequest.objects.create(
            property=property, unit=unit, lease=lease, title=title,
            description=description, priority=priority,
            reported_by_contact_id=reported_by_contact_id,
        )
    else:
        request = MaintenanceRequest.objects.create(
            property=property, unit=unit, lease=lease, title=title,
            description=description, priority=priority,
            estimated_cost=estimated_cost,
            reported_by_user=actor,
        )

    from apps.collaboration import services as collaboration_services

    collaboration_services.notify(
        recipient=property.managed_by,
        type="MAINTENANCE_UPDATE",
        title=f"New maintenance request: {title}",
        body=description[:300],
        entity_type="MAINTENANCE_REQUEST",
        entity_id=request.pk,
        actor=actor if portal_cid is None else None,
    )
    return request


def _tenancy_q(contact_id):
    """Leases this contact is party to — the same shapes as the lease PORTAL_OWN scope."""
    return Q(tenant_id=contact_id) | Q(landlord_id=contact_id) | Q(
        parties__contact_id=contact_id
    )


def _lease_belongs_to_contact(lease, contact_id) -> bool:
    return (
        lease.tenant_id == contact_id
        or lease.landlord_id == contact_id
        or lease.parties.filter(contact_id=contact_id).exists()
    )


@transaction.atomic
def assign_maintenance(request, *, actor, vendor=None, assignee=None):
    if vendor is None and assignee is None:
        raise ValidationError({"assignee": "Assign to a vendor, a staff member, or both."})
    _maintenance_move(request, MaintenanceRequest.Status.ASSIGNED, actor=actor)
    request.assigned_vendor = vendor
    request.assigned_to = assignee
    request.save(update_fields=["assigned_vendor", "assigned_to", "status"])
    if assignee is not None:
        from apps.collaboration import services as collaboration_services

        collaboration_services.notify(
            recipient=assignee,
            type="MAINTENANCE_UPDATE",
            title=f"Maintenance assigned: {request.title}",
            entity_type="MAINTENANCE_REQUEST",
            entity_id=request.pk,
            actor=actor,
        )
    return request


def _maintenance_move(request, new_status, *, actor):
    allowed = MAINTENANCE_TRANSITIONS.get(request.status, set())
    if new_status not in allowed and new_status != request.status:
        raise ValidationError(
            {"status": f"Cannot move a request from {request.status} to {new_status}."}
        )
    old = request.status
    request.status = new_status
    if old != new_status:
        signals.maintenance_status_changed.send_robust(
            sender=None, request=request, from_status=old, to_status=new_status, actor=actor
        )


@transaction.atomic
def change_maintenance_status(request, new_status, *, actor, note=None):
    """Progress a request (SRS 3.14.2). Every move notifies the reporter — the tenant's
    visibility into "is anyone doing anything?" IS the requirement."""
    if new_status not in MAINTENANCE_TRANSITIONS:
        raise ValidationError({"status": f"Unknown maintenance status {new_status!r}."})
    if new_status == request.status:
        return request
    _maintenance_move(request, new_status, actor=actor)
    fields = ["status"]
    if new_status == MaintenanceRequest.Status.COMPLETED:
        request.resolved_at = timezone.now()
        fields.append("resolved_at")
        total = request.work_orders.filter(
            status=WorkOrder.Status.COMPLETED, final_amount__isnull=False
        ).aggregate(total=Sum("final_amount"))["total"]
        if total is not None:
            request.actual_cost = total
            fields.append("actual_cost")
    request.save(update_fields=fields)
    _notify_reporter(request, new_status, note)
    return request


def _notify_reporter(request, new_status, note=None):
    from apps.collaboration import services as collaboration_services
    from apps.identity.models import PortalProfile

    recipient = request.reported_by_user
    if recipient is None and request.reported_by_contact_id:
        profile = PortalProfile.objects.filter(
            contact_id=request.reported_by_contact_id,
            eligibility_status=PortalProfile.EligibilityStatus.ACTIVE,
        ).first()
        recipient = profile.user if profile else None
    if recipient is not None:
        collaboration_services.notify(
            recipient=recipient,
            type="MAINTENANCE_UPDATE",
            title=f"{request.title}: {new_status}",
            body=note,
            entity_type="MAINTENANCE_REQUEST",
            entity_id=request.pk,
        )


# --- Vendors --------------------------------------------------------------------------------


@transaction.atomic
def create_vendor(*, actor, contact, service_category, license_number=None):
    if Vendor.objects.filter(contact=contact).exists():
        raise ValidationError({"contact": "This contact is already a vendor."})
    return Vendor.objects.create(
        contact=contact, service_category=service_category, license_number=license_number
    )


@transaction.atomic
def update_vendor(vendor, *, actor, **fields):
    _reject_unknown(
        fields, frozenset({"service_category", "license_number", "is_active"}), "vendor"
    )
    for name, value in fields.items():
        setattr(vendor, name, value)
    vendor.save()
    return vendor


@transaction.atomic
def rate_vendor(vendor, *, actor, rating):
    rating = Decimal(str(rating))
    if not (0 <= rating <= 5):
        raise ValidationError({"rating": "A rating is 0 to 5."})
    vendor.rating = rating
    vendor.save(update_fields=["rating"])
    return vendor


# --- Work orders (SRS 3.14.2–3.14.4) --------------------------------------------------------

WORK_ORDER_TRANSITIONS = {
    WorkOrder.Status.DRAFT: {WorkOrder.Status.QUOTED, WorkOrder.Status.CANCELLED},
    WorkOrder.Status.QUOTED: {WorkOrder.Status.APPROVED, WorkOrder.Status.CANCELLED},
    WorkOrder.Status.APPROVED: {
        WorkOrder.Status.SCHEDULED, WorkOrder.Status.IN_PROGRESS, WorkOrder.Status.CANCELLED,
    },
    WorkOrder.Status.SCHEDULED: {WorkOrder.Status.IN_PROGRESS, WorkOrder.Status.CANCELLED},
    WorkOrder.Status.IN_PROGRESS: {WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED},
    WorkOrder.Status.COMPLETED: set(),
    WorkOrder.Status.CANCELLED: set(),
}


def _work_order_move(work_order, new_status):
    allowed = WORK_ORDER_TRANSITIONS.get(work_order.status, set())
    if new_status not in allowed:
        raise ValidationError(
            {"status": f"Cannot move a work order from {work_order.status} to {new_status}."}
        )
    work_order.status = new_status


@transaction.atomic
def create_work_order(*, actor, maintenance_request, vendor, notes=None, scheduled_at=None):
    return WorkOrder.objects.create(
        maintenance_request=maintenance_request, vendor=vendor, notes=notes,
        scheduled_at=scheduled_at,
    )


@transaction.atomic
def quote_work_order(work_order, *, actor, quoted_amount):
    quoted_amount = Decimal(str(quoted_amount))
    if quoted_amount <= 0:
        raise ValidationError({"quoted_amount": "A quote must be positive."})
    _work_order_move(work_order, WorkOrder.Status.QUOTED)
    work_order.quoted_amount = quoted_amount
    work_order.save(update_fields=["status", "quoted_amount"])
    return work_order


@transaction.atomic
def approve_work_order(work_order, *, actor, approved_amount=None):
    _work_order_move(work_order, WorkOrder.Status.APPROVED)
    work_order.approved_amount = (
        Decimal(str(approved_amount)) if approved_amount is not None
        else work_order.quoted_amount
    )
    work_order.save(update_fields=["status", "approved_amount"])
    return work_order


@transaction.atomic
def schedule_work_order(work_order, *, actor, scheduled_at):
    _work_order_move(work_order, WorkOrder.Status.SCHEDULED)
    work_order.scheduled_at = scheduled_at
    work_order.save(update_fields=["status", "scheduled_at"])
    return work_order


@transaction.atomic
def start_work_order(work_order, *, actor):
    _work_order_move(work_order, WorkOrder.Status.IN_PROGRESS)
    work_order.save(update_fields=["status"])
    return work_order


@transaction.atomic
def complete_work_order(work_order, *, actor, final_amount, bill_to_owner=False,
                        account=None, expense_status="APPROVED", notes=None):
    """Finish the job and hand the cost to finance (SRS 3.14.4) — THE §1.2 money hand-off:
    "Billable maintenance: property_ops → finance.services.record_expense → owner
    statement". property_ops never writes a finance row itself."""
    from apps.finance import services as finance_services

    final_amount = Decimal(str(final_amount))
    if final_amount < 0:
        raise ValidationError({"final_amount": "A cost cannot be negative."})
    _work_order_move(work_order, WorkOrder.Status.COMPLETED)
    work_order.final_amount = final_amount
    work_order.completed_at = timezone.now()
    if notes:
        work_order.notes = ((work_order.notes or "") + f"\n{notes}").strip()
    work_order.save(update_fields=["status", "final_amount", "completed_at", "notes"])

    request = work_order.maintenance_request
    expense = None
    if final_amount > 0:
        expense = finance_services.record_expense(
            actor=actor,
            category="MAINTENANCE",
            description=f"Work order {str(work_order.pk)[:8]} — {request.title}",
            amount=final_amount,
            expense_date=timezone.localdate(),
            property=request.property,
            unit=request.unit,
            lease=request.lease,
            maintenance_request=request,
            vendor_contact=work_order.vendor.contact,
            account=account,
            is_billable_to_owner=bill_to_owner,
            status=expense_status,
        )
    signals.work_order_completed.send_robust(
        sender=None, work_order=work_order, actor=actor,
        final_amount=final_amount, expense=expense,
    )
    _notify_reporter(request, "Work completed", notes)
    return work_order, expense


@transaction.atomic
def cancel_work_order(work_order, *, actor, reason=None):
    _work_order_move(work_order, WorkOrder.Status.CANCELLED)
    if reason:
        work_order.notes = ((work_order.notes or "") + f"\nCancelled: {reason}").strip()
    work_order.save(update_fields=["status", "notes"])
    return work_order
