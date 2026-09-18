"""Database-level invariants.

The point of a schema-first pass: every constraint architecture.md specifies should be
provable without a service or an endpoint. Each test drives the database straight to the
edge and asserts it refuses.

Grows one section per roadmap phase.
"""
import pytest
from django.db import IntegrityError, transaction

from apps.contacts.models import Consent, Contact, ContactRole
from apps.identity.models import User


def make_contact(**kwargs):
    kwargs.setdefault("contact_type", Contact.ContactType.PERSON)
    kwargs.setdefault("first_name", "Sam")
    kwargs.setdefault("last_name", "Rivera")
    return Contact.objects.create(**kwargs)


# --- identity (§4) ----------------------------------------------------------


def test_user_email_is_unique_among_live_users(db):
    User.objects.create_user(email="dup@acme.test", password="x", first_name="A", last_name="B")
    with pytest.raises(IntegrityError):
        User.objects.create_user(
            email="dup@acme.test", password="x", first_name="C", last_name="D"
        )


def test_soft_deleting_a_user_frees_their_email(db):
    """§4 makes the email index partial (WHERE deleted_at IS NULL) precisely for this."""
    from django.utils import timezone

    first = User.objects.create_user(
        email="reuse@acme.test", password="x", first_name="A", last_name="B"
    )
    first.deleted_at = timezone.now()
    first.save(update_fields=["deleted_at"])

    second = User.objects.create_user(
        email="reuse@acme.test", password="x", first_name="C", last_name="D"
    )
    assert second.pk != first.pk


def test_role_codes_are_unique(db, roles):
    from apps.identity.models import Role

    assert set(roles) == {
        "super_admin",
        "owner",
        "manager",
        "agent",
        "property_manager",
        "marketing",
        "finance",
        "portal",
    }
    with pytest.raises(IntegrityError):
        Role.objects.create(name="Dup", code="agent", data_scope=Role.DataScope.OWN)


# --- contacts (§5) ----------------------------------------------------------


def test_a_contact_cannot_hold_the_same_role_twice(db):
    """UNIQUE(contact_id, role) — but a contact may hold several *different* roles."""
    contact = make_contact()
    ContactRole.objects.create(contact=contact, role=ContactRole.Role.BUYER)
    ContactRole.objects.create(contact=contact, role=ContactRole.Role.LANDLORD)

    with pytest.raises(IntegrityError):
        ContactRole.objects.create(contact=contact, role=ContactRole.Role.BUYER)


def test_soft_deleted_contact_keeps_its_rows(db):
    """Soft delete is a flag, not a cascade — child rows must survive for the audit trail."""
    from django.utils import timezone

    contact = make_contact()
    Consent.objects.create(
        contact=contact,
        channel=Consent.Channel.EMAIL,
        status=Consent.Status.OPTED_IN,
        source="web-form",
    )
    contact.deleted_at = timezone.now()
    contact.save(update_fields=["deleted_at"])

    assert Consent.objects.filter(contact=contact).count() == 1


