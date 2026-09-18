"""The finance core (§11, SRS 3.6) — invoices, payments, allocation, reversal, cheques.

The invariant everything leans on: `amount_paid` equals the sum of allocations from POSTED
payments and `balance_due = total − amount_paid` (a CHECK). `_refresh_invoice` is the only
writer of those fields, and the last test in this file greps the codebase to keep it that
way.
"""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.finance import services
from apps.finance.models import Account, AccountEntry, Cheque, Invoice, Payment


@pytest.fixture
def fin(make_user):
    return make_user("finance")


@pytest.fixture
def account(db, fin):
    return services.create_account(
        actor=fin, name="Operating", account_type=Account.AccountType.OPERATING_ACCOUNT,
        currency="AED",
    )


@pytest.fixture
def invoice_for(fin, make_contact):
    def _make(total="1000", contact=None, issue=True, **kwargs):
        return services.create_invoice(
            actor=fin,
            contact=contact or make_contact(),
            invoice_type=Invoice.InvoiceType.OTHER,
            issue_date="2026-09-01",
            due_date="2026-09-15",
            lines=[{"description": "Service", "quantity": 1, "unit_price": total,
                    "tax_rate": 0}],
            issue=issue,
            **kwargs,
        )

    return _make


@pytest.fixture
def pay(fin, account):
    def _make(payer, amount, **kwargs):
        return services.record_payment(
            actor=fin, payer=payer, account=account, amount=amount,
            payment_method=Payment.PaymentMethod.BANK_TRANSFER,
            payment_date="2026-09-10", **kwargs,
        )

    return _make


class TestInvoices:
    def test_line_arithmetic_with_default_vat(self, db, fin, make_contact):
        """`tax_rate=None` means the company default (SRS 3.6.7); explicit 0 means untaxed."""
        from apps.identity.models import Company

        company = Company.objects.first()
        if company is None:
            company = Company.objects.create(
                name="T", legal_name="T", email="t@t.t", phone="1", country="AE",
                timezone="UTC", default_currency="AED",
            )
        company.settings = {"vat_rate": "5"}
        company.save(update_fields=["settings"])

        invoice = services.create_invoice(
            actor=fin, contact=make_contact(), invoice_type=Invoice.InvoiceType.OTHER,
            issue_date="2026-09-01", due_date="2026-09-15",
            lines=[
                {"description": "Taxed", "quantity": 2, "unit_price": "100"},
                {"description": "Untaxed", "quantity": 1, "unit_price": "50", "tax_rate": 0},
            ],
        )
        assert invoice.subtotal == Decimal("250")
        assert invoice.tax_amount == Decimal("10")
        assert invoice.total_amount == Decimal("260")
        assert invoice.balance_due == Decimal("260")

    def test_a_draft_is_editable_an_issued_one_is_not(self, db, fin, invoice_for):
        draft = invoice_for(issue=False)
        services.update_draft_invoice(draft, actor=fin, notes="Edited.")
        issued = invoice_for()
        with pytest.raises(ValidationError, match="draft"):
            services.update_draft_invoice(issued, actor=fin, notes="Nope.")

    def test_issuing_is_audited(self, db, invoice_for):
        from apps.platform.models import AuditEvent

        invoice = invoice_for()
        assert AuditEvent.objects.filter(
            entity_type="INVOICE", entity_id=invoice.pk, action=AuditEvent.Action.CREATE
        ).exists()


