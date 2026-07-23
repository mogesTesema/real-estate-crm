"""Opportunity stage movement: mandatory reason + history (SRS §3.4.4)."""
import pytest

from apps.contacts.models import Contact
from apps.core.tenancy import tenant_context
from apps.deals.models import Opportunity, Pipeline, Stage
from apps.deals.services import MissingReason, move_stage

pytestmark = pytest.mark.django_db


def _setup(tenant):
    pipeline = Pipeline.objects.create(tenant=tenant, name="Sales")
    new = Stage.objects.create(
        tenant=tenant, pipeline=pipeline, name="New", order=0, probability=10
    )
    won = Stage.objects.create(
        tenant=tenant, pipeline=pipeline, name="Won", order=1, probability=100, is_won=True
    )
    contact = Contact.objects.create(tenant=tenant, full_name="Jane")
    opp = Opportunity.objects.create(
        tenant=tenant, title="Deal", pipeline=pipeline, stage=new, contact=contact,
        value=200000,
    )
    return opp, new, won


def test_move_requires_reason(tenant):
    with tenant_context(tenant.id):
        opp, new, won = _setup(tenant)
        with pytest.raises(MissingReason):
            move_stage(opportunity=opp, to_stage=won, reason="")


def test_move_writes_history_and_marks_won(tenant):
    with tenant_context(tenant.id):
        opp, new, won = _setup(tenant)
        move_stage(opportunity=opp, to_stage=won, reason="Buyer signed")
        opp.refresh_from_db()
        assert opp.stage_id == won.id
        assert opp.status == Opportunity.Status.WON
        assert opp.probability == 100
        assert opp.stage_history.count() == 1
        assert opp.stage_history.first().reason == "Buyer signed"


def test_cannot_move_to_foreign_pipeline_stage(tenant):
    with tenant_context(tenant.id):
        opp, new, won = _setup(tenant)
        other_pipe = Pipeline.objects.create(tenant=tenant, name="Leasing")
        foreign = Stage.objects.create(
            tenant=tenant, pipeline=other_pipe, name="X", order=0
        )
        with pytest.raises(ValueError):
            move_stage(opportunity=opp, to_stage=foreign, reason="x")
