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
