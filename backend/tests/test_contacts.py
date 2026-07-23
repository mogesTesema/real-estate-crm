"""Contact dedupe + merge (SRS §3.1.3, §3.1.10, §3.2.7)."""
import pytest

from apps.contacts.models import Contact, ContactRole
from apps.contacts.services import find_duplicates, merge_contacts
from apps.core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


def test_find_duplicates_by_phone_and_email(tenant):
    with tenant_context(tenant.id):
        Contact.objects.create(
            tenant=tenant, full_name="Jane", email="jane@x.com", phone="555 0100"
        )
        # normalized phone match (whitespace stripped -> digits compared)
        assert find_duplicates(phone="(555) 0100").count() == 1
        # email match (case-insensitive/normalized)
        assert find_duplicates(email="JANE@x.com").count() == 1
        # no match
        assert find_duplicates(email="nobody@x.com").count() == 0


def test_merge_moves_roles_and_soft_deletes_duplicate(tenant):
    with tenant_context(tenant.id):
        survivor = Contact.objects.create(tenant=tenant, full_name="Jane", email="j@x.com")
        dup = Contact.objects.create(tenant=tenant, full_name="Jane", email="j2@x.com")
        ContactRole.objects.create(
            tenant=tenant, contact=survivor, role=ContactRole.RoleType.BUYER
        )
        ContactRole.objects.create(
            tenant=tenant, contact=dup, role=ContactRole.RoleType.SELLER
        )

        merge_contacts(survivor=survivor, duplicate=dup)

        survivor.refresh_from_db()
        assert set(survivor.roles) == {"buyer", "seller"}
        assert "j2@x.com" in survivor.extra_emails
        # duplicate is soft-deleted (hidden from default manager)
        assert not Contact.objects.filter(id=dup.id).exists()
        assert Contact.all_objects.get(id=dup.id).is_deleted
