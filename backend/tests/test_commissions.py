"""Commissions (SRS 3.6.4/3.6.5/3.15.6) and installment plans (3.6.3)."""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.finance import services
from apps.finance.models import (
    Account,
    AccountEntry,
    Commission,
    CommissionPlan,
    InstallmentMilestone,
)


@pytest.fixture
def fin(make_user):
    return make_user("finance")


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


def plan_factory(**kwargs):
    kwargs.setdefault("name", "Plan")
    kwargs.setdefault("plan_type", CommissionPlan.PlanType.FLAT_PERCENT)
    kwargs.setdefault("applies_to", CommissionPlan.AppliesTo.BOTH)
    kwargs.setdefault("base", CommissionPlan.Base.GROSS_AMOUNT)
    return CommissionPlan.objects.create(**kwargs)


class TestCalculation:
    def test_flat_percent(self, db, fin, make_transaction):
        plan = plan_factory(rate=Decimal("2"))
        commission = services.create_commission_for_transaction(
            actor=fin, transaction_obj=make_transaction(), plan=plan, agent=fin
        )
        assert commission.gross_commission == Decimal("20000.00")  # 2% of 1M

    def test_tiered_is_marginal_across_bands(self, db, fin, make_transaction):
        """Like income tax: each band's rate applies to the slice inside it. A cliff where
        one extra dirham cuts the whole commission teaches agents to game the price."""
        plan = plan_factory(
            plan_type=CommissionPlan.PlanType.TIERED,
            config={"tiers": [
                {"up_to": "500000", "rate": "3"},
                {"up_to": None, "rate": "1"},
            ]},
        )
        commission = services.create_commission_for_transaction(
            actor=fin, transaction_obj=make_transaction(), plan=plan, agent=fin
        )
        # 500k @ 3% + 500k @ 1% = 15000 + 5000
        assert commission.gross_commission == Decimal("20000.00")

    def test_tiered_refuses_descending_caps(self, db, fin, make_transaction):
        plan = plan_factory(
            plan_type=CommissionPlan.PlanType.TIERED,
            config={"tiers": [
                {"up_to": "500000", "rate": "3"},
                {"up_to": "400000", "rate": "1"},
            ]},
        )
        with pytest.raises(ValidationError, match="ascend"):
            services.create_commission_for_transaction(
                actor=fin, transaction_obj=make_transaction(), plan=plan, agent=fin
            )

    def test_rental_commission_needs_no_transaction(
        self, db, fin, make_user, make_property, make_contact
    ):
        """§1.2: never a fake crm transaction for a letting commission."""
        from apps.property_ops import services as po_services
        from apps.property_ops.models import Lease

        pm = make_user("property_manager")
        lease = po_services.create_lease(
            actor=pm, property=make_property(managed_by=pm), tenant=make_contact(),
            landlord=make_contact(), property_manager=pm,
            lease_type=Lease.LeaseType.RESIDENTIAL,
            start_date="2026-01-01", end_date="2026-12-31",
            rent_amount=Decimal("10000"),
            billing_frequency=Lease.BillingFrequency.MONTHLY,
            security_deposit=Decimal("0"),
        )
        plan = plan_factory(
            base=CommissionPlan.Base.RENT_PERIODS, rate=None,
            plan_type=CommissionPlan.PlanType.FLAT_PERCENT, config={"periods": 1},
        )
        plan.rate = Decimal("100")  # 100% of one period = one month's rent
        plan.save()
        commission = services.create_commission_for_lease(actor=fin, lease=lease, plan=plan)
        assert commission.lease_id == lease.pk
        assert commission.transaction_id is None
        assert commission.gross_commission == Decimal("10000.00")

    def test_vat_rides_on_top(self, db, fin, make_transaction):
        from apps.identity.models import Company

        company = Company.objects.first()
        company.settings = {"vat_rate": "5"}
        company.save(update_fields=["settings"])
        plan = plan_factory(rate=Decimal("2"))
        commission = services.create_commission_for_transaction(
            actor=fin, transaction_obj=make_transaction(), plan=plan, agent=fin
        )
        assert commission.tax_amount == Decimal("1000.00")  # 5% of 20000
        assert commission.net_commission == Decimal("20000.00")  # net excludes VAT


