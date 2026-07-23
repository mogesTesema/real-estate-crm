"""
Row-level scoping through the API (SRS §3.17.2, plan §2.2).

Agents see only their own leads; managers see their branch; owners see all — all
within tenant isolation.
"""
import pytest

from apps.core.tenancy import tenant_context
from apps.leads.models import Lead

pytestmark = pytest.mark.django_db


def _lead_for(tenant, agent, email):
    with tenant_context(tenant.id):
        return Lead.objects.create(
            tenant=tenant,
            lead_type=Lead.Type.BUY,
            email=email,
            assigned_agent=agent,
        )


def _ids(response):
    return {row["id"] for row in response.json()["results"]}


def test_agent_sees_only_own_leads(auth_api, tenant, agent1, agent2):
    la = _lead_for(tenant, agent1, "a@x.com")
    lb = _lead_for(tenant, agent2, "b@x.com")

    resp = auth_api(agent1).get("/api/v1/leads/")
    ids = _ids(resp)
    assert str(la.id) in ids
    assert str(lb.id) not in ids


def test_manager_sees_branch_leads(auth_api, tenant, manager, agent1, agent2):
    la = _lead_for(tenant, agent1, "a@x.com")
    lb = _lead_for(tenant, agent2, "b@x.com")

    resp = auth_api(manager).get("/api/v1/leads/")
    ids = _ids(resp)
    assert {str(la.id), str(lb.id)} <= ids


def test_owner_sees_all_leads(auth_api, tenant, owner, agent1):
    la = _lead_for(tenant, agent1, "a@x.com")
    resp = auth_api(owner).get("/api/v1/leads/")
    assert str(la.id) in _ids(resp)


def test_tenant_isolation_through_api(auth_api, tenant, other_tenant, owner, agent1):
    from apps.core.models import Role, User

    other_owner = User.objects.create_user(
        email="o@globex.test", password="x", tenant=other_tenant, role=Role.OWNER
    )
    la = _lead_for(tenant, agent1, "a@x.com")
    resp = auth_api(other_owner).get("/api/v1/leads/")
    assert str(la.id) not in _ids(resp)