def test_a_contact_in_use_by_a_portal_profile_cannot_be_hard_deleted(db, make_user):
    """PROTECT on identity_portal_profile.contact (§2: PROTECT on historical/legal FKs)."""
    from apps.identity.models import PortalProfile

    contact = make_contact()
    PortalProfile.objects.create(
        user=make_user(),
        contact=contact,
        portal_type=PortalProfile.PortalType.TENANT,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        contact.delete()


# --- inventory (§8) ---------------------------------------------------------


def test_unit_numbers_are_unique_within_a_property(make_property):
    from apps.inventory.models import Unit

    prop = make_property(is_multi_unit=True)
    Unit.objects.create(property=prop, unit_number="101", status=Unit.Status.AVAILABLE)
    with pytest.raises(IntegrityError):
        Unit.objects.create(property=prop, unit_number="101", status=Unit.Status.AVAILABLE)


def test_retiring_a_unit_frees_its_number(make_property):
    """Partial unique index: (property, unit_number) WHERE deleted_at IS NULL."""
    from django.utils import timezone

    from apps.inventory.models import Unit

    prop = make_property(is_multi_unit=True)
    old = Unit.objects.create(property=prop, unit_number="101", status=Unit.Status.AVAILABLE)
    old.deleted_at = timezone.now()
    old.save(update_fields=["deleted_at"])

    reused = Unit.objects.create(property=prop, unit_number="101", status=Unit.Status.AVAILABLE)
    assert reused.pk != old.pk


def test_status_history_targets_exactly_one_of_property_or_unit(make_property, make_user):
    """§8: "Exactly one of property_id/unit_id is set" — neither both nor neither."""
    from apps.inventory.models import PropertyStatusHistory, Unit

    prop = make_property()
    unit = Unit.objects.create(property=prop, unit_number="1", status=Unit.Status.AVAILABLE)
    user = make_user()

    # Both set -> rejected.
    with pytest.raises(IntegrityError), transaction.atomic():
        PropertyStatusHistory.objects.create(
            property=prop, unit=unit, to_status="AVAILABLE", changed_by=user
        )

    # Neither set -> rejected.
    with pytest.raises(IntegrityError), transaction.atomic():
        PropertyStatusHistory.objects.create(to_status="AVAILABLE", changed_by=user)

    # Exactly one -> accepted.
    assert PropertyStatusHistory.objects.create(
        property=prop, to_status="AVAILABLE", changed_by=user
    ).pk


@pytest.mark.parametrize("pct", ["0", "-5", "100.01", "150"])
def test_ownership_percentage_must_be_within_zero_to_one_hundred(make_property, pct):
    from decimal import Decimal

    from apps.inventory.models import PropertyOwner

    prop = make_property()
    with pytest.raises(IntegrityError), transaction.atomic():
        PropertyOwner.objects.create(
            property=prop,
            contact=make_contact(),
            ownership_percentage=Decimal(pct),
            is_primary_owner=True,
            start_date="2026-01-01",
        )


def test_media_needs_a_target_and_a_blob_reference(make_property):
    from apps.inventory.models import Media

    prop = make_property()

    # No target at all -> rejected.
    with pytest.raises(IntegrityError), transaction.atomic():
        Media.objects.create(
            media_type=Media.MediaType.PHOTO, storage_key="media/x.jpg"
        )

    # Target but no blob reference -> rejected (widens to "file or storage_key" in 0002).
    with pytest.raises(IntegrityError), transaction.atomic():
        Media.objects.create(property=prop, media_type=Media.MediaType.PHOTO)

    assert Media.objects.create(
        property=prop, media_type=Media.MediaType.PHOTO, storage_key="media/x.jpg"
    ).pk


def test_listing_reference_is_unique_among_live_listings(make_property):
    from django.utils import timezone

    from apps.inventory.models import Listing

    prop = make_property()

    def listing(**kw):
        return Listing.objects.create(
            property=prop,
            reference_code="LST-0001",
            listing_type=Listing.ListingType.SALE,
            title="A listing",
            status=Listing.Status.ACTIVE,
            **kw,
        )

    first = listing()
    with pytest.raises(IntegrityError), transaction.atomic():
        listing()

    first.deleted_at = timezone.now()
    first.save(update_fields=["deleted_at"])
    assert listing().pk


# --- crm (§6, §7, §9) -------------------------------------------------------


def test_a_routing_rule_targets_exactly_one_of_user_or_team(db, make_user, team):
    """§7: exactly one of assign_to_user / assign_to_team. Neither can never assign;
    both has no defined meaning."""
    from apps.crm.models import LeadRoutingRule

    with pytest.raises(IntegrityError), transaction.atomic():
        LeadRoutingRule.objects.create(
            name="both", priority=1, assign_to_user=make_user(), assign_to_team=team
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        LeadRoutingRule.objects.create(name="neither", priority=2)

    assert LeadRoutingRule.objects.create(
        name="team only", priority=3, assign_to_team=team, round_robin=True
    ).pk


def test_a_lost_deal_must_carry_a_reason(db, make_deal):
    """Mandatory loss reason (SRS 3.4.5). The spec permits service-layer enforcement; a
    CHECK is stronger because imports and data migrations cannot route around it."""
    from apps.crm.models import Deal

    with pytest.raises(IntegrityError), transaction.atomic():
        make_deal(status=Deal.Status.LOST)

    assert make_deal(status=Deal.Status.LOST, lost_reason="Bought elsewhere").pk
    assert make_deal(status=Deal.Status.OPEN).pk  # OPEN needs no reason


def test_deal_reference_is_unique_among_live_deals(db, make_deal):
    from django.utils import timezone

    first = make_deal(reference_code="DL-0001")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_deal(reference_code="DL-0001")

    first.deleted_at = timezone.now()
    first.save(update_fields=["deleted_at"])
    assert make_deal(reference_code="DL-0001").pk


def test_a_viewing_must_end_after_it_starts(db, make_deal, make_property, make_user):
    from datetime import timedelta

    from django.utils import timezone

    from apps.crm.models import Viewing

    start = timezone.now()
    common = dict(
        property=make_property(),
        agent=make_user("agent"),
        contact=make_contact(),
        deal=make_deal(),
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Viewing.objects.create(
            scheduled_start=start, scheduled_end=start - timedelta(hours=1), **common
        )

    assert Viewing.objects.create(
        scheduled_start=start, scheduled_end=start + timedelta(hours=1), **common
    ).pk


def test_a_stage_cannot_be_both_won_and_lost(db, pipeline):
    from apps.crm.models import PipelineStage

    with pytest.raises(IntegrityError), transaction.atomic():
        PipelineStage.objects.create(
            pipeline=pipeline, name="Impossible", code="IMP", sort_order=99,
            probability=50, is_won=True, is_lost=True,
        )


def test_a_closing_checklist_needs_a_deal_or_a_transaction(db, make_deal):
    from apps.crm.models import ClosingChecklist

    with pytest.raises(IntegrityError), transaction.atomic():
        ClosingChecklist.objects.create(
            checklist_type=ClosingChecklist.ChecklistType.SALE, name="Orphan"
        )

    assert ClosingChecklist.objects.create(
        deal=make_deal(), checklist_type=ClosingChecklist.ChecklistType.SALE, name="Attached"
    ).pk


def test_campaign_metrics_are_one_row_per_campaign_per_day(db, make_user):
    """Not in the spec's constraint list, but a daily rollup that can be inserted twice
    silently double-counts spend and conversions on a job re-run."""
    from apps.crm.models import Campaign, CampaignMetric

    campaign = Campaign.objects.create(
        name="Spring", campaign_type="PPC", start_date="2026-03-01",
        status="ACTIVE", owner=make_user(),
    )
    CampaignMetric.objects.create(campaign=campaign, metric_date="2026-03-02", cost=100)
    with pytest.raises(IntegrityError):
        CampaignMetric.objects.create(campaign=campaign, metric_date="2026-03-02", cost=100)


# --- property_ops (§10, §14) ------------------------------------------------


def test_two_active_leases_cannot_overlap_on_the_same_property(make_property, make_lease):
    """The exclusion constraint — double-letting must be impossible, not merely unlikely."""
    prop = make_property()
    make_lease(property=prop, start_date="2026-01-01", end_date="2026-06-30")

    with pytest.raises(IntegrityError), transaction.atomic():
        make_lease(property=prop, start_date="2026-06-01", end_date="2026-12-31")


def test_leases_that_merely_touch_at_the_boundary_still_collide(make_property, make_lease):
    """The range is inclusive on both ends: one lease ending on the 30th and another
    starting on the 30th genuinely contend for that day."""
    prop = make_property()
    make_lease(property=prop, start_date="2026-01-01", end_date="2026-06-30")

    with pytest.raises(IntegrityError), transaction.atomic():
        make_lease(property=prop, start_date="2026-06-30", end_date="2026-12-31")


def test_consecutive_leases_on_the_same_property_are_allowed(make_property, make_lease):
    prop = make_property()
    make_lease(property=prop, start_date="2026-01-01", end_date="2026-06-30")
    assert make_lease(property=prop, start_date="2026-07-01", end_date="2026-12-31").pk


def test_a_draft_lease_may_overlap_an_active_one(make_property, make_lease):
    """Only occupying statuses participate — otherwise you could never draft a successor
    lease while the current one is still running."""
    from apps.property_ops.models import Lease

    prop = make_property()
    make_lease(property=prop, start_date="2026-01-01", end_date="2026-12-31")
    assert make_lease(
        property=prop, start_date="2026-06-01", end_date="2027-05-31",
        status=Lease.Status.DRAFT,
    ).pk


def test_different_units_of_one_property_can_be_let_simultaneously(make_property, make_lease):
    from apps.inventory.models import Unit

    prop = make_property(is_multi_unit=True)
    a = Unit.objects.create(property=prop, unit_number="101", status=Unit.Status.AVAILABLE)
    b = Unit.objects.create(property=prop, unit_number="102", status=Unit.Status.AVAILABLE)

    make_lease(property=prop, unit=a)
    assert make_lease(property=prop, unit=b).pk


def test_the_same_unit_cannot_be_let_twice_over(make_property, make_lease):
    from apps.inventory.models import Unit

    prop = make_property(is_multi_unit=True)
    unit = Unit.objects.create(property=prop, unit_number="101", status=Unit.Status.AVAILABLE)

    make_lease(property=prop, unit=unit, start_date="2026-01-01", end_date="2026-06-30")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_lease(property=prop, unit=unit, start_date="2026-03-01", end_date="2026-09-30")


def test_a_lease_must_end_after_it_starts(make_lease):
    with pytest.raises(IntegrityError), transaction.atomic():
        make_lease(start_date="2026-06-01", end_date="2026-01-01")


def test_deposit_refunds_and_deductions_cannot_exceed_the_amount_held(make_lease):
    from apps.property_ops.models import Deposit

    lease = make_lease()
    with pytest.raises(IntegrityError), transaction.atomic():
        Deposit.objects.create(
            lease=lease, amount=10000, held_amount=10000,
            refunded_amount=8000, deducted_amount=5000,
        )

    assert Deposit.objects.create(
        lease=lease, amount=10000, held_amount=10000,
        refunded_amount=6000, deducted_amount=4000,
    ).pk


def test_a_maintenance_request_needs_a_reporter(make_property):
    from apps.property_ops.models import MaintenanceRequest

    with pytest.raises(IntegrityError), transaction.atomic():
        MaintenanceRequest.objects.create(
            property=make_property(), title="Leak", description="Kitchen tap"
        )


def test_one_vendor_profile_per_contact(db):
    from apps.property_ops.models import Vendor

    contact = make_contact()
    Vendor.objects.create(contact=contact, service_category="Plumbing")
    with pytest.raises(IntegrityError):
        Vendor.objects.create(contact=contact, service_category="Electrical")


# --- finance (§11) ----------------------------------------------------------


@pytest.fixture
def account(db):
    from apps.finance.models import Account

    return Account.objects.create(
        name="Operating", account_type=Account.AccountType.OPERATING_ACCOUNT, currency="AED"
    )


def make_invoice(user, **kwargs):
    from apps.finance.models import Invoice

    kwargs.setdefault("invoice_number", f"INV-{Invoice.objects.count() + 1:05d}")
    kwargs.setdefault("contact", make_contact())
    kwargs.setdefault("invoice_type", Invoice.InvoiceType.RENT)
    kwargs.setdefault("issue_date", "2026-01-01")
    kwargs.setdefault("due_date", "2026-02-01")
    kwargs.setdefault("subtotal", 1000)
    kwargs.setdefault("total_amount", 1000)
    kwargs.setdefault("amount_paid", 0)
    kwargs.setdefault("balance_due", kwargs["total_amount"] - kwargs["amount_paid"])
    kwargs.setdefault("currency", "AED")
    kwargs.setdefault("created_by", user)
    return Invoice.objects.create(**kwargs)


def test_invoice_balance_must_equal_total_minus_paid(db, make_user):
    """The arithmetic is held by a CHECK. Keeping amount_paid *true* to the allocations is
    the allocation service's job — see the finance module docstring."""
    user = make_user()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_invoice(user, total_amount=1000, amount_paid=200, balance_due=900)

    assert make_invoice(user, total_amount=1000, amount_paid=200, balance_due=800).pk


def test_invoice_total_cannot_be_negative(db, make_user):
    user = make_user()
    with pytest.raises(IntegrityError), transaction.atomic():
        make_invoice(user, subtotal=-5, total_amount=-5, amount_paid=0, balance_due=-5)


def test_a_payment_must_be_positive(db, make_user, account):
    from apps.finance.models import Payment

    def payment(amount):
        return Payment.objects.create(
            payment_reference=f"PAY-{amount}", payer=make_contact(), account=account,
            amount=amount, currency="AED",
            payment_method=Payment.PaymentMethod.BANK_TRANSFER,
            payment_date="2026-01-15", recorded_by=make_user(),
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        payment(0)
    with pytest.raises(IntegrityError), transaction.atomic():
        payment(-100)
    assert payment(100).pk


def test_one_payment_hits_an_invoice_at_most_once(db, make_user, account):
    """Keeps SUM(allocations) unambiguous — a top-up adjusts the existing row."""
    from apps.finance.models import Payment, PaymentAllocation

    user = make_user()
    invoice = make_invoice(user)
    payment = Payment.objects.create(
        payment_reference="PAY-DUP", payer=make_contact(), account=account,
        amount=1000, currency="AED", payment_method=Payment.PaymentMethod.CASH,
        payment_date="2026-01-15", recorded_by=user,
    )
    PaymentAllocation.objects.create(payment=payment, invoice=invoice, allocated_amount=400)
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentAllocation.objects.create(payment=payment, invoice=invoice, allocated_amount=600)


def test_an_allocation_must_be_positive(db, make_user, account):
    from apps.finance.models import Payment, PaymentAllocation

    user = make_user()
    payment = Payment.objects.create(
        payment_reference="PAY-NEG", payer=make_contact(), account=account,
        amount=1000, currency="AED", payment_method=Payment.PaymentMethod.CASH,
        payment_date="2026-01-15", recorded_by=user,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PaymentAllocation.objects.create(
            payment=payment, invoice=make_invoice(user), allocated_amount=0
        )


def test_a_commission_hangs_off_a_transaction_or_a_lease_but_not_both(
    db, make_user, make_lease, make_deal, make_property
):
    """§11 is explicit: a rental-only commission must never be forced to invent a fake
    transaction to hang from."""
    from apps.crm.models import Transaction
    from apps.finance.models import Commission, CommissionPlan

    agent = make_user("agent")
    plan = CommissionPlan.objects.create(
        name="Standard 2%", plan_type=CommissionPlan.PlanType.FLAT_PERCENT,
        applies_to=CommissionPlan.AppliesTo.BOTH, base=CommissionPlan.Base.GROSS_AMOUNT,
        rate=2,
    )
    txn = Transaction.objects.create(
        property=make_property(), transaction_type=Transaction.TransactionType.SALE,
        reference_code="TXN-0001", gross_amount=1000000, currency="AED",
        transaction_date="2026-01-01",
    )
    lease = make_lease()
    common = dict(
        agent=agent, commission_plan=plan, gross_commission=20000, net_commission=18000
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Commission.objects.create(transaction=txn, lease=lease, **common)
    with pytest.raises(IntegrityError), transaction.atomic():
        Commission.objects.create(**common)

    assert Commission.objects.create(transaction=txn, **common).pk
    assert Commission.objects.create(lease=lease, **common).pk


def test_a_commission_split_names_exactly_one_recipient(db, make_user, make_property):
    from apps.crm.models import Transaction
    from apps.finance.models import Commission, CommissionPlan, CommissionSplit

    plan = CommissionPlan.objects.create(
        name="Flat", plan_type=CommissionPlan.PlanType.FLAT_PERCENT,
        applies_to=CommissionPlan.AppliesTo.SALE, base=CommissionPlan.Base.GROSS_AMOUNT, rate=2,
    )
    commission = Commission.objects.create(
        transaction=Transaction.objects.create(
            property=make_property(), transaction_type=Transaction.TransactionType.SALE,
            reference_code="TXN-SPLIT", gross_amount=1000000, currency="AED",
            transaction_date="2026-01-01",
        ),
        agent=make_user("agent"), commission_plan=plan,
        gross_commission=20000, net_commission=18000,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        CommissionSplit.objects.create(
            commission=commission, recipient_type=CommissionSplit.RecipientType.AGENT,
            recipient_user=make_user(), recipient_contact=make_contact(),
            percentage=50, amount=9000,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        CommissionSplit.objects.create(
            commission=commission, recipient_type=CommissionSplit.RecipientType.AGENT,
            percentage=50, amount=9000,
        )
    assert CommissionSplit.objects.create(
        commission=commission, recipient_type=CommissionSplit.RecipientType.REFERRAL_EXTERNAL,
        recipient_contact=make_contact(), percentage=50, amount=9000,
    ).pk


def test_an_invoiced_milestone_must_carry_its_invoice(db, make_user, make_property):
    from apps.crm.models import Transaction
    from apps.finance.models import InstallmentMilestone, InstallmentPlan

    user = make_user()
    plan = InstallmentPlan.objects.create(
        transaction=Transaction.objects.create(
            property=make_property(), transaction_type=Transaction.TransactionType.SALE,
            reference_code="TXN-PLAN", gross_amount=1000000, currency="AED",
            transaction_date="2026-01-01",
        ),
        name="Off-plan 60/40", currency="AED", total_amount=1000000,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        InstallmentMilestone.objects.create(
            plan=plan, label="On booking", due_date="2026-02-01", amount=600000,
            status=InstallmentMilestone.Status.INVOICED,
        )

    assert InstallmentMilestone.objects.create(
        plan=plan, label="On booking", due_date="2026-02-01", amount=600000,
        status=InstallmentMilestone.Status.INVOICED, invoice=make_invoice(user),
    ).pk


def test_a_reconciliation_cannot_claim_to_balance_while_showing_a_difference(db, account):
    """§11: RECONCILED requires difference = 0. This is precisely the state an audit looks
    for, so it is worth making unrepresentable."""
    from apps.finance.models import Reconciliation

    def recon(status, statement, system):
        return Reconciliation.objects.create(
            account=account, period_start="2026-01-01", period_end="2026-01-31",
            opening_balance=0, closing_balance_statement=statement,
            closing_balance_system=system, difference=statement - system, status=status,
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        recon(Reconciliation.Status.RECONCILED, 1000, 900)

    assert recon(Reconciliation.Status.DISCREPANCY, 1000, 900).pk
    assert recon(Reconciliation.Status.RECONCILED, 1000, 1000).pk


def test_reconciliation_difference_is_derived_not_free_form(db, account):
    from apps.finance.models import Reconciliation

    with pytest.raises(IntegrityError), transaction.atomic():
        Reconciliation.objects.create(
            account=account, period_start="2026-01-01", period_end="2026-01-31",
            opening_balance=0, closing_balance_statement=1000,
            closing_balance_system=900, difference=0,  # lies: should be 100
            status=Reconciliation.Status.OPEN,
        )


# --- SRS-driven schema amendments (v3.3) ------------------------------------


class TestDealStageHistory:
    """SRS 3.4.4 — "log a mandatory reason and next action when a deal moves stage or is
    marked Lost". architecture.md v3.2 defined history tables for leads and properties but
    none for deals, so the mandate had nowhere to land."""

    def test_a_stage_move_records_its_reason(self, db, make_deal, make_user, pipeline):
        from apps.crm.models import DealStageHistory

        deal = make_deal()
        stages = list(pipeline.stages.all())

        row = DealStageHistory.objects.create(
            deal=deal,
            from_stage=stages[0],
            to_stage=stages[1],
            changed_by=make_user(),
            reason="Client confirmed the viewing.",
            next_action="Send the offer letter",
        )
        assert row.pk and row.changed_at

    def test_an_empty_reason_is_refused(self, db, make_deal, make_user, pipeline):
        """"Mandatory" means a reason, not a placeholder. A NOT NULL column alone would
        accept the empty string."""
        from apps.crm.models import DealStageHistory

        stages = list(pipeline.stages.all())
        with pytest.raises(IntegrityError), transaction.atomic():
            DealStageHistory.objects.create(
                deal=make_deal(), from_stage=stages[0], to_stage=stages[1],
                changed_by=make_user(), reason="",
            )

    def test_the_first_move_may_have_no_from_stage(self, db, make_deal, make_user, pipeline):
        from apps.crm.models import DealStageHistory

        assert DealStageHistory.objects.create(
            deal=make_deal(), from_stage=None, to_stage=pipeline.stages.first(),
            changed_by=make_user(), reason="Created.",
        ).pk


class TestDealProperty:
    """SRS 3.4.7 — "linking one or more properties to an opportunity, and one opportunity to
    a specific matched property once identified"."""

    def test_a_deal_can_reference_several_properties(self, db, make_deal, make_property):
        from apps.crm.models import DealProperty

        deal = make_deal()
        for _ in range(3):
            DealProperty.objects.create(deal=deal, property=make_property())
        assert deal.deal_properties.count() == 3

    def test_the_same_property_cannot_be_linked_twice(self, db, make_deal, make_property):
        from apps.crm.models import DealProperty

        deal, prop = make_deal(), make_property()
        DealProperty.objects.create(deal=deal, property=prop)
        with pytest.raises(IntegrityError), transaction.atomic():
            DealProperty.objects.create(deal=deal, property=prop)

    def test_only_one_property_can_be_the_matched_one(self, db, make_deal, make_property):
        """"one opportunity to a specific matched property" — one, not several."""
        from apps.crm.models import DealProperty

        deal = make_deal()
        DealProperty.objects.create(deal=deal, property=make_property(), is_primary=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            DealProperty.objects.create(
                deal=deal, property=make_property(), is_primary=True
            )

    def test_two_deals_may_each_have_their_own_primary(self, db, make_deal, make_property):
        from apps.crm.models import DealProperty

        for _ in range(2):
            DealProperty.objects.create(
                deal=make_deal(), property=make_property(), is_primary=True
            )


def test_a_listing_can_carry_a_co_listing_agent(db, make_property, make_user):
    """SRS 3.3.5 — "multi-agent/co-listing assignment"."""
    from apps.inventory.models import Listing

    listing = Listing.objects.create(
        property=make_property(),
        reference_code="LST-CO-1",
        listing_type=Listing.ListingType.SALE,
        title="Co-listed villa",
        status=Listing.Status.ACTIVE,
        assigned_agent=make_user("agent"),
        co_listing_agent=make_user("agent"),
    )
    assert listing.assigned_agent_id != listing.co_listing_agent_id


def test_a_listing_cannot_be_co_listed_with_its_own_agent(db, make_property, make_user):
    """Co-listing means a *second* agent."""
    from apps.inventory.models import Listing

    agent = make_user("agent")
    with pytest.raises(IntegrityError), transaction.atomic():
        Listing.objects.create(
            property=make_property(),
            reference_code="LST-CO-2",
            listing_type=Listing.ListingType.SALE,
            title="Self co-listed",
            status=Listing.Status.ACTIVE,
            assigned_agent=agent,
            co_listing_agent=agent,
        )


def test_a_listing_with_no_co_listing_agent_is_fine(db, make_property, make_user):
    """The constraint is IS DISTINCT FROM in spirit: the common case is one agent and a null
    co-lister, and a NOT (a = b) check must not trip on the null."""
    from apps.inventory.models import Listing

    assert Listing.objects.create(
        property=make_property(),
        reference_code="LST-CO-3",
        listing_type=Listing.ListingType.SALE,
        title="Single agent",
        status=Listing.Status.ACTIVE,
        assigned_agent=make_user("agent"),
    ).pk


def test_owner_records_carry_mandate_terms(db, make_property):
    """SRS 3.3.9 — "owner records per listing (linking to a Contact) with commission/mandate
    terms". The terms belong to the ownership agreement, so they survive a listing being
    withdrawn and relisted."""
    from apps.inventory.models import PropertyOwner

    owner = PropertyOwner.objects.create(
        property=make_property(),
        contact=make_contact(),
        ownership_percentage=100,
        is_primary_owner=True,
        start_date="2026-01-01",
        commission_rate=2.5,
        mandate_type=PropertyOwner.MandateType.EXCLUSIVE,
        mandate_expires_at="2026-12-31",
    )
    assert owner.mandate_type == "EXCLUSIVE"


def test_lead_carries_the_sla_and_capture_flags(db, make_contact):
    """SRS 3.1.9, 3.1.10, 3.1.12 — architecture.md v3.2 omitted all five fields, so none of
    those three requirements had anywhere to record their state."""
    from apps.crm.models import Lead

    lead = Lead.objects.create(
        contact=make_contact(), lead_type=Lead.LeadType.BUY, title="Villa enquiry"
    )
    assert lead.sla_due_at is None
    assert lead.sla_breached is False
    assert lead.first_response_at is None
    assert lead.acknowledged is False
    assert lead.is_possible_duplicate is False