class TestSplits:
    @pytest.fixture
    def commission(self, db, fin, make_transaction):
        plan = plan_factory(rate=Decimal("2"))
        return services.create_commission_for_transaction(
            actor=fin, transaction_obj=make_transaction(), plan=plan, agent=fin
        )

    def test_split_plan_config_distributes_including_head_office(
        self, db, fin, agent_user, make_user, make_transaction
    ):
        """SRS 3.15.6 — the head-office share is a NAMED split row, not a deduction, so the
        sum-to-100 rule stays whole."""
        head_office = make_user("owner")
        plan = plan_factory(
            plan_type=CommissionPlan.PlanType.SPLIT,
            config={
                "rate": "2",
                "splits": [
                    {"recipient_type": "AGENT", "source": "primary_agent",
                     "percentage": "60"},
                    {"recipient_type": "BROKER",
                     "recipient_user_id": str(head_office.pk), "percentage": "40"},
                ],
            },
        )
        commission = services.create_commission_for_transaction(
            actor=fin, transaction_obj=make_transaction(), plan=plan, agent=agent_user
        )
        splits = {s.recipient_user_id: s for s in commission.splits.all()}
        assert splits[agent_user.pk].amount == Decimal("12000.00")
        assert splits[head_office.pk].amount == Decimal("8000.00")
        assert sum(s.amount for s in splits.values()) == commission.net_commission

    def test_rounding_residue_folds_into_the_largest(self, db, fin, agent_user, make_user,
                                                     commission):
        other = make_user("agent")
        services.set_commission_splits(
            commission,
            [
                {"recipient_type": "AGENT", "recipient_user": agent_user,
                 "percentage": Decimal("33.33")},
                {"recipient_type": "AGENT", "recipient_user": other,
                 "percentage": Decimal("33.33")},
                {"recipient_type": "AGENT", "recipient_user": fin,
                 "percentage": Decimal("33.34")},
            ],
            actor=fin,
        )
        total = sum(s.amount for s in commission.splits.all())
        assert total == commission.net_commission  # exactly — residue folded

    def test_wrong_recipient_typing_is_refused(self, db, fin, make_contact, commission):
        with pytest.raises(ValidationError, match="user, not a contact"):
            services.set_commission_splits(
                commission,
                [{"recipient_type": "AGENT", "recipient_contact": make_contact(),
                  "percentage": Decimal("100")}],
                actor=fin,
            )

    def test_approval_enforces_the_sum(self, db, fin, agent_user, commission):
        services.set_commission_splits(
            commission,
            [{"recipient_type": "AGENT", "recipient_user": agent_user,
              "percentage": Decimal("60")}],
            actor=fin,
        )
        services.submit_commission(commission, actor=fin)
        with pytest.raises(ValidationError, match="100%"):
            services.approve_commission(commission, actor=fin)

    def test_approve_notifies_and_audits_then_pay_debits(
        self, db, fin, agent_user, commission
    ):
        from apps.collaboration.models import Notification
        from apps.platform.models import AuditEvent

        services.submit_commission(commission, actor=fin)
        services.approve_commission(commission, actor=fin)
        assert AuditEvent.objects.filter(
            action=AuditEvent.Action.COMMISSION_APPROVED, entity_id=commission.pk
        ).exists()
        assert Notification.objects.filter(
            type="COMMISSION_APPROVED", recipient=commission.agent
        ).exists()

        account = services.create_account(
            actor=fin, name="Ops", account_type=Account.AccountType.OPERATING_ACCOUNT,
            currency="AED",
        )
        services.pay_commission(commission, actor=fin, account=account)
        entry = AccountEntry.objects.get(
            reference_type=AccountEntry.ReferenceType.COMMISSION,
            reference_id=commission.pk,
        )
        assert entry.entry_type == AccountEntry.EntryType.DEBIT
        assert entry.amount == commission.net_commission


class TestInstallments:
    @pytest.fixture
    def plan(self, db, fin, make_transaction):
        return services.create_installment_plan(
            actor=fin, transaction_obj=make_transaction(), name="Off-plan 2026",
            currency="AED", total_amount=Decimal("1000000"),
            milestones=[
                {"label": "Booking", "due_date": "2026-01-01", "percentage": "10"},
                {"label": "Construction", "due_date": "2026-06-01", "percentage": "40"},
                {"label": "Handover", "due_date": "2026-12-01", "percentage": "50"},
            ],
        )

    def test_percentages_resolve_to_amounts_summing_exactly(self, plan):
        amounts = list(plan.milestones.values_list("amount", flat=True))
        assert sum(amounts) == plan.total_amount

    def test_activation_enforces_the_sum(self, db, fin, plan):
        services.activate_installment_plan(plan, actor=fin)
        assert plan.status == "ACTIVE"

        broken = services.create_installment_plan(
            actor=fin, transaction_obj=plan.transaction, name="Broken",
            currency="AED", total_amount=Decimal("100"),
            milestones=[{"label": "Only half", "due_date": "2026-01-01", "amount": "50"}],
        )
        with pytest.raises(ValidationError, match="sum to"):
            services.activate_installment_plan(broken, actor=fin)

    def test_invoicing_a_milestone_and_paying_it_completes_the_plan(
        self, db, fin, plan, make_contact
    ):
        from apps.finance.models import Payment

        services.activate_installment_plan(plan, actor=fin)
        payer = plan.transaction.deal.primary_contact if plan.transaction.deal else make_contact()
        account = services.create_account(
            actor=fin, name="Ops", account_type=Account.AccountType.OPERATING_ACCOUNT,
            currency="AED",
        )
        for milestone in plan.milestones.all():
            invoice = services.invoice_milestone(milestone, actor=fin, contact=payer)
            services.record_payment(
                actor=fin, payer=payer, account=account, amount=invoice.total_amount,
                payment_method=Payment.PaymentMethod.BANK_TRANSFER,
                payment_date="2026-09-10",
                allocations=[{"invoice": invoice, "amount": invoice.total_amount}],
            )
        plan.refresh_from_db()
        assert plan.status == "COMPLETED"
        assert set(plan.milestones.values_list("status", flat=True)) == {
            InstallmentMilestone.Status.PAID
        }