class TestAllocation:
    def test_the_walk_to_paid(self, db, invoice_for, pay, make_contact):
        payer = make_contact()
        invoice = invoice_for("1000", contact=payer)
        payment = pay(payer, "400", allocations=[{"invoice": invoice, "amount": "400"}])
        invoice.refresh_from_db()
        assert invoice.status == Invoice.Status.PARTIALLY_PAID
        assert invoice.amount_paid == Decimal("400")
        assert invoice.balance_due == Decimal("600")

        services.allocate_payment(
            pay(payer, "600"), actor=payment.recorded_by,
            allocations=[{"invoice": invoice, "amount": "600"}],
        )
        invoice.refresh_from_db()
        assert invoice.status == Invoice.Status.PAID
        assert invoice.balance_due == Decimal("0")

    def test_a_top_up_adjusts_the_row_never_stacks(self, db, invoice_for, pay, make_contact, fin):
        """UNIQUE(payment, invoice): the second allocation of the same payment to the same
        invoice adds to the existing row."""
        payer = make_contact()
        invoice = invoice_for("1000", contact=payer)
        payment = pay(payer, "1000")
        services.allocate_payment(payment, actor=fin,
                                  allocations=[{"invoice": invoice, "amount": "300"}])
        services.allocate_payment(payment, actor=fin,
                                  allocations=[{"invoice": invoice, "amount": "200"}])
        assert payment.allocations.count() == 1
        assert payment.allocations.get().allocated_amount == Decimal("500")

    def test_over_allocation_of_the_payment_is_refused(self, db, invoice_for, pay, make_contact, fin):
        payer = make_contact()
        a, b = invoice_for("1000", contact=payer), invoice_for("1000", contact=payer)
        payment = pay(payer, "800")
        services.allocate_payment(payment, actor=fin,
                                  allocations=[{"invoice": a, "amount": "500"}])
        with pytest.raises(ValidationError, match="exceed the payment"):
            services.allocate_payment(payment, actor=fin,
                                      allocations=[{"invoice": b, "amount": "400"}])

    def test_over_payment_of_the_invoice_is_refused(self, db, invoice_for, pay, make_contact, fin):
        payer = make_contact()
        invoice = invoice_for("300", contact=payer)
        payment = pay(payer, "1000")
        with pytest.raises(ValidationError, match="outstanding"):
            services.allocate_payment(payment, actor=fin,
                                      allocations=[{"invoice": invoice, "amount": "400"}])

    def test_a_draft_invoice_takes_no_money(self, db, invoice_for, pay, make_contact, fin):
        payer = make_contact()
        draft = invoice_for("300", contact=payer, issue=False)
        with pytest.raises(ValidationError, match="not open"):
            services.allocate_payment(pay(payer, "300"), actor=fin,
                                      allocations=[{"invoice": draft, "amount": "300"}])

    def test_the_ledger_gets_one_credit_per_payment_none_per_allocation(
        self, db, invoice_for, pay, make_contact, fin
    ):
        """Cash enters the account when the payment posts; allocation only says which bill
        it answers."""
        payer = make_contact()
        invoice = invoice_for("1000", contact=payer)
        payment = pay(payer, "1000")
        services.allocate_payment(payment, actor=fin,
                                  allocations=[{"invoice": invoice, "amount": "1000"}])
        entries = AccountEntry.objects.filter(reference_id=payment.pk)
        assert entries.count() == 1
        assert entries.get().entry_type == AccountEntry.EntryType.CREDIT

    def test_payment_posting_is_audited(self, db, pay, make_contact):
        from apps.platform.models import AuditEvent

        payment = pay(make_contact(), "500")
        assert AuditEvent.objects.filter(
            entity_type="PAYMENT", entity_id=payment.pk,
            action=AuditEvent.Action.PAYMENT_POSTED,
        ).exists()


class TestReversal:
    def test_reversal_regresses_the_invoice_and_keeps_history(
        self, db, invoice_for, pay, make_contact, fin
    ):
        payer = make_contact()
        invoice = invoice_for("1000", contact=payer)
        payment = pay(payer, "1000", allocations=[{"invoice": invoice, "amount": "1000"}])
        invoice.refresh_from_db()
        assert invoice.status == Invoice.Status.PAID

        services.reverse_payment(payment, actor=fin, reason="Bank clawback.")
        invoice.refresh_from_db()
        payment.refresh_from_db()
        assert payment.status == Payment.Status.REVERSED
        # Allocation rows are history, not deleted — but excluded from every sum.
        assert payment.allocations.count() == 1
        assert invoice.amount_paid == Decimal("0")
        assert invoice.status in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE)
        # Compensating DEBIT posted; net cash movement zero.
        debits = AccountEntry.objects.filter(
            reference_id=payment.pk, entry_type=AccountEntry.EntryType.DEBIT
        )
        assert debits.count() == 1

    def test_a_reversed_payment_allocates_nothing(self, db, invoice_for, pay, make_contact, fin):
        payer = make_contact()
        invoice = invoice_for("500", contact=payer)
        payment = pay(payer, "500")
        services.reverse_payment(payment, actor=fin, reason="Duplicate entry.")
        with pytest.raises(ValidationError, match="reversed"):
            services.allocate_payment(payment, actor=fin,
                                      allocations=[{"invoice": invoice, "amount": "500"}])

    def test_void_is_refused_once_money_landed(self, db, invoice_for, pay, make_contact, fin):
        payer = make_contact()
        invoice = invoice_for("500", contact=payer)
        pay(payer, "500", allocations=[{"invoice": invoice, "amount": "500"}])
        with pytest.raises(ValidationError, match="reverse them first"):
            services.void_invoice(invoice, actor=fin, reason="Mistake.")


