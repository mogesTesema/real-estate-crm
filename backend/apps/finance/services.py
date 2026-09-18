"""Public write API for `finance` (architecture.md §1.2, §11) — the sole money writer.

Every create/update/post of money goes through this module. No other app writes a finance
row; no signal creates one; the DAG's forbidden-patterns list names both. The one sanctioned
outbound write is stamping `property_ops.RentSchedule.invoice/status` when a period is
invoiced — the RentSchedule docstring grants exactly that.

**Ledger convention, stated once**: `finance_account_entry` is a cash register per account.
Money in = CREDIT, money out = DEBIT, balance = ΣCREDIT − ΣDEBIT. The table is append-only
at the database-role level — a posted entry is never edited; corrections are opposite
entries.

**The one invariant everything else leans on**: `Invoice.amount_paid` equals the sum of
allocations from POSTED payments, and `balance_due = total_amount − amount_paid` (a CHECK).
`_refresh_invoice` is the only code allowed to write those two fields, and it always writes
them together. A grep-guard test holds that exclusivity.
"""
import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.services import next_reference

from . import signals
from .models import (
    Account,
    AccountEntry,
    Cheque,
    InstallmentMilestone,
    InstallmentPlan,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
)

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")


def _reject_unknown(fields, allowed, what):
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError(
            {name: f"This field cannot be set through the {what} service." for name in unknown}
        )


def _company_setting(key, default):
    """Config knobs live in `identity_company.settings` JSON (single-company system) with a
    Django-settings fallback — VAT rates and late-fee policy are operations decisions, not
    deployments."""
    from apps.identity.models import Company

    company = Company.objects.first()
    if company is not None and isinstance(company.settings, dict) and key in company.settings:
        return company.settings[key]
    return getattr(django_settings, f"FINANCE_{key.upper()}", default)


def _vat_rate() -> Decimal:
    return Decimal(str(_company_setting("vat_rate", "0")))


def _default_currency() -> str:
    from apps.identity.models import Company

    company = Company.objects.first()
    return company.default_currency if company else "AED"


# --- Ledger ---------------------------------------------------------------------------------


def _post_entry(*, account, entry_type, amount, reference_type, reference_id,
                description=None, currency=None, posted_at=None):
    return AccountEntry.objects.create(
        account=account,
        entry_type=entry_type,
        amount=amount,
        currency=currency or account.currency,
        reference_type=reference_type,
        reference_id=reference_id,
        description=description,
        posted_at=posted_at or timezone.now(),
    )


@transaction.atomic
def create_account(*, actor, name, account_type, currency, account_number=None):
    return Account.objects.create(
        name=name, account_type=account_type, currency=currency,
        account_number=account_number,
    )


@transaction.atomic
def update_account(account, *, actor, **fields):
    _reject_unknown(fields, frozenset({"name", "account_number", "is_active"}), "account")
    for name, value in fields.items():
        setattr(account, name, value)
    account.save()
    return account


@transaction.atomic
def post_adjustment(*, actor, account, entry_type, amount, description,
                    reference_type=AccountEntry.ReferenceType.ADJUSTMENT, reference_id=None):
    """The only public door for a raw ledger entry — manual corrections, reconciliation
    adjustments. Everything else posts through its owning operation."""
    if amount <= 0:
        raise ValidationError({"amount": "Ledger amounts are positive; direction is the type."})
    if not (description or "").strip():
        raise ValidationError({"description": "A manual ledger entry needs its reason."})
    return _post_entry(
        account=account, entry_type=entry_type, amount=amount,
        reference_type=reference_type, reference_id=reference_id, description=description,
    )


# --- Invoices (SRS 3.6.2, 3.6.7) ------------------------------------------------------------


