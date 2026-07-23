"""Lead engine: capture (dedupe/route/score/SLA/ack) + convert (SRS §3.1)."""
import pytest

from apps.contacts.models import Contact
from apps.core.tenancy import tenant_context
from apps.deals.models import Pipeline, Stage
from apps.leads.models import Lead
from apps.leads.services import capture_lead, convert_lead
from apps.leads.tasks import sweep_sla_breaches

pytestmark = pytest.mark.django_db


def _capture(tenant, **overrides):
    data = {
        "name": "Bob Buyer",
        "email": "bob@x.com",
        "phone": "+1 555 0100",
        "lead_type": "buy",
        "source": "referral",
        "budget_max": 500000,
        "preferred_location": "Downtown",
        "timeline": "1_month",
    }
    data.update(overrides)
    return capture_lead(tenant_id=tenant.id, data=data)


def test_capture_creates_contact_scores_routes_and_sets_sla(tenant, agent1, agent2):
    with tenant_context(tenant.id):
        lead = _capture(tenant)
        assert lead.contact is not None
        assert lead.score > 0
        assert lead.assigned_agent in (agent1, agent2)  # round-robin
        assert lead.sla_due_at is not None
        assert lead.acknowledged is True  # console email provider "sent"


def test_capture_links_existing_contact_and_flags_duplicate(tenant, agent1):
    with tenant_context(tenant.id):
        existing = Contact.objects.create(
            tenant=tenant, full_name="Bob", email="bob@x.com"
        )
        lead = _capture(tenant)
        assert lead.contact_id == existing.id
        assert lead.is_possible_duplicate is True


def test_round_robin_prefers_less_loaded_agent(tenant, agent1, agent2):
    with tenant_context(tenant.id):
        first = _capture(tenant, email="c1@x.com", phone="5550001")
        second = _capture(tenant, email="c2@x.com", phone="5550002")
        # second lead should go to the other agent
        assert first.assigned_agent_id != second.assigned_agent_id


def test_convert_lead_creates_opportunity(tenant, agent1):
    with tenant_context(tenant.id):
        pipeline = Pipeline.objects.create(tenant=tenant, name="Sales")
        stage = Stage.objects.create(
            tenant=tenant, pipeline=pipeline, name="New", order=0, probability=10
        )
        lead = _capture(tenant)
        opp = convert_lead(lead=lead, pipeline=pipeline, stage=stage)
        lead.refresh_from_db()
        assert lead.status == Lead.Status.CONVERTED
        assert opp.contact_id == lead.contact_id
        assert opp.value == 500000


def test_sla_sweep_flags_overdue_leads(tenant, agent1):
    from datetime import timedelta

    from django.utils import timezone

    with tenant_context(tenant.id):
        lead = _capture(tenant)
        Lead.objects.filter(id=lead.id).update(
            sla_due_at=timezone.now() - timedelta(minutes=1)
        )
    flagged = sweep_sla_breaches()
    assert flagged == 1
    with tenant_context(tenant.id):
        assert Lead.objects.get(id=lead.id).sla_breached is True
