"""
The mandatory tenancy isolation test (plan §8).

Proves that Postgres RLS — not just the app-layer manager — prevents one tenant
from seeing another's rows, even when the app-layer filter is deliberately bypassed.
Requires the app to connect as a NON-superuser role (superusers bypass RLS).
"""
import pytest
from django.db import connection

from apps.contacts.models import Contact
from apps.core.tenancy import tenant_context

pytestmark = pytest.mark.django_db


def _make_contact(tenant, name):
    with tenant_context(tenant.id):
        return Contact.objects.create(tenant=tenant, full_name=name)


def test_app_layer_scoping_filters_by_tenant(tenant, other_tenant):
    _make_contact(tenant, "Acme Person")
    _make_contact(other_tenant, "Globex Person")

    with tenant_context(tenant.id):
        names = list(Contact.objects.values_list("full_name", flat=True))
    assert names == ["Acme Person"]


def test_rls_blocks_bypass_manager(tenant, other_tenant):
    """all_objects skips the app filter; RLS must STILL hide the other tenant."""
    _make_contact(tenant, "Acme Person")
    globex = _make_contact(other_tenant, "Globex Person")

    with tenant_context(tenant.id):
        # Even addressing the row by id, RLS denies it under tenant A's session.
        assert not Contact.all_objects.filter(id=globex.id).exists()
        assert Contact.all_objects.count() == 1


def test_rls_enforced_in_raw_sql(tenant, other_tenant):
    _make_contact(tenant, "Acme Person")
    _make_contact(other_tenant, "Globex Person")

    with tenant_context(other_tenant.id):
        with connection.cursor() as cur:
            cur.execute("SELECT full_name FROM contacts_contact;")
            rows = [r[0] for r in cur.fetchall()]
    assert rows == ["Globex Person"]


def test_insert_without_tenant_context_is_rejected(tenant):
    """WITH CHECK on the policy blocks inserts when no tenant is bound."""
    from django.db import Error, transaction

    # Wrap in a savepoint so the expected RLS error rolls back cleanly and leaves
    # the outer test transaction usable.
    with pytest.raises(Error):
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO contacts_contact "
                    "(id, tenant_id, kind, full_name, company_name, email, phone, "
                    " extra_emails, extra_phones, source, tags, location_preference, "
                    " is_vip, custom_fields, is_deleted, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), %s, 'person', 'X', '', '', '', "
                    " '[]', '[]', '', '[]', '', false, '{}', false, now(), now());",
                    [str(tenant.id)],
                )