def _compute_lines(lines):
    """Line arithmetic, one place. `tax_rate=None` means "the company default VAT"
    (SRS 3.6.7); an explicit 0 means genuinely untaxed."""
    computed, subtotal, tax_total = [], Decimal("0"), Decimal("0")
    if not lines:
        raise ValidationError({"lines": "An invoice needs at least one line."})
    for line in lines:
        quantity = Decimal(str(line["quantity"]))
        unit_price = Decimal(str(line["unit_price"]))
        if quantity <= 0:
            raise ValidationError({"lines": "Line quantity must be positive."})
        rate = line.get("tax_rate")
        rate = _vat_rate() if rate is None else Decimal(str(rate))
        base = (quantity * unit_price).quantize(TWO_PLACES, ROUND_HALF_UP)
        tax = (base * rate / 100).quantize(TWO_PLACES, ROUND_HALF_UP)
        computed.append(
            {
                "description": line["description"],
                "quantity": quantity,
                "unit_price": unit_price,
                "tax_rate": rate,
                "tax_amount": tax,
                "line_total": base + tax,
                "property": line.get("property"),
            }
        )
        subtotal += base
        tax_total += tax
    return computed, subtotal, tax_total


@transaction.atomic
def create_invoice(*, actor, contact, invoice_type, issue_date, due_date, lines,
                   currency=None, deal=None, lease=None, transaction_obj=None,
                   discount_amount=Decimal("0"), notes=None, issue=False):
    computed, subtotal, tax_total = _compute_lines(lines)
    discount = Decimal(str(discount_amount))
    total = subtotal + tax_total - discount
    if total < 0:
        raise ValidationError({"discount_amount": "Discount exceeds the invoice value."})

    invoice = Invoice.objects.create(
        invoice_number=next_reference("invoice", prefix="INV"),
        contact=contact,
        deal=deal,
        lease=lease,
        transaction=transaction_obj,
        invoice_type=invoice_type,
        status=Invoice.Status.ISSUED if issue else Invoice.Status.DRAFT,
        issue_date=issue_date,
        due_date=due_date,
        currency=currency or _default_currency(),
        subtotal=subtotal,
        tax_amount=tax_total,
        discount_amount=discount,
        total_amount=total,
        amount_paid=Decimal("0"),
        balance_due=total,  # set with total in the same INSERT — the CHECK holds
        notes=notes,
        created_by=actor,
    )
    InvoiceLine.objects.bulk_create(
        [InvoiceLine(invoice=invoice, **line) for line in computed]
    )
    if issue:
        signals.invoice_issued.send_robust(sender=None, invoice=invoice, actor=actor)
    return invoice


@transaction.atomic
def update_draft_invoice(invoice, *, actor, lines=None, **fields):
    if invoice.status != Invoice.Status.DRAFT:
        raise ValidationError({"status": "Only a draft invoice is editable; void and reissue."})
    _reject_unknown(
        fields, frozenset({"issue_date", "due_date", "notes", "discount_amount"}), "invoice"
    )
    for name, value in fields.items():
        setattr(invoice, name, value)
    if lines is not None:
        computed, subtotal, tax_total = _compute_lines(lines)
        invoice.lines.all().delete()
        InvoiceLine.objects.bulk_create(
            [InvoiceLine(invoice=invoice, **line) for line in computed]
        )
        invoice.subtotal, invoice.tax_amount = subtotal, tax_total
    total = invoice.subtotal + invoice.tax_amount - invoice.discount_amount
    if total < 0:
        raise ValidationError({"discount_amount": "Discount exceeds the invoice value."})
    invoice.total_amount = total
    invoice.balance_due = total - invoice.amount_paid
    invoice.save()
    return invoice


@transaction.atomic
def issue_invoice(invoice, *, actor):
    if invoice.status != Invoice.Status.DRAFT:
        raise ValidationError({"status": "Only a draft invoice can be issued."})
    invoice.status = Invoice.Status.ISSUED
    invoice.save(update_fields=["status", "updated_at"])
    signals.invoice_issued.send_robust(sender=None, invoice=invoice, actor=actor)
    return invoice


