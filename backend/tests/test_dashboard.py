"""Dashboards and reports (SRS §3.13).

The point of this module is that analytics is the easiest place for row scoping to quietly
stop applying — an aggregate has no obvious owner, so a COUNT(*) over the table looks
perfectly reasonable in review. Every figure here is computed over an `apply_scope`d queryset
reached through the owning app's `selectors`, and most of these tests exist to prove it.
"""
import pytest
from django.utils import timezone

from apps.crm import services as crm_services
from apps.crm.models import Lead, LeadSource
from apps.platform import selectors as platform_selectors


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def seed(agent_user, make_contact, pipeline, make_property):
    """A small book of business owned by `agent_user`."""

    def _seed(owner=None, count=3, source_type=LeadSource.SourceType.WEBSITE):
        owner = owner or agent_user
        source = LeadSource.objects.create(name=source_type, source_type=source_type)
        leads = []
        for n in range(count):
            lead = crm_services.capture_lead(
                actor=owner,
                contact_data={"first_name": f"P{n}", "email": f"p{n}-{owner.pk}@example.test"},
                lead_type=Lead.LeadType.BUY,
                title=f"Enquiry {n}",
                source=source,
                route=False,
                acknowledge=False,
            )
            crm_services.assign_lead(lead, to_user=owner, actor=owner)
            leads.append(lead)
        return {"owner": owner, "leads": leads, "source": source}

    return _seed


# --- the dashboard (SRS 3.13.1) ---------------------------------------------------------------


class TestDashboard:
    def test_an_agent_sees_only_their_own_numbers(self, seed, agent_user, make_user):
        """The figure that is wrong when an aggregate forgets to scope."""
        seed(count=3)
        seed(owner=make_user("agent"), count=5)
        data = platform_selectors.dashboard(agent_user)
        assert data["leads"]["captured"] == 3

    def test_an_owner_sees_the_whole_company(self, seed, agent_user, owner, make_user):
        seed(count=3)
        seed(owner=make_user("agent"), count=5)
        assert platform_selectors.dashboard(owner)["leads"]["captured"] == 8

    def test_a_manager_sees_their_branch_only(self, seed, make_user, manager, other_branch):
        from apps.identity.models import Team

        far_team = Team.objects.create(branch=other_branch, name="Far", code="FARD")
        seed(owner=make_user("agent"), count=2)
        seed(owner=make_user("agent", branch=other_branch, team=far_team), count=4)
        assert platform_selectors.dashboard(manager)["leads"]["captured"] == 2

    def test_conversion_rate_is_none_rather_than_zero_when_there_are_no_leads(
        self, db, make_user
    ):
        """A new agent's first day is "no rate yet", not "a rate of zero"."""
        assert platform_selectors.dashboard(make_user("agent"))["leads"]["conversion_rate"] is None

    def test_conversion_rate_counts_converted_leads(self, seed, agent_user, pipeline):
        data = seed(count=4)
        crm_services.change_lead_status(
            data["leads"][0], Lead.Status.QUALIFIED, actor=agent_user
        )
        crm_services.convert_lead(data["leads"][0], actor=agent_user, pipeline=pipeline)
        assert platform_selectors.dashboard(agent_user)["leads"]["conversion_rate"] == 25.0

    def test_the_window_bounds_the_figures(self, seed, agent_user):
        """A dashboard with no window reports the company's whole history, where a bad month
        is invisible against three good years."""
        data = seed(count=2)
        Lead.objects.filter(pk__in=[lead.pk for lead in data["leads"]]).update(
            created_at=timezone.now() - timezone.timedelta(days=90)
        )
        assert platform_selectors.dashboard(agent_user, days=30)["leads"]["captured"] == 0
        assert platform_selectors.dashboard(agent_user, days=180)["leads"]["captured"] == 2

    def test_overdue_leads_are_counted(self, seed, agent_user):
        data = seed(count=2)
        Lead.objects.filter(pk=data["leads"][0].pk).update(
            sla_due_at=timezone.now() - timezone.timedelta(minutes=5)
        )
        crm_services.sweep_sla()
        assert platform_selectors.dashboard(agent_user)["leads"]["overdue"] == 1

    def test_unassigned_leads_are_counted(self, seed, agent_user, owner):
        crm_services.capture_lead(
            actor=owner,
            contact_data={"first_name": "Loose", "email": "loose@example.test"},
            lead_type=Lead.LeadType.BUY,
            title="Nobody's",
            route=False,
            acknowledge=False,
        )
        assert platform_selectors.dashboard(owner)["leads"]["unassigned"] == 1

    def test_the_pipeline_block_carries_the_weighted_forecast(
        self, seed, agent_user, pipeline, make_contact
    ):
        crm_services.create_deal(
            actor=agent_user,
            pipeline=pipeline,
            primary_contact=make_contact(),
            owner=agent_user,
            title="Villa",
            deal_type="SALE",
            estimated_value=1_000_000,
            currency="AED",
        )
        data = platform_selectors.dashboard(agent_user)
        assert data["pipeline"]["open_deals"] == 1
        assert float(data["pipeline"]["weighted_forecast"]) == pytest.approx(100_000)

    def test_response_time_excludes_unanswered_leads(self, seed, agent_user):
        """Including them as zero flatters the figure; including them as "now minus capture"
        makes it drift upward on every refresh."""
        data = seed(count=2)
        crm_services.record_first_response(data["leads"][0], actor=agent_user)
        minutes = platform_selectors.dashboard(agent_user)["response"]["median_minutes"]
        assert minutes is not None
        assert minutes < 1

    def test_response_time_is_none_when_nothing_was_answered(self, seed, agent_user):
        seed(count=2)
        assert platform_selectors.dashboard(agent_user)["response"]["median_minutes"] is None

    def test_inventory_counts_are_scoped_too(self, db, make_user, make_property):
        pm = make_user("property_manager")
        make_property(managed_by=pm)
        make_property()
        assert platform_selectors.dashboard(pm)["inventory"]["properties"] == 1