class TestCheques:
    def test_the_pdc_walk_to_cleared_money(self, db, fin, account, make_contact):
        payer = make_contact()
        cheque = services.register_cheque(
            actor=fin, payer=payer, drawer_name="Sam", bank_name="ENBD",
            cheque_number="000123", amount="5000", cheque_date="2026-10-01",
        )
        assert cheque.status == Cheque.Status.HELD_IN_SAFE
        services.deposit_cheque(cheque, actor=fin, account=account)
        cheque, payment = services.clear_cheque(cheque, actor=fin)
        assert cheque.status == Cheque.Status.CLEARED
        assert payment.payment_method == Payment.PaymentMethod.CHEQUE
        # The cash-in entry is CHEQUE-referenced — the PDC register reconciles the ledger.
        entry = AccountEntry.objects.get(reference_type=AccountEntry.ReferenceType.CHEQUE)
        assert entry.reference_id == cheque.pk

    def test_clearing_auto_pays_the_leases_oldest_rent_first(
        self, db, fin, account, make_user, make_property, make_contact
    ):
        from apps.property_ops import services as po_services
        from apps.property_ops.models import Lease

        pm = make_user("property_manager")
        tenant = make_contact()
        lease = po_services.create_lease(
            actor=pm, property=make_property(managed_by=pm), tenant=tenant,
            landlord=make_contact(), property_manager=pm,
            lease_type=Lease.LeaseType.RESIDENTIAL,
            start_date="2026-01-01", end_date="2026-06-30",
            rent_amount="1000", billing_frequency=Lease.BillingFrequency.MONTHLY,
            security_deposit="0",
        )
        po_services.activate_lease(lease, actor=pm)
        # Materialized: a lazy queryset would silently re-evaluate after clearing, and
        # the newly-PAID invoice would vanish from its own assertion.
        open_before = list(
            Invoice.objects.filter(
                lease=lease, status__in=("ISSUED", "OVERDUE", "PARTIALLY_PAID")
            ).order_by("due_date")
        )
        assert len(open_before) >= 2

        cheque = services.register_cheque(
            actor=fin, payer=tenant, drawer_name="T", bank_name="ENBD",
            cheque_number="42", amount="1500", cheque_date="2026-03-01", lease=lease,
        )
        services.deposit_cheque(cheque, actor=fin, account=account)
        services.clear_cheque(cheque, actor=fin)

        oldest = open_before[0]
        oldest.refresh_from_db()
        assert oldest.status == Invoice.Status.PAID
        second = open_before[1]
        second.refresh_from_db()
        assert second.amount_paid == Decimal("500")

    def test_a_bounce_notifies_and_leaves_rent_unpaid(self, db, fin, account, make_contact):
        cheque = services.register_cheque(
            actor=fin, payer=make_contact(), drawer_name="S", bank_name="B",
            cheque_number="9", amount="100", cheque_date="2026-10-01",
        )
        services.deposit_cheque(cheque, actor=fin, account=account)
        services.bounce_cheque(cheque, actor=fin, reason="Insufficient funds.")
        assert cheque.status == Cheque.Status.BOUNCED
        assert Payment.objects.count() == 0  # no payment was ever born

    def test_replace_chains(self, db, fin, make_contact):
        cheque = services.register_cheque(
            actor=fin, payer=make_contact(), drawer_name="S", bank_name="B",
            cheque_number="1", amount="100", cheque_date="2026-10-01",
        )
        old, new = services.replace_cheque(
            cheque, actor=fin, cheque_number="2", amount="100", cheque_date="2026-11-01"
        )
        assert old.status == Cheque.Status.REPLACED
        assert new.status == Cheque.Status.HELD_IN_SAFE


class TestRefreshExclusivity:
    def test_nothing_else_writes_amount_paid(self):
        """The grep guard: `_refresh_invoice` is the only writer of amount_paid/balance_due
        outside creation. Cheap, blunt, and it has already earned its keep in review."""
        import pathlib

        source = pathlib.Path("apps/finance/services.py").read_text()
        import re

        writes = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"\.amount_paid\s*=[^=]", line)
            and "invoice.amount_paid = paid" not in line
        ]
        assert writes == [], f"Unexpected amount_paid writers: {writes}"
