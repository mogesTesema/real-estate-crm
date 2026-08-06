"""Phase 1 close-out: auth reset/MFA, lead sources, geo filter, notifications."""
import pytest
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.core.models import Notification, Tenant
from apps.core.tenancy import tenant_context
from apps.leads.models import LeadSource
from apps.leads.services import capture_lead, route_lead
from apps.properties.models import Property

pytestmark = pytest.mark.django_db


def test_forgot_password_always_200(api, owner):
    res = api.post(
        "/api/v1/auth/forgot-password/",
        {"email": owner.email},
        format="json",
    )
    assert res.status_code == 200
    # Unknown email still 200 (no enumeration).
    res2 = api.post(
        "/api/v1/auth/forgot-password/",
        {"email": "nobody@example.com"},
        format="json",
    )
    assert res2.status_code == 200


def test_reset_password_with_valid_token(api, owner):
    uid = urlsafe_base64_encode(force_bytes(owner.pk))
    token = PasswordResetTokenGenerator().make_token(owner)
    res = api.post(
        "/api/v1/auth/reset-password/",
        {"uid": uid, "token": token, "password": "newpass12345"},
        format="json",
    )
    assert res.status_code == 200
    owner.refresh_from_db()
    assert owner.check_password("newpass12345")


def test_me_exposes_mfa_and_routing_strategy(auth_api, owner, tenant):
    tenant.lead_routing_strategy = Tenant.LeadRoutingStrategy.FIRST_AVAILABLE
    tenant.save(update_fields=["lead_routing_strategy"])
    client = auth_api(owner)
    res = client.get("/api/v1/me/")
    assert res.status_code == 200
    assert "mfa_enabled" in res.data
    assert res.data["lead_routing_strategy"] == "first_available"


def test_me_patch_mfa_and_strategy(auth_api, owner):
    client = auth_api(owner)
    res = client.patch(
        "/api/v1/me/",
        {"mfa_enabled": True, "lead_routing_strategy": "first_available"},
        format="json",
    )
    assert res.status_code == 200
    assert res.data["mfa_enabled"] is True
    assert res.data["lead_routing_strategy"] == "first_available"


def test_mfa_verify_stub(auth_api, owner):
    owner.mfa_enabled = True
    owner.save(update_fields=["mfa_enabled"])
    client = auth_api(owner)
    res = client.post("/api/v1/auth/mfa/verify/", {"code": "123456"}, format="json")
    assert res.status_code == 200
    assert res.data["mfa_verified"] is True


def test_lead_source_crud(auth_api, owner, tenant):
    client = auth_api(owner)
    with tenant_context(tenant.id):
        res = client.post(
            "/api/v1/lead-sources/",
            {"key": "instagram", "label": "Instagram", "weight": 15, "is_active": True},
            format="json",
        )
        assert res.status_code == 201
        assert LeadSource.objects.filter(key="instagram").exists()
        listed = client.get("/api/v1/lead-sources/")
        assert listed.status_code == 200
        assert listed.data["count"] >= 1


def test_first_available_routing(tenant, agent1, agent2):
    with tenant_context(tenant.id):
        Tenant.objects.filter(pk=tenant.id).update(
            lead_routing_strategy="first_available"
        )
        from apps.leads.models import Lead

        # Load agent1 with an open lead so first_available prefers agent2.
        Lead.objects.create(
            tenant=tenant,
            name="Busy",
            lead_type="buy",
            status="new",
            assigned_agent=agent1,
        )
        lead = Lead(tenant_id=tenant.id, name="Free", lead_type="buy")
        route_lead(lead, strategy="first_available")
        assert lead.assigned_agent_id == agent2.id


def test_capture_emits_notification(tenant, agent1):
    with tenant_context(tenant.id):
        lead = capture_lead(
            tenant_id=tenant.id,
            data={
                "name": "Notify Me",
                "email": "notify@x.com",
                "lead_type": "buy",
                "source": "manual",
            },
        )
        assert lead.assigned_agent_id
        assert Notification.objects.filter(
            user_id=lead.assigned_agent_id, title__icontains="lead"
        ).exists()


def test_property_geo_radius_filter(auth_api, owner, tenant):
    client = auth_api(owner)
    with tenant_context(tenant.id):
        Property.objects.create(
            tenant=tenant,
            category=Property.Category.RESIDENTIAL,
            title="Near",
            latitude="9.030000",
            longitude="38.740000",
        )
        Property.objects.create(
            tenant=tenant,
            category=Property.Category.RESIDENTIAL,
            title="Far",
            latitude="10.500000",
            longitude="40.000000",
        )
        near = client.get(
            "/api/v1/properties/",
            {"lat": "9.03", "lng": "38.74", "radius_km": "5", "page_size": 50},
        )
        assert near.status_code == 200
        titles = [p["title"] for p in near.data["results"]]
        assert "Near" in titles
        assert "Far" not in titles
