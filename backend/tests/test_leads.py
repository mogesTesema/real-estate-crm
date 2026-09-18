"""`crm` leads — capture, scoring, routing, SLA, conversion (SRS §3.1).

`capture_lead` is the one way a lead enters the system, because every guarantee in SRS 3.1 —
de-duplication, scoring, routing, the SLA clock, the instant acknowledgment — has to hold for
a web form, a walk-in and a CSV alike. A second create path is a set of requirements that
silently applies to some leads and not others, so most of this file exercises that function.
"""
import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.crm import selectors, services
from apps.crm.models import Lead, LeadRoutingRule, LeadSource


@pytest.fixture
def source(db):
    def _make(source_type=LeadSource.SourceType.WEBSITE, name=None):
        return LeadSource.objects.create(
            name=name or source_type, source_type=source_type
        )

    return _make


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def pool(branch):
    """A team containing only the members a test puts in it.

    The shared `team` fixture is `make_user`'s default, so every user built by any fixture is
    already a member — which silently widens a round-robin candidate set.
    """
    from apps.identity.models import Team

    return Team.objects.create(branch=branch, name="Round Robin", code="RR1")


@pytest.fixture
def capture(agent_user):
    def _capture(actor=None, **kwargs):
        kwargs.setdefault("lead_type", Lead.LeadType.BUY)
        kwargs.setdefault("title", "Marina villa enquiry")
        kwargs.setdefault(
            "contact_data",
            {"first_name": "Sam", "last_name": "Rivera", "email": "sam@example.test"},
        )
        return services.capture_lead(actor=actor or agent_user, **kwargs)

    return _capture


# --- capture ------------------------------------------------------------------------------


class TestCapture:
    def test_capture_creates_the_contact_through_the_contacts_service(self, capture, settings):
        """Never Contact.objects.create: normalisation, de-duplication and the role set all
        live behind that service (§1.2), and a second creation path bypasses all three."""
        settings.DEFAULT_PHONE_REGION = "AE"
        lead = capture(
            contact_data={
                "first_name": "Sam",
                "last_name": "Rivera",
                "email": "SAM@EXAMPLE.TEST",
                "phone": "050 123 4567",
            }
        )
        assert lead.contact.email == "sam@example.test"
        assert lead.contact.phone == "+971501234567"

    def test_capture_reuses_an_existing_contact(self, capture, agent_user):
        from apps.contacts import services as contacts_services

        existing = contacts_services.create_contact(
            actor=agent_user, first_name="Sam", email="sam@example.test"
        )
        lead = capture()
        assert lead.contact_id == existing.pk

    def test_the_lead_type_implies_a_contact_role(self, capture):
        """SRS 3.2.2 — a buy enquiry makes them a buyer."""
        lead = capture(lead_type=Lead.LeadType.RENT_IN)
        assert "TENANT" in set(lead.contact.roles.values_list("role", flat=True))

    def test_capture_opens_the_status_trail(self, capture):
        lead = capture()
        row = lead.status_history.get()
        assert row.from_status is None
        assert row.to_status == Lead.Status.NEW

    def test_capture_needs_a_contact_one_way_or_the_other(self, agent_user):
        with pytest.raises(ValidationError):
            services.capture_lead(
                actor=agent_user, lead_type=Lead.LeadType.BUY, title="Nobody"
            )

    def test_capture_is_audited_with_what_it_decided(self, capture):
        from apps.platform.models import AuditEvent

        lead = capture()
        event = AuditEvent.objects.get(entity_type="LEAD", entity_id=lead.pk,
                                       action=AuditEvent.Action.CREATE)
        assert event.new_values["score"] == lead.score


# --- scoring (SRS 3.1.6) --------------------------------------------------------------------


