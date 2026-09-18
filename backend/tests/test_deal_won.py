"""`mark_deal_won` — §1.2's orchestration keystone, and the three properties that make it
safe to fire on every stage churn: idempotent, degrading, quietly-refusing."""
from decimal import Decimal

import pytest

from apps.crm import services
from apps.crm.models import Transaction
from apps.finance.models import Commission, CommissionPlan


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def won_ready_deal(agent_user, pipeline, make_contact, make_property, make_user):
    """A SALE deal with a linked property, one stage away from Won."""
    deal = services.create_deal(
        actor=agent_user, pipeline=pipeline, primary_contact=make_contact(),
        owner=agent_user, title="Villa sale", deal_type="SALE",
        estimated_value=Decimal("2000000"), currency="AED",
    )
    services.link_property(
        deal, property=make_property(managed_by=make_user("property_manager")),
        is_primary=True, actor=agent_user,
    )
    return deal


@pytest.fixture
def flat_plan(db):
    return CommissionPlan.objects.create(
        name="Standard 2%", plan_type=CommissionPlan.PlanType.FLAT_PERCENT,
        applies_to=CommissionPlan.AppliesTo.SALE, base=CommissionPlan.Base.GROSS_AMOUNT,
        rate=Decimal("2"),
    )


def win(deal, actor):
    stages = {s.code: s for s in deal.pipeline.stages.all()}
    return services.move_stage(deal, stages["WON"], actor=actor, reason="Contract signed.")


class TestMarkDealWon:
    def test_winning_creates_the_transaction_and_commission(
        self, db, won_ready_deal, agent_user, flat_plan
    ):
        win(won_ready_deal, agent_user)
        txn = Transaction.objects.get(deal=won_ready_deal)
        assert txn.reference_code.startswith("TXN-")
        assert txn.transaction_type == Transaction.TransactionType.SALE
        assert txn.gross_amount == Decimal("2000000")

        commission = Commission.objects.get(transaction=txn)
        assert commission.gross_commission == Decimal("40000")  # 2% of 2M
        assert commission.agent_id == agent_user.pk
        assert commission.status == Commission.Status.CALCULATED

    def test_stage_churn_through_won_mints_one_transaction(
        self, db, won_ready_deal, agent_user, flat_plan
    ):
        """A deal dragged out of Won and back in must not create a second sale."""
        win(won_ready_deal, agent_user)
        stages = {s.code: s for s in won_ready_deal.pipeline.stages.all()}
        services.move_stage(
            won_ready_deal, stages["VIEWING"], actor=agent_user,
            reason="Reopened.", next_action="Renegotiate",
        )
        win(won_ready_deal, agent_user)
        assert Transaction.objects.filter(deal=won_ready_deal).count() == 1
        assert Commission.objects.count() == 1

    def test_no_commission_plan_defers_but_never_blocks_the_move(
        self, db, won_ready_deal, agent_user
    ):
        """Back-office configuration must not hold a stage move hostage."""
        assert not CommissionPlan.objects.exists()
        deal = win(won_ready_deal, agent_user)
        assert deal.status == "WON"
        assert Transaction.objects.filter(deal=won_ready_deal).exists()
        assert not Commission.objects.exists()  # deferred, creatable later

    def test_ambiguous_plans_also_defer(self, db, won_ready_deal, agent_user, flat_plan):
        CommissionPlan.objects.create(
            name="Other 3%", plan_type=CommissionPlan.PlanType.FLAT_PERCENT,
            applies_to=CommissionPlan.AppliesTo.BOTH, base=CommissionPlan.Base.GROSS_AMOUNT,
            rate=Decimal("3"),
        )
        win(won_ready_deal, agent_user)
        assert Transaction.objects.count() == 1
        assert not Commission.objects.exists()

    def test_a_deal_with_no_property_refuses_quietly(
        self, db, agent_user, pipeline, make_contact
    ):
        """crm_transaction.property is NOT NULL; blocking the move over a missing link would
        hold the sale hostage to data entry."""
        deal = services.create_deal(
            actor=agent_user, pipeline=pipeline, primary_contact=make_contact(),
            owner=agent_user, title="No property", deal_type="SALE",
            estimated_value=Decimal("1"), currency="AED",
        )
        won = win(deal, agent_user)
        assert won.status == "WON"
        assert not Transaction.objects.exists()

    def test_a_rental_deal_creates_the_transaction_but_no_lease(
        self, db, agent_user, pipeline, make_contact, make_property, make_user
    ):
        """Lease terms are a property manager's decision, not a derivation from an estimate
        — the lease follows via the explicit create-lease endpoint."""
        from apps.property_ops.models import Lease

        deal = services.create_deal(
            actor=agent_user, pipeline=pipeline, primary_contact=make_contact(),
            owner=agent_user, title="Letting", deal_type="RENT_OUT",
            estimated_value=Decimal("120000"), currency="AED",
        )
        services.link_property(
            deal, property=make_property(managed_by=make_user("property_manager")),
            is_primary=True, actor=agent_user,
        )
        win(deal, agent_user)
        txn = Transaction.objects.get(deal=deal)
        assert txn.transaction_type == Transaction.TransactionType.RENTAL
        assert not Lease.objects.exists()

    def test_create_lease_from_deal_builds_the_draft(
        self, db, agent_user, pipeline, make_contact, make_property, make_user
    ):
        from apps.inventory.models import PropertyOwner
        from apps.property_ops import services as po_services
        from apps.property_ops.models import Lease

        pm = make_user("property_manager")
        prop = make_property(managed_by=pm)
        landlord = make_contact()
        PropertyOwner.objects.create(
            property=prop, contact=landlord, ownership_percentage=100,
            is_primary_owner=True, start_date="2026-01-01",
        )
        deal = services.create_deal(
            actor=agent_user, pipeline=pipeline, primary_contact=make_contact(),
            owner=agent_user, title="Letting", deal_type="RENT_OUT",
            estimated_value=Decimal("120000"), currency="AED",
        )
        services.link_property(deal, property=prop, is_primary=True, actor=agent_user)
        txn = win(deal, agent_user) and Transaction.objects.get(deal=deal)

        lease = po_services.create_lease_from_deal(
            deal=deal, actor=pm, transaction=txn,
            start_date="2027-01-01", end_date="2027-12-31",
            rent_amount=Decimal("10000"),
            billing_frequency=Lease.BillingFrequency.MONTHLY,
            security_deposit=Decimal("10000"),
        )
        assert lease.status == Lease.Status.DRAFT
        assert lease.tenant_id == deal.primary_contact_id
        assert lease.landlord_id == landlord.pk
        assert lease.transaction_id == txn.pk