@transaction.atomic
def void_invoice(invoice, *, actor, reason, status=Invoice.Status.VOID):
    """Kill an invoice that has taken no money. Anything POSTED-allocated is history now —
    reverse the payment first; a voided invoice with cash against it is a books error."""
    if not (reason or "").strip():
        raise ValidationError({"reason": "Voiding a money document needs a reason."})
    if status not in (Invoice.Status.VOID, Invoice.Status.CANCELLED):
        raise ValidationError({"status": "An invoice is voided or cancelled, nothing else."})
    if invoice.status in (Invoice.Status.VOID, Invoice.Status.CANCELLED):
        return invoice
    if invoice.allocations.filter(payment__status=Payment.Status.POSTED).exists():
        raise ValidationError(
            {"status": "Payments are allocated to this invoice; reverse them first."}
        )

    from apps.property_ops.models import Lease, RentSchedule

    old_status = invoice.status
    # Detach dependents CHECK-safely: milestones drop to PENDING *before* losing the FK
    # (the INVOICED-requires-invoice CHECK), rent periods reopen or cancel with their lease.
    for milestone in invoice.installment_milestones.all():
        milestone.status = InstallmentMilestone.Status.PENDING
        milestone.invoice = None
        milestone.save(update_fields=["status", "invoice"])
    for schedule in invoice.rent_schedules.select_related("lease"):
        schedule.status = (
            RentSchedule.Status.CANCELLED
            if schedule.lease.status == Lease.Status.TERMINATED
            else RentSchedule.Status.SCHEDULED
        )
        schedule.invoice = None
        schedule.save(update_fields=["status", "invoice"])

    invoice.status = status
    invoice.save(update_fields=["status", "updated_at"])
    signals.invoice_voided.send_robust(
        sender=None, invoice=invoice, actor=actor, reason=reason, old_status=old_status
    )
    return invoice


def _refresh_invoice(invoice):
    """THE invariant-holder: amount_paid = Σ POSTED allocations; balance follows; status
    walks. The only code that writes these fields, always together, always one save."""
    paid = invoice.allocations.filter(
        payment__status=Payment.Status.POSTED
    ).aggregate(total=Sum("allocated_amount"))["total"] or Decimal("0")

    invoice.amount_paid = paid
    invoice.balance_due = invoice.total_amount - paid
    fields = ["amount_paid", "balance_due", "updated_at"]

    if invoice.status not in (
        Invoice.Status.DRAFT, Invoice.Status.VOID, Invoice.Status.CANCELLED
    ):
        if paid <= 0:
            overdue = invoice.due_date < timezone.localdate()
            invoice.status = Invoice.Status.OVERDUE if overdue else Invoice.Status.ISSUED
        elif paid < invoice.total_amount:
            invoice.status = Invoice.Status.PARTIALLY_PAID
        else:
            invoice.status = Invoice.Status.PAID
        fields.append("status")

    invoice.save(update_fields=fields)
    _sync_rent_schedule(invoice)
    _sync_milestones(invoice)
    return invoice


def _sync_rent_schedule(invoice):
    """The sanctioned cross-app write: rent periods mirror their invoice (model docstring:
    "`invoice` is written only by finance.services")."""
    from apps.property_ops.models import RentSchedule

    mirror = {
        Invoice.Status.PAID: RentSchedule.Status.PAID,
        Invoice.Status.PARTIALLY_PAID: RentSchedule.Status.PARTIALLY_PAID,
        Invoice.Status.ISSUED: RentSchedule.Status.INVOICED,
        Invoice.Status.OVERDUE: RentSchedule.Status.OVERDUE,
    }
    target = mirror.get(invoice.status)
    if target:
        invoice.rent_schedules.exclude(status=target).update(status=target)


def _sync_milestones(invoice):
    for milestone in invoice.installment_milestones.select_related("plan"):
        if invoice.status == Invoice.Status.PAID:
            if milestone.status != InstallmentMilestone.Status.PAID:
                milestone.status = InstallmentMilestone.Status.PAID
                milestone.paid_at = timezone.now()
                milestone.save(update_fields=["status", "paid_at"])
            plan = milestone.plan
            if not plan.milestones.exclude(
                status__in=(
                    InstallmentMilestone.Status.PAID, InstallmentMilestone.Status.WAIVED
                )
            ).exists():
                plan.status = InstallmentPlan.Status.COMPLETED
                plan.save(update_fields=["status", "updated_at"])
        elif milestone.status == InstallmentMilestone.Status.PAID:
            milestone.status = InstallmentMilestone.Status.INVOICED
            milestone.paid_at = None
            milestone.save(update_fields=["status", "paid_at"])


