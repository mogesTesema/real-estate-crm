"""Campaigns, drip, landing pages, saved-search alerts (SRS §3.8).

The drip runner's contract: SKIP LOCKED claim on `next_send_at` (no double sends), closed
leads exit instead of receiving marketing, a send failure retries without advancing the
step, and re-running when nothing is due changes nothing.
"""
from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.crm import services
from apps.crm.models import CampaignEnrollment


@pytest.fixture
def marketer(make_user):
    return make_user("marketing")


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def campaign(marketer):
    campaign = services.create_campaign(
        actor=marketer, name="Spring nurture", campaign_type="DRIP",
        start_date=timezone.localdate(), status="ACTIVE",
    )
    services.add_campaign_step(
        campaign, actor=marketer, step_order=1, channel="EMAIL",
        delay_days=0, subject="Welcome", body="Hello {{ contact.first_name }}",
    )
    services.add_campaign_step(
        campaign, actor=marketer, step_order=2, channel="EMAIL",
        delay_days=3, subject="Still looking?", body="Any questions?",
    )
    return campaign


@pytest.fixture
def lead(agent_user, make_contact):
    return services.capture_lead(
        actor=agent_user, contact=make_contact(), lead_type="BUY",
        acknowledge=False, route=False, assigned_agent=agent_user,
    )


class TestDrip:
    def test_the_full_two_step_walkthrough(self, db, campaign, lead, marketer):
        enrollment = services.enroll_lead(campaign, lead, actor=marketer)
        assert enrollment.current_step == 0
        # Step 1 has delay 0 — due immediately.
        services.run_drip()
        enrollment.refresh_from_db()
        assert enrollment.current_step == 1
        assert enrollment.status == CampaignEnrollment.Status.ACTIVE
        assert len(mail.outbox) == 1
        assert lead.contact.first_name in mail.outbox[0].body  # merge ran

        # Nothing due until day 3: a re-run changes nothing.
        services.run_drip()
        enrollment.refresh_from_db()
        assert enrollment.current_step == 1
        assert len(mail.outbox) == 1

        # Fast-forward: the last step completes the enrollment.
        services.run_drip(now=timezone.now() + timedelta(days=3, minutes=1))
        enrollment.refresh_from_db()
        assert enrollment.current_step == 2
        assert enrollment.status == CampaignEnrollment.Status.COMPLETED
        assert enrollment.next_send_at is None
        assert len(mail.outbox) == 2

    def test_a_converted_lead_exits_instead_of_receiving_marketing(
        self, db, campaign, lead, marketer, agent_user, pipeline
    ):
        services.enroll_lead(campaign, lead, actor=marketer)
        services.change_lead_status(lead, "CONTACTED", actor=agent_user)
        services.change_lead_status(lead, "QUALIFIED", actor=agent_user)
        services.convert_lead(lead, actor=agent_user, pipeline=pipeline)
        services.run_drip()
        enrollment = CampaignEnrollment.objects.get(lead=lead)
        assert enrollment.status == CampaignEnrollment.Status.EXITED
        assert len(mail.outbox) == 0

    def test_a_send_failure_retries_without_advancing(
        self, db, campaign, lead, marketer, monkeypatch
    ):
        from apps.collaboration.services import communications

        def broken_gateway(channel):
            raise RuntimeError("provider down")

        monkeypatch.setattr(communications, "get_gateway", broken_gateway)
        services.enroll_lead(campaign, lead, actor=marketer)
        processed = services.run_drip()
        enrollment = CampaignEnrollment.objects.get(lead=lead)
        assert processed == [(enrollment.pk, "RETRY")]
        assert enrollment.current_step == 0  # not advanced
        assert enrollment.next_send_at > timezone.now()  # backed off

    def test_enrollment_is_unique_and_restartable(self, db, campaign, lead, marketer):
        first = services.enroll_lead(campaign, lead, actor=marketer)
        again = services.enroll_lead(campaign, lead, actor=marketer)
        assert first.pk == again.pk
        services.exit_enrollment(first, actor=marketer)
        revived = services.enroll_lead(campaign, lead, actor=marketer)
        assert revived.pk == first.pk
        assert revived.status == CampaignEnrollment.Status.ACTIVE

    def test_writes_are_marketing_staff_only(
        self, db, auth_client, agent_user, marketer
    ):
        payload = {
            "name": "Q4 push", "campaign_type": "DRIP",
            "start_date": str(timezone.localdate()), "status": "DRAFT",
        }
        assert auth_client(agent_user).post(
            "/api/v1/campaigns/", payload
        ).status_code == 403
        assert auth_client(marketer).post(
            "/api/v1/campaigns/", payload
        ).status_code == 201
        # Reads are open to staff — an agent can see what marketing is running.
        assert auth_client(agent_user).get("/api/v1/campaigns/").status_code == 200


