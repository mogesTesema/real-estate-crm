"""Saved reports, schedules and dashboard snapshots (SRS 3.13).

Both halves of the SavedReport doctrine under test: visibility gates the DEFINITION,
execution re-scopes rows through the caller. Snapshots: the same KPI arithmetic as the
live dashboard, upserted idempotently, read back with scope-gated access.
"""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.platform import reporting
from apps.platform.models import DashboardSnapshot, SavedReport


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


def make_lead(agent, make_contact, **kwargs):
    from apps.crm import services

    kwargs.setdefault("lead_type", "BUY")
    return services.capture_lead(
        actor=agent, contact=make_contact(), acknowledge=False, route=False,
        assigned_agent=agent, **kwargs,
    )


class TestExecutor:
    def test_definitions_are_validated_at_save_time(self, db):
        with pytest.raises(ValidationError, match="Unknown base"):
            reporting.validate_definition({"base": "users"})
        with pytest.raises(ValidationError, match="Unknown filters"):
            reporting.validate_definition(
                {"base": "leads", "filters": {"password": "x"}}
            )
        with pytest.raises(ValidationError, match="Unknown columns"):
            reporting.validate_definition({"base": "leads", "columns": ["secret"]})

    def test_execution_rescopes_rows_through_the_caller(
        self, db, agent_user, make_user, make_contact, owner
    ):
        """An ORG-shared report opened by an agent renders only the agent's rows."""
        mine = make_lead(agent_user, make_contact)
        make_lead(make_user("agent"), make_contact)  # someone else's

        definition = {"base": "leads", "columns": ["id", "status"]}
        as_agent = reporting.execute(definition, agent_user)
        as_owner = reporting.execute(definition, owner)
        assert len(as_agent["rows"]) == 1
        assert str(as_agent["rows"][0]["id"]) == str(mine.pk)
        assert len(as_owner["rows"]) == 2

    def test_filters_and_group_by_use_the_whitelist_mapping(
        self, db, agent_user, make_contact
    ):
        make_lead(agent_user, make_contact, priority="HIGH")
        make_lead(agent_user, make_contact, priority="LOW")
        filtered = reporting.execute(
            {"base": "leads", "columns": ["id"], "filters": {"priority": "HIGH"}},
            agent_user,
        )
        assert len(filtered["rows"]) == 1
        grouped = reporting.execute(
            {"base": "leads", "group_by": "priority"}, agent_user
        )
        counts = {row["priority"]: row["count"] for row in grouped["rows"]}
        assert counts == {"HIGH": 1, "LOW": 1}

    def test_every_run_is_audited_export(self, db, agent_user, make_contact):
        from apps.platform.models import AuditEvent

        report = SavedReport.objects.create(
            name="My leads", report_type="LEADS",
            definition={"base": "leads", "columns": ["id"]}, owner=agent_user,
        )
        reporting.execute_and_audit(report, agent_user)
        assert AuditEvent.objects.filter(
            entity_type="SAVED_REPORT", entity_id=report.pk, action="EXPORT"
        ).exists()


class TestVisibility:
    def test_visibility_gates_the_definition(
        self, db, auth_client, agent_user, make_user
    ):
        author = make_user("agent")  # same team as agent_user (conftest default)
        SavedReport.objects.create(
            name="Private", report_type="LEADS",
            definition={"base": "leads"}, owner=author, visibility="PRIVATE",
        )
        shared = SavedReport.objects.create(
            name="Team funnel", report_type="LEADS",
            definition={"base": "leads"}, owner=author, visibility="TEAM",
        )
        listed = auth_client(agent_user).get("/api/v1/saved-reports/").data["results"]
        assert {row["name"] for row in listed} == {"Team funnel"}

        # Editing someone else's shared definition is refused.
        assert auth_client(agent_user).patch(
            f"/api/v1/saved-reports/{shared.pk}/", {"name": "Hijacked"}
        ).status_code == 403

    def test_schedules_are_csv_only_for_now(self, db, auth_client, agent_user):
        report = SavedReport.objects.create(
            name="Mine", report_type="LEADS",
            definition={"base": "leads"}, owner=agent_user,
        )
        response = auth_client(agent_user).post(
            "/api/v1/report-schedules/",
            {
                "saved_report": str(report.pk), "frequency": "DAILY",
                "next_run_at": timezone.now().isoformat(),
                "recipients": ["boss@example.test"], "export_format": "XLSX",
            },
            format="json",
        )
        assert response.status_code == 400
        assert "CSV" in str(response.data)