# --- Payments and allocation (SRS 3.6) ------------------------------------------------------


@transaction.atomic
def record_payment(*, actor, payer, account, amount, payment_method, payment_date,
                   currency=None, allocations=None, external_reference=None, notes=None,
                   _entry_reference=None):
    """Post money in. One CREDIT ledger entry; optional inline allocations; audited.

    `_entry_reference` is the cheque path's hook: a payment created by `clear_cheque` posts
    its cash-in referenced to the CHEQUE, which is what makes the PDC register reconcile
    against the ledger.
    """
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValidationError({"amount": "A payment must be positive."})

    payment = Payment.objects.create(
        payment_reference=next_reference("payment", prefix="PAY"),
        payer=payer,
        account=account,
        amount=amount,
        currency=currency or account.currency,
        payment_method=payment_method,
        payment_date=payment_date,
        external_reference=external_reference,
        notes=notes,
        recorded_by=actor,
        status=Payment.Status.POSTED,
    )
    reference_type, reference_id = _entry_reference or (
        AccountEntry.ReferenceType.PAYMENT, payment.pk
    )
    _post_entry(
        account=account,
        entry_type=AccountEntry.EntryType.CREDIT,
        amount=amount,
        currency=payment.currency,
        reference_type=reference_type,
        reference_id=reference_id,
        description=f"Payment {payment.payment_reference}",
    )
    if allocations:
        allocate_payment(payment, actor=actor, allocations=allocations)

    signals.payment_posted.send_robust(
        sender=None, payment=payment, actor=actor, allocations=allocations
    )
    return payment


@transaction.atomic
def allocate_payment(payment, *, actor, allocations):
    """Apply a payment to invoices. `allocations` = [{"invoice": ..., "amount": delta}].

    Amounts are DELTAS, and a repeat allocation to the same invoice adjusts the existing
    row — UNIQUE(payment, invoice) means top-ups never stack. No ledger entry: the cash
    entered the account when the payment posted; allocation only says which bill it answers.
    """
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.POSTED:
        raise ValidationError({"payment": "A reversed payment allocates nothing."})

    resolved = []
    for entry in allocations:
        invoice = entry["invoice"]
        if not isinstance(invoice, Invoice):
            invoice = Invoice.objects.get(pk=invoice)
        delta = Decimal(str(entry["amount"]))
        if delta <= 0:
            raise ValidationError({"allocations": "Allocation amounts must be positive."})
        resolved.append((invoice, delta))

    # Lock invoices in a stable order — two clerks allocating overlapping sets must queue,
    # not deadlock.
    ids = [invoice.pk for invoice, _ in resolved]
    locked = {
        inv.pk: inv
        for inv in Invoice.objects.select_for_update()
        .filter(pk__in=ids)
        .order_by("invoice_number")
    }

    already = payment.allocations.aggregate(total=Sum("allocated_amount"))[
        "total"
    ] or Decimal("0")
    incoming = sum(delta for _, delta in resolved)
    if already + incoming > payment.amount:
        raise ValidationError(
            {"allocations": "Allocations would exceed the payment amount."}
        )

    rows = []
    for invoice, delta in resolved:
        invoice = locked[invoice.pk]
        if invoice.status not in (
            Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE
        ):
            raise ValidationError(
                {"allocations": f"{invoice.invoice_number} is not open for payment."}
            )
        if invoice.currency != payment.currency:
            raise ValidationError(
                {"allocations": f"{invoice.invoice_number} is billed in {invoice.currency}; "
                                f"this payment is {payment.currency}."}
            )
        if delta > invoice.balance_due:
            raise ValidationError(
                {"allocations": f"{invoice.invoice_number} has only "
                                f"{invoice.balance_due} outstanding."}
            )
        allocation, created = PaymentAllocation.objects.get_or_create(
            payment=payment, invoice=invoice, defaults={"allocated_amount": delta}
        )
        if not created:
            allocation.allocated_amount += delta
            allocation.save(update_fields=["allocated_amount"])
        rows.append(allocation)
        _refresh_invoice(invoice)
        _notify_rent_payment(invoice, payment, actor)
    return rows


