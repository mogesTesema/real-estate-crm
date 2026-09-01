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
