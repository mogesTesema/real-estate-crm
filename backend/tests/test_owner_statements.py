"""Owner statements (SRS 3.5.6, 3.14.4) — collection basis, and the double-billing guards."""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.finance import services
from apps.finance.models import Account, Invoice, Payment
from apps.property_ops import services as po_services
from apps.property_ops.models import Lease


@pytest.fixture
def fin(make_user):
    return make_user("finance")


@pytest.fixture
def world(db, fin, make_user, make_property, make_contact):
    """A landlord with one active lease, one paid month, one billable expense."""
    pm = make_user("property_manager")
    landlord, tenant = make_contact(), make_contact()
    prop = make_property(managed_by=pm)
    lease = po_services.create_lease(
        actor=pm, property=prop, tenant=tenant, landlord=landlord, property_manager=pm,
        lease_type=Lease.LeaseType.RESIDENTIAL,
        start_date="2026-01-01", end_date="2026-12-31",
        rent_amount=Decimal("10000"), billing_frequency=Lease.BillingFrequency.MONTHLY,
        security_deposit=Decimal("0"), management_fee=Decimal("5"),
    )
    po_services.activate_lease(lease, actor=pm)
    account = services.create_account(
        actor=fin, name="Ops", account_type=Account.AccountType.OPERATING_ACCOUNT,
        currency="AED",
    )
    january = Invoice.objects.filter(lease=lease).order_by("due_date").first()
    services.record_payment(
        actor=fin, payer=tenant, account=account, amount=january.total_amount,
        payment_method=Payment.PaymentMethod.BANK_TRANSFER, payment_date="2026-01-05",
        allocations=[{"invoice": january, "amount": january.total_amount}],
    )
    expense = services.record_expense(
        actor=fin, category="MAINTENANCE", description="AC repair",
        amount=Decimal("800"), expense_date="2026-01-20",
        property=prop, lease=lease, is_billable_to_owner=True, status="APPROVED",
    )
    return {
        "pm": pm, "landlord": landlord, "tenant": tenant, "lease": lease,
        "account": account, "expense": expense,
    }


class TestOwnerStatement:
    def test_collection_basis_arithmetic(self, world, fin):
        """Rent COLLECTED in the period (by payment date), 5% management fee on it, the
        billable expense — net derived."""
        statement = services.generate_owner_statement(
            actor=fin, owner_contact=world["landlord"],
            period_start="2026-01-01", period_end="2026-01-31",
        )
        assert statement.gross_rent_collected == Decimal("10000.00")
        assert statement.management_fees == Decimal("500.00")
        assert statement.expenses_total == Decimal("800.00")
        assert statement.net_payable == Decimal("8700.00")
        kinds = set(statement.lines.values_list("line_type", flat=True))
        assert kinds == {"RENT", "MANAGEMENT_FEE", "EXPENSE"}

    def test_an_invoice_paid_outside_the_period_is_excluded(self, world, fin):
        statement = services.generate_owner_statement(
            actor=fin, owner_contact=world["landlord"],
            period_start="2026-02-01", period_end="2026-02-28",
        )
        assert statement.gross_rent_collected == Decimal("0")

    def test_regenerating_the_draft_does_not_double_bill_the_expense(self, world, fin):
        first = services.generate_owner_statement(
            actor=fin, owner_contact=world["landlord"],
            period_start="2026-01-01", period_end="2026-01-31",
        )
        second = services.generate_owner_statement(
            actor=fin, owner_contact=world["landlord"],
            period_start="2026-01-01", period_end="2026-01-31",
        )
        assert first.pk == second.pk  # regenerated in place
        assert second.expenses_total == Decimal("800.00")
        assert second.lines.filter(line_type="EXPENSE").count() == 1

    def test_an_issued_statement_blocks_the_period(self, world, fin):
        statement = services.generate_owner_statement(
            actor=fin, owner_contact=world["landlord"],
            period_start="2026-01-01", period_end="2026-01-31",
        )
        services.issue_owner_statement(statement, actor=fin)
        with pytest.raises(ValidationError, match="already covers"):
            services.generate_owner_statement(
                actor=fin, owner_contact=world["landlord"],
                period_start="2026-01-15", period_end="2026-02-15",
            )

    def test_issue_audits_and_paying_debits(self, world, fin):
        from apps.finance.models import AccountEntry
        from apps.platform.models import AuditEvent

        statement = services.generate_owner_statement(
            actor=fin, owner_contact=world["landlord"],
            period_start="2026-01-01", period_end="2026-01-31",
        )
        services.issue_owner_statement(statement, actor=fin)
        assert AuditEvent.objects.filter(
            entity_type="OWNER_STATEMENT", entity_id=statement.pk
        ).exists()
        services.mark_statement_paid(statement, actor=fin, account=world["account"])
        entry = AccountEntry.objects.get(reference_id=statement.pk)
        assert entry.entry_type == "DEBIT"
        assert entry.amount == statement.net_payable

    def test_a_voided_expense_cannot_be_on_a_statement_and_vice_versa(self, world, fin):
        statement = services.generate_owner_statement(
            actor=fin, owner_contact=world["landlord"],
            period_start="2026-01-01", period_end="2026-01-31",
        )
        with pytest.raises(ValidationError, match="regenerate the draft"):
            services.void_expense(world["expense"], actor=fin, reason="Mistake.")


class TestReconciliation:
    def test_the_walk_to_reconciled(self, world, fin):
        recon = services.start_reconciliation(
            actor=fin, account=world["account"],
            period_start="2026-01-01", period_end="2026-01-31",
            opening_balance=Decimal("0"),
            closing_balance_statement=Decimal("10000"),  # the bank agrees with the ledger
        )
        assert recon.difference == Decimal("0")
        recon = services.complete_reconciliation(recon, actor=fin)
        assert recon.status == "RECONCILED"

    def test_a_discrepancy_is_a_status_not_a_crash(self, world, fin):
        recon = services.start_reconciliation(
            actor=fin, account=world["account"],
            period_start="2026-01-01", period_end="2026-01-31",
            opening_balance=Decimal("0"),
            closing_balance_statement=Decimal("9000"),  # bank says less
        )
        recon = services.complete_reconciliation(recon, actor=fin)
        assert recon.status == "DISCREPANCY"
        assert recon.difference == Decimal("-1000")

        # Correct with an adjustment, refresh, complete again.
        services.post_adjustment(
            actor=fin, account=world["account"], entry_type="DEBIT",
            amount=Decimal("1000"), description="Bank charge missed in January.",
            reference_type="RECONCILIATION", reference_id=recon.pk,
            posted_on="2026-01-31",  # the correction belongs to the period it fixes
        )
        recon = services.complete_reconciliation(recon, actor=fin)
        assert recon.status == "RECONCILED"
