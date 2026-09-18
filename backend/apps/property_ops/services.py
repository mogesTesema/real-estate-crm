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
from django.utils import timezone

from apps.core.services import next_reference

from . import signals
from .models import (
    Deposit,
    Lease,
    LeaseParty,
    RentSchedule,
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