# --- reports (SRS 3.13.2) ----------------------------------------------------------------------


class TestReports:
    def test_lead_source_roi_ranks_by_volume_and_reports_conversions(
        self, seed, agent_user, pipeline
    ):
        """Volume alone ranks the cheapest channel first; pairing it with conversions is what
        makes the report answer the question it is named for."""
        web = seed(count=3, source_type=LeadSource.SourceType.WEBSITE)
        seed(count=1, source_type=LeadSource.SourceType.REFERRAL)
        crm_services.change_lead_status(web["leads"][0], Lead.Status.QUALIFIED, actor=agent_user)
        crm_services.convert_lead(web["leads"][0], actor=agent_user, pipeline=pipeline)

        rows = platform_selectors.lead_source_roi(agent_user)
        assert rows[0]["source_type"] == "WEBSITE"
        assert rows[0]["leads"] == 3
        assert rows[0]["converted"] == 1
        assert rows[0]["conversion_rate"] == pytest.approx(33.3)

    def test_lead_source_roi_is_scoped(self, seed, agent_user, make_user):
        seed(count=2)
        seed(owner=make_user("agent"), count=5)
        assert sum(row["leads"] for row in platform_selectors.lead_source_roi(agent_user)) == 2

    def test_the_leaderboard_shows_one_row_to_an_agent(self, seed, agent_user, make_user):
        """Scoped like everything else: an agent running this sees their own row, which is the
        correct answer, not an empty report."""
        seed(count=2)
        seed(owner=make_user("agent"), count=3)
        rows = platform_selectors.agent_leaderboard(agent_user)
        assert len(rows) == 1
        assert rows[0]["agent_id"] == str(agent_user.id)

    def test_the_leaderboard_shows_everyone_to_an_owner(self, seed, agent_user, owner, make_user):
        seed(count=2)
        seed(owner=make_user("agent"), count=3)
        assert len(platform_selectors.agent_leaderboard(owner)) == 2

    def test_the_leaderboard_counts_won_deals(self, seed, agent_user, owner, pipeline, make_contact):
        stages = {s.code: s for s in pipeline.stages.all()}
        deal = crm_services.create_deal(
            actor=agent_user, pipeline=pipeline, primary_contact=make_contact(),
            owner=agent_user, title="Villa", deal_type="SALE",
            estimated_value=2_000_000, currency="AED",
        )
        crm_services.move_stage(deal, stages["WON"], actor=agent_user, reason="Signed.")
        row = next(
            r for r in platform_selectors.agent_leaderboard(owner)
            if r["agent_id"] == str(agent_user.id)
        )
        assert row["deals_won"] == 1
        assert row["won_value"] == 2_000_000

    def test_inventory_aging_buckets_live_listings(self, db, make_user, make_property):
        """"Eleven listings over 90 days old" is the number a manager acts on; a thousand rows
        sorted by date is not."""
        from apps.inventory import services as inventory_services
        from apps.inventory.models import Listing

        pm = make_user("property_manager")
        inventory_services.create_listing(
            actor=pm, property=make_property(managed_by=pm),
            listing_type=Listing.ListingType.SALE, title="Fresh",
            status=Listing.Status.ACTIVE,
        )
        stale = inventory_services.create_listing(
            actor=pm, property=make_property(managed_by=pm),
            listing_type=Listing.ListingType.SALE, title="Stale",
            status=Listing.Status.ACTIVE,
        )
        Listing.objects.filter(pk=stale.pk).update(
            published_at=timezone.now() - timezone.timedelta(days=200)
        )
        report = platform_selectors.inventory_aging(pm)
        assert report["buckets"]["0-30"] == 1
        assert report["buckets"]["90+"] == 1
        assert report["oldest"][0]["reference_code"] == stale.reference_code

    def test_an_unknown_report_raises(self, agent_user, db):
        with pytest.raises(KeyError):
            platform_selectors.run_report("nope", agent_user)