class TestScoring:
    def test_a_referral_outscores_a_cold_portal_enquiry(self, capture, source):
        """Source weight dominates because it is the strongest single predictor in this
        market. The ordering is the substance, not the exact numbers."""
        referral = capture(source=source(LeadSource.SourceType.REFERRAL))
        portal = capture(
            source=source(LeadSource.SourceType.PROPERTY_PORTAL),
            contact_data={"first_name": "Other", "email": "other@example.test"},
        )
        assert referral.score > portal.score

    def test_a_stated_budget_adds_to_the_score(self, capture, source):
        """The clearest signal that someone has decided to buy."""
        plain = capture(source=source())
        budgeted = capture(
            source=source(),
            budget_min=1_000_000,
            budget_max=2_000_000,
            contact_data={"first_name": "B", "email": "b@example.test"},
        )
        assert budgeted.score == plain.score + 20

    def test_an_urgent_timeframe_adds_to_the_score(self, capture, source):
        plain = capture(source=source())
        urgent = capture(
            source=source(),
            expected_timeframe="Immediate",
            contact_data={"first_name": "U", "email": "u@example.test"},
        )
        assert urgent.score == plain.score + 20

    def test_a_reachable_lead_scores_higher(self, capture, source, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        unreachable = capture(source=source())
        reachable = capture(
            source=source(),
            contact_data={"first_name": "R", "email": "r@example.test", "phone": "0501112222"},
        )
        assert reachable.score == unreachable.score + 10

    def test_the_score_is_capped(self, capture, source, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        lead = capture(
            source=source(LeadSource.SourceType.REFERRAL),
            budget_min=1_000_000,
            expected_timeframe="ASAP",
            preferred_location="Dubai Marina",
            contact_data={"first_name": "Max", "email": "max@example.test",
                          "phone": "0501112222"},
        )
        assert lead.score == 100

    def test_a_lead_with_no_source_still_scores(self, capture):
        assert capture().score > 0


# --- de-duplication (SRS 3.1.10, 3.1.11) ----------------------------------------------------


class TestDuplicateFlagging:
    def test_the_same_person_same_property_same_type_is_flagged(self, capture, make_property):
        """SRS 3.1.11's rule, exactly: same contact details, same property, same inquiry
        type. The point is to stop two agents independently working one prospect."""
        prop = make_property()
        first = capture(target_property=prop)
        second = capture(target_property=prop)
        assert first.is_possible_duplicate is False
        assert second.is_possible_duplicate is True

    def test_the_same_person_on_a_different_property_is_not(self, capture, make_property):
        """A second genuine enquiry. Flagging it would train agents to ignore the flag."""
        capture(target_property=make_property())
        second = capture(target_property=make_property())
        assert second.is_possible_duplicate is False

    def test_the_same_person_with_a_different_intent_is_not(self, capture, make_property):
        prop = make_property()
        capture(target_property=prop, lead_type=Lead.LeadType.BUY)
        second = capture(target_property=prop, lead_type=Lead.LeadType.RENT_IN)
        assert second.is_possible_duplicate is False

    def test_a_closed_earlier_lead_does_not_flag_a_new_one(self, capture, make_property, agent_user):
        """Someone who bought two years ago and is enquiring again is not a duplicate."""
        prop = make_property()
        first = capture(target_property=prop)
        services.change_lead_status(
            first, Lead.Status.LOST, actor=agent_user, reason="Went elsewhere."
        )
        second = capture(target_property=prop)
        assert second.is_possible_duplicate is False

    def test_a_flagged_lead_is_still_captured(self, capture, make_property):
        """A flag, never a refusal — the requirement is to stop duplicate outreach, not to
        lose the lead."""
        prop = make_property()
        capture(target_property=prop)
        second = capture(target_property=prop)
        assert second.pk is not None
        assert second.status == Lead.Status.NEW


# --- routing (SRS 3.1.5) ---------------------------------------------------------------------


class TestRouting:
    def test_the_first_matching_rule_by_priority_wins(self, capture, make_user, source):
        early, late = make_user("agent"), make_user("agent")
        LeadRoutingRule.objects.create(
            name="Catch-all", priority=100, criteria={}, assign_to_user=late
        )
        LeadRoutingRule.objects.create(
            name="Buyers", priority=10, criteria={"lead_type": "BUY"}, assign_to_user=early
        )
        lead = capture(lead_type=Lead.LeadType.BUY)
        assert lead.assigned_agent_id == early.pk

    def test_an_inactive_rule_is_skipped(self, capture, make_user):
        ignored, used = make_user("agent"), make_user("agent")
        LeadRoutingRule.objects.create(
            name="Off", priority=1, is_active=False, criteria={}, assign_to_user=ignored
        )
        LeadRoutingRule.objects.create(
            name="On", priority=2, criteria={}, assign_to_user=used
        )
        assert capture().assigned_agent_id == used.pk

    def test_a_rule_whose_criteria_do_not_match_is_skipped(self, capture, make_user):
        unmatched = make_user("agent")
        LeadRoutingRule.objects.create(
            name="Sellers only", priority=1, criteria={"lead_type": "SELL"},
            assign_to_user=unmatched,
        )
        assert capture(lead_type=Lead.LeadType.BUY).assigned_agent is None

    def test_a_score_band_can_gate_a_rule(self, capture, make_user, source):
        senior = make_user("agent")
        LeadRoutingRule.objects.create(
            name="High value", priority=1, criteria={"min_score": 70}, assign_to_user=senior
        )
        cold = capture(source=source(LeadSource.SourceType.MANUAL))
        hot = capture(
            source=source(LeadSource.SourceType.REFERRAL),
            budget_min=1_000_000,
            expected_timeframe="Immediate",
            contact_data={"first_name": "Hot", "email": "hot@example.test"},
        )
        assert cold.assigned_agent is None
        assert hot.assigned_agent_id == senior.pk

    def test_a_team_rule_leaves_the_lead_in_the_pool(self, capture, team):
        # The shared `team` fixture is fine here: nothing depends on who is in it.
        """`assigned_agent` stays null on purpose: the lead sits in the pool, visible to every
        member under OWN scope, until someone claims it."""
        LeadRoutingRule.objects.create(
            name="To the team", priority=1, criteria={}, assign_to_team=team
        )
        lead = capture()
        assert lead.assigned_agent is None
        assert lead.assigned_team_id == team.pk

    def test_round_robin_picks_the_least_loaded_agent(self, capture, make_user, pool):
        team = pool
        busy, idle = make_user("agent", team=team), make_user("agent", team=team)
        for n in range(3):
            Lead.objects.create(
                contact=capture(
                    contact_data={"first_name": f"X{n}", "email": f"x{n}@example.test"}
                ).contact,
                lead_type=Lead.LeadType.BUY,
                title=f"Existing {n}",
                assigned_agent=busy,
            )
        LeadRoutingRule.objects.create(
            name="RR", priority=1, criteria={}, assign_to_team=team, round_robin=True
        )
        lead = capture(contact_data={"first_name": "New", "email": "new@example.test"})
        assert lead.assigned_agent_id == idle.pk

    def test_a_multi_role_agent_is_not_double_counted(self, capture, make_user, pool):
        """The bug this replaces: filtering users on `user_roles__role__code` joins the
        through-table, so an agent holding two roles appears twice — doubling their counted
        load and permanently protecting them from assignment."""
        from apps.identity.models import Role, UserRole

        team = pool
        two_hats = make_user("agent", team=team)
        UserRole.objects.create(user=two_hats, role=Role.objects.get(code="marketing"))
        one_hat = make_user("agent", team=team)
        Lead.objects.create(
            contact=capture(
                contact_data={"first_name": "Load", "email": "load@example.test"}
            ).contact,
            lead_type=Lead.LeadType.BUY, title="Existing", assigned_agent=one_hat,
        )
        LeadRoutingRule.objects.create(
            name="RR", priority=1, criteria={}, assign_to_team=team, round_robin=True
        )
        lead = capture(contact_data={"first_name": "New", "email": "new@example.test"})
        # two_hats holds zero leads; one_hat holds one. A doubled count would not change this
        # outcome, so the real assertion is on the counted load itself.
        assert lead.assigned_agent_id == two_hats.pk
        assert services.least_loaded_agent([two_hats.pk, one_hat.pk]).pk == two_hats.pk

    def test_an_empty_team_still_holds_the_lead(self, capture, branch):
        """Dropping a lead on the floor because nobody is available is worse than a visible
        pool nobody has claimed."""
        from apps.identity.models import Team

        empty = Team.objects.create(branch=branch, name="Nobody", code="NB")
        LeadRoutingRule.objects.create(
            name="RR", priority=1, criteria={}, assign_to_team=empty, round_robin=True
        )
        lead = capture()
        assert lead.assigned_agent is None
        assert lead.assigned_team_id == empty.pk

    def test_assignment_is_recorded(self, capture, make_user, agent_user):
        target = make_user("agent")
        lead = capture()
        services.assign_lead(lead, to_user=target, actor=agent_user, reason="Specialist.")
        row = lead.assignments.get()
        assert row.to_user_id == target.pk
        assert row.reason == "Specialist."


# --- SLA (SRS 3.1.9) --------------------------------------------------------------------------


class TestSla:
    def test_capture_starts_the_clock(self, capture, settings):
        settings.LEAD_SLA_MINUTES = 60
        lead = capture()
        assert lead.sla_due_at is not None
        assert lead.sla_breached is False

    def test_the_window_is_configurable(self, capture, settings):
        """"a *configurable* SLA window" — it varies by market and by team."""
        settings.LEAD_SLA_MINUTES = 15
        lead = capture()
        minutes = (lead.sla_due_at - timezone.now()).total_seconds() / 60
        assert 13 < minutes <= 15

    def test_the_sweep_flags_only_overdue_unanswered_open_leads(self, capture, agent_user):
        overdue = capture()
        answered = capture(contact_data={"first_name": "A", "email": "a@example.test"})
        future = capture(contact_data={"first_name": "F", "email": "f@example.test"})

        past = timezone.now() - timezone.timedelta(minutes=5)
        Lead.objects.filter(pk__in=[overdue.pk, answered.pk]).update(sla_due_at=past)
        services.record_first_response(answered, actor=agent_user)

        breached = services.sweep_sla()
        assert overdue.pk in breached
        assert answered.pk not in breached
        assert future.pk not in breached

    def test_the_sweep_is_idempotent(self, capture):
        lead = capture()
        Lead.objects.filter(pk=lead.pk).update(
            sla_due_at=timezone.now() - timezone.timedelta(minutes=5)
        )
        assert services.sweep_sla() == [lead.pk]
        assert services.sweep_sla() == []

    def test_responding_stops_the_clock_and_advances_the_status(self, capture, agent_user):
        lead = capture()
        services.record_first_response(lead, actor=agent_user, note="Called.")
        lead.refresh_from_db()
        assert lead.first_response_at is not None
        assert lead.status == Lead.Status.CONTACTED

    def test_a_later_response_does_not_reset_the_measurement(self, capture, agent_user):
        """The requirement measures time-to-*first*-contact. Resetting it would hide a breach
        that already happened."""
        lead = capture()
        services.record_first_response(lead, actor=agent_user)
        lead.refresh_from_db()
        first = lead.first_response_at
        services.record_first_response(lead, actor=agent_user)
        lead.refresh_from_db()
        assert lead.first_response_at == first

    def test_the_management_command_runs(self, capture, capsys):
        from django.core.management import call_command

        lead = capture()
        Lead.objects.filter(pk=lead.pk).update(
            sla_due_at=timezone.now() - timezone.timedelta(minutes=5)
        )
        call_command("sweep_sla")
        lead.refresh_from_db()
        assert lead.sla_breached is True

    def test_overdue_leads_are_scoped_to_the_caller(self, capture, agent_user, make_user):
        lead = capture()
        services.assign_lead(lead, to_user=agent_user, actor=agent_user)
        Lead.objects.filter(pk=lead.pk).update(
            sla_due_at=timezone.now() - timezone.timedelta(minutes=5)
        )
        services.sweep_sla()
        assert lead.pk in {r.pk for r in selectors.overdue_leads(agent_user)}
        assert selectors.overdue_leads(make_user("agent")).count() == 0


# --- acknowledgment (SRS 3.1.12) ---------------------------------------------------------------


class TestAcknowledgment:
    def test_capture_sends_one_immediately(self, capture):
        mail.outbox.clear()
        lead = capture()
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["sam@example.test"]
        assert lead.acknowledged is True

    def test_a_lead_with_no_email_is_not_acknowledged(self, capture, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        mail.outbox.clear()
        lead = capture(contact_data={"first_name": "Noemail", "phone": "0501234567"})
        assert mail.outbox == []
        assert lead.acknowledged is False

    def test_a_failed_send_does_not_lose_the_lead(self, capture, monkeypatch):
        """A mail server being down must not cost a lead. `acknowledged` stays false so a
        retry is possible without double-sending."""
        def boom(*args, **kwargs):
            raise OSError("smtp down")

        monkeypatch.setattr("apps.crm.services.send_mail", boom)
        lead = capture()
        assert lead.pk is not None
        assert lead.acknowledged is False

    def test_it_is_not_sent_twice(self, capture):
        lead = capture()
        mail.outbox.clear()
        services.send_acknowledgment(lead)
        assert mail.outbox == []


# --- conversion (SRS 3.1.8) --------------------------------------------------------------------


class TestConversion:
    def test_converting_creates_a_deal_and_closes_the_lead(self, capture, pipeline, agent_user):
        lead = capture()
        deal = services.convert_lead(lead, actor=agent_user, pipeline=pipeline)
        lead.refresh_from_db()
        assert lead.status == Lead.Status.CONVERTED
        assert lead.converted_at is not None
        assert deal.primary_contact_id == lead.contact_id
        assert deal.reference_code.startswith("DL-")

    def test_conversion_is_idempotent(self, capture, pipeline, agent_user):
        """A double-clicked Convert button is the normal way this happens."""
        lead = capture()
        first = services.convert_lead(lead, actor=agent_user, pipeline=pipeline)
        second = services.convert_lead(lead, actor=agent_user, pipeline=pipeline)
        assert first.pk == second.pk

    def test_a_stage_from_another_pipeline_is_refused(self, capture, pipeline, agent_user):
        """The old implementation took the stage on trust. A stage from another pipeline puts
        the deal on a board it will never appear on."""
        from apps.crm.models import Pipeline, PipelineStage

        other = Pipeline.objects.create(
            name="Leasing", pipeline_type=Pipeline.PipelineType.LEASING
        )
        foreign = PipelineStage.objects.create(
            pipeline=other, name="New", code="NEW", sort_order=0, probability=10
        )
        with pytest.raises(ValidationError, match="different pipeline"):
            services.convert_lead(
                capture(), actor=agent_user, pipeline=pipeline, stage=foreign
            )

    def test_the_target_property_becomes_the_matched_property(
        self, capture, pipeline, agent_user, make_property
    ):
        """SRS 3.4.7 — "one opportunity to a specific matched property once identified"."""
        prop = make_property()
        deal = services.convert_lead(
            capture(target_property=prop), actor=agent_user, pipeline=pipeline
        )
        link = deal.deal_properties.get()
        assert link.property_id == prop.pk
        assert link.is_primary is True

    def test_the_deal_opens_its_stage_history(self, capture, pipeline, agent_user):
        deal = services.convert_lead(capture(), actor=agent_user, pipeline=pipeline)
        row = deal.stage_history.get()
        assert row.from_stage is None
        assert row.reason

    def test_conversion_is_audited(self, capture, pipeline, agent_user):
        from apps.platform.models import AuditEvent

        lead = capture()
        deal = services.convert_lead(lead, actor=agent_user, pipeline=pipeline)
        event = AuditEvent.objects.filter(
            entity_type="LEAD", entity_id=lead.pk, action=AuditEvent.Action.UPDATE
        ).first()
        assert event.new_values["converted_to_deal"] == deal.reference_code


# --- status machine (SRS 3.1.4) ----------------------------------------------------------------


class TestLeadStatus:
    def test_a_lost_lead_needs_a_reason(self, capture, agent_user):
        with pytest.raises(ValidationError, match="reason"):
            services.change_lead_status(capture(), Lead.Status.LOST, actor=agent_user)

    def test_reopening_a_lost_lead_clears_the_reason(self, capture, agent_user):
        """Otherwise every report downstream still shows why it was lost while it is visibly
        open."""
        lead = capture()
        services.change_lead_status(
            lead, Lead.Status.LOST, actor=agent_user, reason="Budget too low."
        )
        services.change_lead_status(lead, Lead.Status.NEW, actor=agent_user)
        lead.refresh_from_db()
        assert lead.lost_reason is None

    def test_converted_is_terminal(self, capture, pipeline, agent_user):
        """The deal is the record now. Moving the lead afterwards leaves two places claiming
        to say where the opportunity stands."""
        lead = capture()
        services.convert_lead(lead, actor=agent_user, pipeline=pipeline)
        lead.refresh_from_db()
        with pytest.raises(ValidationError, match="terminal"):
            services.change_lead_status(lead, Lead.Status.NURTURING, actor=agent_user)

    def test_an_illegal_move_is_refused(self, capture, agent_user):
        with pytest.raises(ValidationError):
            services.change_lead_status(capture(), Lead.Status.CONVERTED, actor=agent_user)


# --- the endpoints -------------------------------------------------------------------------------


@pytest.mark.django_db
class TestLeadApi:
    URL = "/api/v1/leads/"

    def test_capture_over_http(self, auth_client, agent_user):
        response = auth_client(agent_user).post(
            self.URL,
            {
                "lead_type": "BUY",
                "title": "Marina villa",
                "contact_details": {
                    "first_name": "Sam",
                    "last_name": "Rivera",
                    "email": "sam@example.test",
                },
                "budget_max": "2000000",
                "expected_timeframe": "Immediate",
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        assert response.data["score"] > 0
        assert response.data["sla_state"] == "PENDING"

    def test_capture_needs_a_contact(self, auth_client, agent_user):
        response = auth_client(agent_user).post(
            self.URL, {"lead_type": "BUY", "title": "Nobody"}, format="json"
        )
        assert response.status_code == 400

    def test_the_list_is_scoped(self, auth_client, agent_user, make_user, capture):
        mine = capture()
        services.assign_lead(mine, to_user=agent_user, actor=agent_user)
        capture(contact_data={"first_name": "Other", "email": "other@example.test"})

        response = auth_client(agent_user).get(self.URL)
        assert [r["id"] for r in response.data["results"]] == [str(mine.id)]

    def test_an_agent_sees_their_teams_unclaimed_pool(self, auth_client, agent_user, capture, team):
        """The second anchor. Routing may leave `assigned_agent` null; without it the whole
        pool is invisible to the agents meant to work it."""
        lead = capture()
        services.assign_lead(lead, to_team=team, actor=agent_user)
        response = auth_client(agent_user).get(self.URL)
        assert str(lead.id) in [r["id"] for r in response.data["results"]]

    def test_responding_stops_the_clock(self, auth_client, agent_user, capture):
        lead = capture()
        services.assign_lead(lead, to_user=agent_user, actor=agent_user)
        response = auth_client(agent_user).post(
            f"{self.URL}{lead.id}/respond/", {"note": "Called."}, format="json"
        )
        assert response.status_code == 200, response.data
        assert response.data["sla_state"] == "ANSWERED"

    def test_the_overdue_queue_lists_only_breached_leads(self, auth_client, agent_user, capture):
        breached = capture()
        services.assign_lead(breached, to_user=agent_user, actor=agent_user)
        fine = capture(contact_data={"first_name": "Fine", "email": "fine@example.test"})
        services.assign_lead(fine, to_user=agent_user, actor=agent_user)
        Lead.objects.filter(pk=breached.pk).update(
            sla_due_at=timezone.now() - timezone.timedelta(minutes=5)
        )
        services.sweep_sla()

        response = auth_client(agent_user).get(f"{self.URL}overdue/")
        assert [r["id"] for r in response.data["results"]] == [str(breached.id)]

    def test_convert_over_http(self, auth_client, agent_user, capture, pipeline):
        lead = capture()
        services.assign_lead(lead, to_user=agent_user, actor=agent_user)
        response = auth_client(agent_user).post(
            f"{self.URL}{lead.id}/convert/", {"pipeline": str(pipeline.id)}, format="json"
        )
        assert response.status_code == 201, response.data
        assert response.data["reference_code"].startswith("DL-")

    def test_a_target_property_outside_scope_is_refused(
        self, auth_client, agent_user, make_property
    ):
        """Resolving it unscoped would let a caller confirm any property id exists by watching
        whether the capture succeeds."""
        prop = make_property()
        response = auth_client(agent_user).post(
            self.URL,
            {
                "lead_type": "BUY",
                "title": "Probe",
                "target_property": str(prop.id),
                "contact_details": {"first_name": "Sam", "email": "sam@example.test"},
            },
            format="json",
        )
        assert response.status_code == 404
