"""Shared pytest fixtures."""
import pytest
from rest_framework.test import APIClient

from apps.core.models import Branch, Company, Role, Tenant, User
from apps.core.tenancy import clear_current_tenant, tenant_context


@pytest.fixture(autouse=True)
def _reset_tenant_binding():
    """Ensure no tenant GUC leaks between tests (session SET is not rolled back)."""
    clear_current_tenant()
    yield
    clear_current_tenant()


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(name="Acme Realty", subdomain="acme")


@pytest.fixture
def other_tenant(db):
    return Tenant.objects.create(name="Globex Realty", subdomain="globex")


@pytest.fixture
def org(db, tenant):
    """A branch under the tenant, for scoping tests."""
    with tenant_context(tenant.id):
        company = Company.objects.create(tenant=tenant, name="Acme HQ")
        branch = Branch.objects.create(tenant=tenant, company=company, name="Central")
    return branch


@pytest.fixture
def owner(db, tenant):
    return User.objects.create_user(
        email="owner@acme.test", password="x", tenant=tenant, role=Role.OWNER
    )


@pytest.fixture
def manager(db, tenant, org):
    return User.objects.create_user(
        email="mgr@acme.test",
        password="x",
        tenant=tenant,
        role=Role.MANAGER,
        branch=org,
    )


@pytest.fixture
def agent1(db, tenant, org):
    return User.objects.create_user(
        email="a1@acme.test", password="x", tenant=tenant, role=Role.AGENT, branch=org
    )


@pytest.fixture
def agent2(db, tenant, org):
    return User.objects.create_user(
        email="a2@acme.test", password="x", tenant=tenant, role=Role.AGENT, branch=org
    )


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def auth_api(api):
    def _make(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    return _make