class TestMetrics:
    def test_deltas_upsert_the_day_row(self, db, campaign):
        today = timezone.localdate()
        services.record_campaign_metric(campaign, today, clicks=5)
        services.record_campaign_metric(campaign, today, clicks=3, leads_generated=1)
        row = campaign.metrics.get(metric_date=today)
        assert row.clicks == 8
        assert row.leads_generated == 1

    def test_capture_attributes_to_the_campaign(
        self, db, campaign, agent_user, make_contact
    ):
        services.capture_lead(
            actor=agent_user, contact=make_contact(), lead_type="BUY",
            campaign=campaign, acknowledge=False, route=False,
        )
        row = campaign.metrics.get(metric_date=timezone.localdate())
        assert row.leads_generated == 1


class TestLandingPages:
    @pytest.fixture
    def page(self, marketer, campaign):
        return services.create_landing_page(
            actor=marketer, slug="spring-villas", title="Spring Villas",
            campaign=campaign, is_published=True,
            form_config={"fields": [
                {"name": "name", "required": True},
                {"name": "email", "required": True},
            ]},
        )

    def test_submission_runs_the_full_capture_pipeline(self, db, page, campaign):
        lead = services.submit_landing_page(
            page, form_data={"name": "Amira Khan", "email": "amira@example.test"}
        )
        assert lead.campaign == campaign
        assert lead.contact.first_name == "Amira"
        page.refresh_from_db()
        assert page.submissions == 1
        # Attribution counted once, at capture.
        assert campaign.metrics.get().leads_generated == 1

    def test_required_form_fields_are_enforced(self, db, page):
        with pytest.raises(ValidationError):
            services.submit_landing_page(page, form_data={"name": "No Email"})


class TestSavedSearchAlerts:
    def test_criteria_are_whitelisted(self, db, agent_user, make_contact):
        with pytest.raises(ValidationError, match="Unknown criteria"):
            services.create_saved_search_alert(
                actor=agent_user, contact=make_contact(), name="Bad",
                frequency="DAILY", channel="EMAIL",
                criteria={"__class__": "nope"},
            )

    def test_the_window_always_advances(
        self, db, agent_user, make_contact, make_property, make_user
    ):
        """At-most-once: matches or not, `last_run_at` moves — an alert that retries a
        window is an alert that spams a client."""
        from apps.inventory import services as inventory_services

        contact = make_contact(email="buyer@example.test")
        alert = services.create_saved_search_alert(
            actor=agent_user, contact=contact, name="Dubai 2BR",
            frequency="INSTANT", channel="EMAIL",
            criteria={"city": "Dubai", "bedrooms": 2},
        )
        # First run: nothing on the market yet — the window still closes.
        services.run_saved_search_alerts()
        alert.refresh_from_db()
        first_window = alert.last_run_at
        assert first_window is not None
        assert len(mail.outbox) == 0

        pm = make_user("property_manager")
        listing = inventory_services.create_listing(
            actor=pm, property=make_property(bedrooms=3, city="Dubai"),
            listing_type="SALE", title="Marina 3BR", asking_price=2000000,
        )
        inventory_services.change_listing_status(listing, "ACTIVE", actor=pm)
        services.run_saved_search_alerts()
        alert.refresh_from_db()
        assert alert.last_run_at > first_window
        assert len(mail.outbox) == 1
        assert "Marina 3BR" in mail.outbox[0].body

        # The same listing is not re-announced in the next window.
        services.run_saved_search_alerts()
        assert len(mail.outbox) == 1

    def test_alerts_follow_contact_visibility(
        self, db, auth_client, agent_user, owner, make_contact
    ):
        services.create_saved_search_alert(
            actor=owner, contact=make_contact(), name="Private",
            frequency="DAILY", channel="EMAIL",
        )
        # The contact is nobody's — an OWN-scope agent sees no alert rows.
        assert auth_client(agent_user).get(
            "/api/v1/saved-search-alerts/"
        ).data["results"] == []
        assert len(
            auth_client(owner).get("/api/v1/saved-search-alerts/").data["results"]
        ) == 1