def _notify_rent_payment(invoice, payment, actor):
    if invoice.invoice_type == Invoice.InvoiceType.RENT and invoice.lease_id:
        from apps.collaboration import services as collaboration_services

        collaboration_services.notify(
            recipient=invoice.lease.property_manager,
            type="PAYMENT_RECEIVED",
            title=f"Rent received: {invoice.invoice_number}",
            body=f"{payment.amount} {payment.currency} from {payment.payer}",
            entity_type="PAYMENT",
            entity_id=payment.pk,
            actor=actor,
        )


@transaction.atomic
def reverse_payment(payment, *, actor, reason, refund=False):
    """Take money back out — never by editing. The compensating DEBIT is the correction;
    the allocation rows are kept as history and excluded from every sum by the payment's
    status; the invoices regress to whatever the remaining money says."""
    if not (reason or "").strip():
        raise ValidationError({"reason": "Reversing money needs a reason."})
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.POSTED:
        raise ValidationError({"status": "Only a posted payment can be reversed."})

    payment.status = Payment.Status.REFUNDED if refund else Payment.Status.REVERSED
    payment.save(update_fields=["status"])
    _post_entry(
        account=payment.account,
        entry_type=AccountEntry.EntryType.DEBIT,
        amount=payment.amount,
        currency=payment.currency,
        reference_type=AccountEntry.ReferenceType.PAYMENT,
        reference_id=payment.pk,
        description=f"Reversal of {payment.payment_reference}: {reason}",
    )
    for allocation in payment.allocations.select_related("invoice"):
        _refresh_invoice(allocation.invoice)

    signals.payment_reversed.send_robust(
        sender=None, payment=payment, actor=actor, reason=reason, refund=refund
    )
    return payment


# --- Cheques — the PDC register (§11) -------------------------------------------------------

CHEQUE_TRANSITIONS = {
    Cheque.Status.HELD_IN_SAFE: {
        Cheque.Status.DEPOSITED, Cheque.Status.RETURNED, Cheque.Status.REPLACED
    },
    Cheque.Status.DEPOSITED: {Cheque.Status.CLEARED, Cheque.Status.BOUNCED},
    # A bounced cheque can be re-presented, swapped for a new one, or handed back.
    Cheque.Status.BOUNCED: {
        Cheque.Status.DEPOSITED, Cheque.Status.REPLACED, Cheque.Status.RETURNED
    },
    Cheque.Status.CLEARED: set(),
    Cheque.Status.REPLACED: set(),
    Cheque.Status.RETURNED: set(),
}


def _cheque_move(cheque, new_status):
    allowed = CHEQUE_TRANSITIONS.get(cheque.status, set())
    if new_status not in allowed:
        raise ValidationError(
            {"status": f"Cannot move a cheque from {cheque.status} to {new_status}."
                       + (f" Allowed: {sorted(allowed)}." if allowed else " It is settled.")}
        )
    cheque.status = new_status


@transaction.atomic
def register_cheque(*, actor, payer, drawer_name, bank_name, cheque_number, amount,
                    cheque_date, lease=None):
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValidationError({"amount": "A cheque must be positive."})
    return Cheque.objects.create(
        payer=payer, drawer_name=drawer_name, bank_name=bank_name,
        cheque_number=cheque_number, amount=amount, cheque_date=cheque_date, lease=lease,
    )


@transaction.atomic
def deposit_cheque(cheque, *, actor, account):
    _cheque_move(cheque, Cheque.Status.DEPOSITED)
    cheque.deposit_account = account
    cheque.save(update_fields=["status", "deposit_account", "updated_at"])
    return cheque


