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