class TestScheduleRunner:
    def test_next_run_advances_from_the_scheduled_time(
        self, db, agent_user, make_contact
    ):
        from django.core import mail
        from django.core.management import call_command

        from apps.platform.models import ReportSchedule

        make_lead(agent_user, make_contact)
        report = SavedReport.objects.create(
            name="Daily leads", report_type="LEADS",
            definition={"base": "leads", "columns": ["id", "status"]},
            owner=agent_user,
        )
        scheduled_for = timezone.now() - timedelta(hours=3)
        schedule = ReportSchedule.objects.create(
            saved_report=report, frequency="DAILY", next_run_at=scheduled_for,
            recipients=["boss@example.test"], export_format="CSV",
            created_by=agent_user,
        )
        call_command("run_report_schedules")
        schedule.refresh_from_db()
        # Advanced from the SCHEDULED time (no drift), past now (no crash-loop).
        assert schedule.next_run_at == scheduled_for + timedelta(days=1)
        assert schedule.last_run_at is not None
        assert len(mail.outbox) == 1
        assert "Daily leads" in mail.outbox[0].subject

        call_command("run_report_schedules")  # nothing due — nothing sent
        assert len(mail.outbox) == 1


class TestSnapshots:
    def test_build_is_idempotent_and_read_back_scoped(
        self, db, auth_client, agent_user, make_contact, owner, branch, team
    ):
        from django.core.management import call_command

        make_lead(agent_user, make_contact)
        call_command("build_dashboard_snapshots")
        call_command("build_dashboard_snapshots")  # run-twice-changes-nothing
        org_rows = DashboardSnapshot.objects.filter(scope_type="ORG")
        assert org_rows.count() == 1
        assert org_rows.get().data["leads"]["captured"] == 1

        today = timezone.localdate().isoformat()
        # ALL scope reads the org snapshot back through the dashboard.
        response = auth_client(owner).get(f"/api/v1/dashboard/?as_of={today}")
        assert response.status_code == 200
        assert response.data["leads"]["captured"] == 1

        # A team snapshot for someone else's team is not readable by this agent.
        other = auth_client(agent_user).get(
            f"/api/v1/dashboard/?as_of={today}&scope_type=BRANCH&scope_id={branch.pk}"
        )
        # agent has OWN scope, not BRANCH — refused as not-found.
        assert other.status_code == 404

    def test_branch_snapshot_counts_only_branch_rows(
        self, db, agent_user, make_contact, make_user, other_branch, branch
    ):
        from apps.platform.selectors import dashboard_for_scope

        make_lead(agent_user, make_contact)  # default branch
        stranger = make_user("agent", branch=other_branch)
        make_lead(stranger, make_contact)

        ours = dashboard_for_scope("BRANCH", branch.pk)
        theirs = dashboard_for_scope("BRANCH", other_branch.pk)
        org = dashboard_for_scope("ORG")
        assert ours["leads"]["captured"] == 1
        assert theirs["leads"]["captured"] == 1
        assert org["leads"]["captured"] == 2


class TestAi:
    def test_insights_are_deterministic_and_scoped(
        self, db, auth_client, agent_user, make_user, make_contact
    ):
        lead = make_lead(agent_user, make_contact, expected_timeframe="ASAP")
        client = auth_client(agent_user)
        first = client.get(f"/api/v1/leads/{lead.pk}/ai-insights/")
        second = client.get(f"/api/v1/leads/{lead.pk}/ai-insights/")
        assert first.status_code == 200
        assert first.data == second.data  # deterministic mock
        assert first.data["scoring"]["score"] == lead.score  # true, not hallucinated
        assert first.data["next_best_action"]["action"]

        # A stranger's lead is a 404 — scope first, AI second.
        stranger = auth_client(make_user("agent"))
        assert stranger.get(f"/api/v1/leads/{lead.pk}/ai-insights/").status_code == 404

    def test_draft_interpolates_the_lead(self, db, auth_client, agent_user, make_contact):
        lead = make_lead(agent_user, make_contact)
        response = auth_client(agent_user).post(
            "/api/v1/ai/draft/",
            {"lead": str(lead.pk), "channel": "EMAIL", "intent": "follow_up"},
        )
        assert response.status_code == 200
        assert lead.contact.first_name in response.data["body"]

    def test_public_chat_fills_slots_then_captures(self, db, api_client):
        from apps.crm.models import Lead

        state, message = None, ""
        answers = ["buy", "2M", "Dubai Marina", "Lina Haddad", "lina@example.test"]
        for answer in answers:
            response = api_client.post(
                "/api/public/chat/qualify/",
                {"state": state, "message": message}, format="json",
            )
            assert response.status_code == 200
            state, message = response.data["state"], answer
        final = api_client.post(
            "/api/public/chat/qualify/", {"state": state, "message": message},
            format="json",
        )
        assert final.data["done"] is True
        lead = Lead.objects.get()
        assert lead.contact.first_name == "Lina"
        assert lead.contact.email == "lina@example.test"
        assert "2M" in lead.description