@transaction.atomic
def clear_cheque(cheque, *, actor, allocations=None, cleared_at=None):
    """The moment paper becomes money: CLEARED creates the Payment, whose ledger entry is
    CHEQUE-referenced. With no explicit allocations and a lease on the cheque, it pays the
    lease's open rent oldest-due-first — the thing a PDC exists to do."""
    _cheque_move(cheque, Cheque.Status.CLEARED)
    cleared_at = cleared_at or timezone.now()
    cheque.cleared_at = cleared_at
    cheque.save(update_fields=["status", "cleared_at", "updated_at"])

    if allocations is None and cheque.lease_id:
        allocations = _oldest_first_allocations(cheque.lease, cheque.amount)

    payment = record_payment(
        actor=actor,
        payer=cheque.payer,
        account=cheque.deposit_account,
        amount=cheque.amount,
        payment_method=Payment.PaymentMethod.CHEQUE,
        payment_date=cleared_at.date(),
        external_reference=cheque.cheque_number,
        allocations=allocations,
        _entry_reference=(AccountEntry.ReferenceType.CHEQUE, cheque.pk),
    )
    return cheque, payment


def _oldest_first_allocations(lease, budget):
    open_invoices = (
        Invoice.objects.filter(
            lease=lease,
            invoice_type=Invoice.InvoiceType.RENT,
            status__in=(
                Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE
            ),
        )
        .order_by("due_date")
    )
    allocations, remaining = [], Decimal(budget)
    for invoice in open_invoices:
        if remaining <= 0:
            break
        slice_amount = min(remaining, invoice.balance_due)
        if slice_amount > 0:
            allocations.append({"invoice": invoice, "amount": slice_amount})
            remaining -= slice_amount
    return allocations


@transaction.atomic
def bounce_cheque(cheque, *, actor, reason, bounced_at=None):
    """No payment existed yet — the payment is only born at clearing — so there is nothing
    to reverse; the rent it was meant to cover simply stays unpaid and the sweep marks it
    OVERDUE. A clawback *after* clearing is `reverse_payment`, not a cheque transition."""
    if not (reason or "").strip():
        raise ValidationError({"reason": "A bounce needs the bank's reason."})
    _cheque_move(cheque, Cheque.Status.BOUNCED)
    cheque.bounced_at = bounced_at or timezone.now()
    cheque.bounce_reason = reason
    cheque.save(update_fields=["status", "bounced_at", "bounce_reason", "updated_at"])

    signals.cheque_bounced.send_robust(sender=None, cheque=cheque, actor=actor, reason=reason)
    if cheque.lease_id:
        from apps.collaboration import services as collaboration_services

        collaboration_services.notify(
            recipient=cheque.lease.property_manager,
            type="SYSTEM",
            title=f"Cheque bounced: {cheque.cheque_number}",
            body=reason,
            entity_type="CHEQUE",
            entity_id=cheque.pk,
            actor=actor,
        )
    return cheque


@transaction.atomic
def replace_cheque(cheque, *, actor, **new_cheque_fields):
    _cheque_move(cheque, Cheque.Status.REPLACED)
    cheque.save(update_fields=["status", "updated_at"])
    replacement = register_cheque(
        actor=actor,
        payer=new_cheque_fields.pop("payer", cheque.payer),
        lease=new_cheque_fields.pop("lease", cheque.lease),
        drawer_name=new_cheque_fields.pop("drawer_name", cheque.drawer_name),
        bank_name=new_cheque_fields.pop("bank_name", cheque.bank_name),
        **new_cheque_fields,
    )
    return cheque, replacement


@transaction.atomic
def return_cheque(cheque, *, actor, reason=None):
    _cheque_move(cheque, Cheque.Status.RETURNED)
    cheque.save(update_fields=["status", "updated_at"])
    return cheque


# --- Rent invoicing (SRS 3.5.3) — the activate_lease ↔ recurring-command split --------------