# --- the endpoints ---------------------------------------------------------------------------------


@pytest.mark.django_db
class TestDashboardApi:
    def test_the_dashboard_responds_scoped(self, auth_client, seed, agent_user, make_user):
        seed(count=2)
        seed(owner=make_user("agent"), count=4)
        response = auth_client(agent_user).get("/api/v1/dashboard/")
        assert response.status_code == 200, response.data
        assert response.data["leads"]["captured"] == 2

    def test_the_window_is_a_query_parameter(self, auth_client, agent_user):
        response = auth_client(agent_user).get("/api/v1/dashboard/?days=7")
        assert response.data["window_days"] == 7

    def test_an_absurd_window_is_refused(self, auth_client, agent_user):
        assert auth_client(agent_user).get("/api/v1/dashboard/?days=4000").status_code == 400

    def test_the_report_index_lists_what_exists(self, auth_client, agent_user):
        response = auth_client(agent_user).get("/api/v1/reports/")
        assert {row["name"] for row in response.data} == {
            "lead-source-roi", "agent-leaderboard", "inventory-aging",
        }

    def test_a_report_runs(self, auth_client, seed, agent_user):
        seed(count=2)
        response = auth_client(agent_user).get("/api/v1/reports/lead-source-roi/")
        assert response.status_code == 200
        assert response.data[0]["leads"] == 2

    def test_an_unknown_report_is_a_404(self, auth_client, agent_user):
        assert auth_client(agent_user).get("/api/v1/reports/nope/").status_code == 404

    def test_a_flat_report_exports_as_csv(self, auth_client, seed, agent_user):
        seed(count=2)
        response = auth_client(agent_user).get(
            "/api/v1/reports/lead-source-roi/?export=csv"
        )
        assert response.status_code == 200
        body = b"".join(response.streaming_content).decode()
        assert "conversion_rate" in body.splitlines()[0]

    def test_a_nested_report_refuses_csv_rather_than_flattening_it(
        self, auth_client, agent_user
    ):
        """Flattening it silently produces a file that does not say what the reader thinks."""
        response = auth_client(agent_user).get("/api/v1/reports/inventory-aging/?export=csv")
        assert response.status_code == 400


@pytest.mark.django_db
class TestAuditApi:
    URL = "/api/v1/audit-events/"

    def test_an_agent_sees_no_audit_log(self, auth_client, agent_user):
        """An audit log filtered by the reader's ordinary data scope would let the person who
        did something choose not to be in it. It is governance, not a record feed."""
        assert auth_client(agent_user).get(self.URL).data["count"] == 0

    def test_an_owner_reads_the_log(self, auth_client, owner, seed, agent_user):
        seed(count=1)
        response = auth_client(owner).get(self.URL)
        assert response.data["count"] > 0

    def test_it_filters_by_action_and_entity(self, auth_client, owner, seed):
        seed(count=1)
        response = auth_client(owner).get(f"{self.URL}?entity_type=LEAD&action=CREATE")
        assert all(row["entity_type"] == "LEAD" for row in response.data["results"])