def generate_rent_schedule_invoices(*, actor=None, lease=None, as_of=None, horizon_days=None):
    """Invoice every SCHEDULED rent period due within the horizon. Idempotent by
    construction: the schedule row itself is the claim — `invoice IS NULL` under a row lock —
    so overlapping runs cannot double-invoice (§1.3: "finance tasks keyed by business
    reference codes").

    `created_by` falls back to the lease's property manager: the recurring command has no
    human actor, and Invoice.created_by is NOT NULL by design.
    """
    from apps.property_ops.models import Lease, RentSchedule

    as_of = as_of or timezone.localdate()
    horizon = horizon_days if horizon_days is not None else int(
        _company_setting("rent_invoice_horizon_days", 14)
    )
    cutoff = as_of + timedelta(days=horizon)

    invoiceable_statuses = (
        Lease.Status.ACTIVE, Lease.Status.EXPIRING, Lease.Status.RENEWED
    )
    created = []
    with transaction.atomic():
        due = (
            RentSchedule.objects.select_for_update(of=("self",))
            .filter(
                status=RentSchedule.Status.SCHEDULED,
                invoice__isnull=True,
                due_date__lte=cutoff,
                lease__status__in=invoiceable_statuses,
            )
            .select_related(
                "lease", "lease__tenant", "lease__property", "lease__property_manager"
            )
        )
        if lease is not None:
            due = due.filter(lease=lease)

        for row in due:
            row_lease = row.lease
            vat_applies = (
                row_lease.lease_type == Lease.LeaseType.COMMERCIAL
                and bool(_company_setting("vat_on_rent", False))
            )
            invoice = create_invoice(
                actor=actor or row_lease.property_manager,
                contact=row_lease.tenant,
                invoice_type=Invoice.InvoiceType.RENT,
                lease=row_lease,
                issue_date=as_of,
                due_date=row.due_date,
                lines=[
                    {
                        "description": (
                            f"Rent {row.period_start} – {row.period_end} "
                            f"({row_lease.reference_code})"
                        ),
                        "quantity": 1,
                        "unit_price": row.amount + row.late_fee_amount,
                        "tax_rate": None if vat_applies else Decimal("0"),
                        "property": row_lease.property,
                    }
                ],
                issue=True,
            )
            row.invoice = invoice
            row.status = RentSchedule.Status.INVOICED
            row.save(update_fields=["invoice", "status"])
            created.append(invoice)
    return created


def sweep_overdue(*, as_of=None):
    """Everything past due, in one idempotent pass: invoices → OVERDUE, rent periods →
    OVERDUE with a **one-shot** late fee (the `late_fee_amount == 0` guard is the
    idempotency — a fee that compounds on every sweep run is not a policy, it's a bug),
    milestones → OVERDUE. The fee lands on the period's NEXT invoice; an issued invoice is
    never mutated."""
    from apps.property_ops.models import RentSchedule

    as_of = as_of or timezone.localdate()
    late_fee = _company_setting("late_fee", {}) or {}
    grace = int(late_fee.get("grace_days", 0))
    counts = {"invoices": 0, "rent_periods": 0, "milestones": 0}

    with transaction.atomic():
        counts["invoices"] = Invoice.objects.filter(
            status__in=(Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID),
            due_date__lt=as_of,
        ).update(status=Invoice.Status.OVERDUE)

        overdue_rows = RentSchedule.objects.select_for_update().filter(
            status__in=(RentSchedule.Status.INVOICED, RentSchedule.Status.PARTIALLY_PAID),
            due_date__lt=as_of - timedelta(days=grace),
        )
        for row in overdue_rows:
            fields = ["status"]
            row.status = RentSchedule.Status.OVERDUE
            if late_fee and row.late_fee_amount == 0:
                mode, value = late_fee.get("mode"), Decimal(str(late_fee.get("value", 0)))
                if mode == "FIXED":
                    row.late_fee_amount = value
                elif mode == "PERCENT":
                    row.late_fee_amount = (row.amount * value / 100).quantize(
                        TWO_PLACES, ROUND_HALF_UP
                    )
                fields.append("late_fee_amount")
            row.save(update_fields=fields)
            counts["rent_periods"] += 1
            _notify_overdue(row)

        counts["milestones"] = InstallmentMilestone.objects.filter(
            status__in=(
                InstallmentMilestone.Status.PENDING, InstallmentMilestone.Status.INVOICED
            ),
            due_date__lt=as_of,
        ).update(status=InstallmentMilestone.Status.OVERDUE)
    return counts


def _notify_overdue(row):
    from apps.collaboration import services as collaboration_services

    collaboration_services.notify(
        recipient=row.lease.property_manager,
        type="SYSTEM",
        title=f"Rent overdue: {row.lease.reference_code}",
        body=f"Period {row.period_start} – {row.period_end}, due {row.due_date}.",
        entity_type="RENT_SCHEDULE",
        entity_id=row.pk,
    )
