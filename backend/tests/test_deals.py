"""`crm` deals and the pipeline (SRS §3.4).

SRS 3.4.4 is the requirement that shapes this module: "The System shall require users to log a
mandatory reason and next action when a deal moves stage or is marked Lost." Mandatory means
it is refused at the service, refused at the serializer, and refused again by a CHECK
constraint — so an import or a future endpoint cannot get past it either.
"""
import pytest
from django.core.exceptions import ValidationError

from apps.crm import selectors, services
from apps.crm.models import Deal, DealStageHistory, Pipeline, PipelineStage


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def stages(pipeline):
    return {stage.code: stage for stage in pipeline.stages.all()}


@pytest.fixture
def deal(pipeline, agent_user, make_contact):
    def _make(**kwargs):
        kwargs.setdefault("primary_contact", make_contact())
        kwargs.setdefault("owner", agent_user)
        kwargs.setdefault("title", "Marina villa")
        kwargs.setdefault("deal_type", "SALE")
        kwargs.setdefault("estimated_value", 2_000_000)
        kwargs.setdefault("currency", "AED")
        return services.create_deal(actor=agent_user, pipeline=pipeline, **kwargs)

    return _make


# --- the mandatory reason (SRS 3.4.4) -------------------------------------------------------


class TestMoveStage:
    def test_a_move_records_its_reason_and_next_action(self, deal, stages, agent_user):
        d = deal()
        services.move_stage(
            d, stages["VIEWING"], actor=agent_user,
            reason="Client confirmed the viewing.", next_action="Send the offer letter",
        )
        row = d.stage_history.filter(to_stage=stages["VIEWING"]).get()
        assert row.reason == "Client confirmed the viewing."
        assert row.next_action == "Send the offer letter"

    def test_a_blank_reason_is_refused(self, deal, stages, agent_user):
        with pytest.raises(ValidationError, match="reason"):
            services.move_stage(deal(), stages["VIEWING"], actor=agent_user, reason="   ")

    def test_a_missing_reason_is_refused(self, deal, stages, agent_user):
        with pytest.raises(ValidationError):
            services.move_stage(deal(), stages["VIEWING"], actor=agent_user, reason=None)

    def test_a_stage_from_another_pipeline_is_refused(self, deal, agent_user):
        other = Pipeline.objects.create(
            name="Leasing", pipeline_type=Pipeline.PipelineType.LEASING
        )
        foreign = PipelineStage.objects.create(
            pipeline=other, name="New", code="NEW", sort_order=0, probability=10
        )
        with pytest.raises(ValidationError, match="different pipeline"):
            services.move_stage(deal(), foreign, actor=agent_user, reason="Wrong board.")

    def test_probability_follows_the_stage(self, deal, stages, agent_user):
        """The stage's probability is the pipeline's own forecast for anything sitting there,
        so it follows the move rather than being re-typed."""
        d = deal()
        services.move_stage(
            d, stages["VIEWING"], actor=agent_user, reason="Booked.",
            next_action="Collect feedback",
        )
        d.refresh_from_db()
        assert d.probability == stages["VIEWING"].probability

    def test_a_won_stage_closes_the_deal(self, deal, stages, agent_user):
        d = deal()
        services.move_stage(d, stages["WON"], actor=agent_user, reason="Contract signed.")
        d.refresh_from_db()
        assert d.status == Deal.Status.WON
        assert d.actual_close_date is not None

    def test_a_lost_stage_records_the_reason_as_the_loss_reason(self, deal, stages, agent_user):
        """SRS 3.4.5's mandatory loss reason, satisfied by 3.4.4's mandatory move reason —
        one prompt, not two."""
        d = deal()
        services.move_stage(d, stages["LOST"], actor=agent_user, reason="Bought elsewhere.")
        d.refresh_from_db()
        assert d.status == Deal.Status.LOST
        assert d.lost_reason == "Bought elsewhere."

    def test_moving_out_of_a_lost_stage_clears_the_loss_reason(self, deal, stages, agent_user):
        """The old implementation left it set, so the deal read as open while every report
        still showed why it was lost."""
        d = deal()
        services.move_stage(d, stages["LOST"], actor=agent_user, reason="Bought elsewhere.")
        services.move_stage(
            d, stages["VIEWING"], actor=agent_user, reason="They came back.",
            next_action="Re-qualify the budget",
        )
        d.refresh_from_db()
        assert d.status == Deal.Status.OPEN
        assert d.lost_reason is None
        assert d.actual_close_date is None

    def test_a_move_to_the_same_stage_is_a_no_op(self, deal, agent_user):
        d = deal()
        before = d.stage_history.count()
        services.move_stage(
            d, d.stage, actor=agent_user, reason="Nothing changed.",
            next_action="Nothing",
        )
        assert d.stage_history.count() == before

    def test_stage_entered_at_is_stamped_on_every_move(self, deal, stages, agent_user):
        """v3.3's performance change: "days in stage" becomes a subtraction rather than a scan
        of the history table for every card on the board."""
        d = deal()
        first = d.stage_entered_at
        services.move_stage(
            d, stages["VIEWING"], actor=agent_user, reason="Booked.",
            next_action="Collect feedback",
        )
        d.refresh_from_db()
        assert d.stage_entered_at > first

    def test_stage_cannot_be_moved_through_a_plain_update(self, deal, stages, agent_user):
        """Otherwise the mandatory reason and the history row are both bypassed."""
        with pytest.raises(ValidationError, match="move_stage"):
            services.update_deal(deal(), actor=agent_user, stage=stages["WON"])

    def test_the_database_refuses_an_empty_reason_too(self, deal, stages, agent_user):
        """Defence in depth: the service is the friendly refusal, the CHECK constraint is the
        one an import or a future endpoint cannot talk its way past."""
        from django.db import IntegrityError, transaction

        d = deal()
        with pytest.raises(IntegrityError), transaction.atomic():
            DealStageHistory.objects.create(
                deal=d, from_stage=d.stage, to_stage=stages["VIEWING"],
                changed_by=agent_user, reason="",
            )


# --- linked properties (SRS 3.4.7) -----------------------------------------------------------


class TestDealProperties:
    def test_several_properties_can_be_linked(self, deal, agent_user, make_property):
        d = deal()
        for _ in range(3):
            services.link_property(d, property=make_property(), actor=agent_user)
        assert d.deal_properties.count() == 3

    def test_linking_the_same_property_twice_is_a_no_op(self, deal, agent_user, make_property):
        d, prop = deal(), make_property()
        first = services.link_property(d, property=prop, actor=agent_user)
        second = services.link_property(d, property=prop, actor=agent_user)
        assert first.pk == second.pk

    def test_only_one_property_is_the_matched_one(self, deal, agent_user, make_property):
        d = deal()
        a = services.link_property(d, property=make_property(), is_primary=True, actor=agent_user)
        b = services.link_property(d, property=make_property(), is_primary=True, actor=agent_user)
        a.refresh_from_db()
        assert b.is_primary and not a.is_primary

    def test_a_unit_from_another_property_is_refused(self, deal, agent_user, make_property):
        from apps.inventory.models import Unit

        d = deal()
        a, b = make_property(), make_property()
        unit = Unit.objects.create(property=b, unit_number="101", status=Unit.Status.AVAILABLE)
        with pytest.raises(ValidationError, match="different property"):
            services.link_property(d, property=a, unit=unit, actor=agent_user)


# --- the board (SRS 3.4.6) ---------------------------------------------------------------------


class TestBoard:
    def test_the_board_buckets_deals_by_stage(self, deal, stages, agent_user, pipeline):
        a, b = deal(), deal()
        services.move_stage(
            b, stages["VIEWING"], actor=agent_user, reason="Booked.", next_action="Feedback"
        )
        data = selectors.board(agent_user, pipeline=pipeline)
        by_code = {row["stage"].code: row for row in data["stages"]}
        assert [d.id for d in by_code["NEW"]["deals"]] == [a.id]
        assert [d.id for d in by_code["VIEWING"]["deals"]] == [b.id]

    def test_every_stage_appears_even_when_empty(self, deal, agent_user, pipeline):
        """A Kanban column that vanishes when it empties is a column nobody can drag into."""
        deal()
        data = selectors.board(agent_user, pipeline=pipeline)
        assert len(data["stages"]) == pipeline.stages.count()

    def test_the_board_carries_per_stage_counts_and_value(self, deal, agent_user, pipeline):
        deal(estimated_value=1_000_000)
        deal(estimated_value=3_000_000)
        data = selectors.board(agent_user, pipeline=pipeline)
        new_bucket = next(r for r in data["stages"] if r["stage"].code == "NEW")
        assert new_bucket["count"] == 2
        assert new_bucket["value"] == 4_000_000

    def test_days_in_stage_is_a_subtraction_not_a_history_scan(self, deal, agent_user, pipeline):
        data = selectors.board(agent_user, pipeline=pipeline)
        deal()
        data = selectors.board(agent_user, pipeline=pipeline)
        card = data["stages"][0]["deals"][0]
        assert card.days_in_stage == 0

    def test_the_board_is_scoped(self, deal, agent_user, make_user, pipeline):
        """A board is a *view* of the deals the caller may already see, never a second way to
        reach them."""
        deal()
        other = make_user("agent")
        data = selectors.board(other, pipeline=pipeline)
        assert all(row["count"] == 0 for row in data["stages"])

    def test_the_weighted_forecast_is_computed_in_sql(self, deal, stages, agent_user):
        """SRS 3.4.3. Pulling a thousand deals across the wire to multiply two columns is the
        difference between a dashboard that loads and one that times out."""
        d = deal(estimated_value=1_000_000)  # NEW stage: 10%
        services.move_stage(
            d, stages["VIEWING"], actor=agent_user, reason="Booked.",
            next_action="Collect feedback",
        )  # 45%
        deal(estimated_value=2_000_000)  # NEW: 10%
        forecast = selectors.weighted_pipeline(
            selectors.visible_deals(agent_user).filter(status=Deal.Status.OPEN)
        )
        assert float(forecast) == pytest.approx(1_000_000 * 0.45 + 2_000_000 * 0.10)


# --- the endpoints ------------------------------------------------------------------------------


@pytest.mark.django_db
class TestDealApi:
    URL = "/api/v1/deals/"

    def test_moving_without_a_reason_is_a_400(self, auth_client, deal, stages, agent_user):
        d = deal()
        response = auth_client(agent_user).post(
            f"{self.URL}{d.id}/move/", {"stage": str(stages["VIEWING"].id)}, format="json"
        )
        assert response.status_code == 400
        assert "reason" in str(response.data)

    def test_moving_with_a_reason_works(self, auth_client, deal, stages, agent_user):
        d = deal()
        response = auth_client(agent_user).post(
            f"{self.URL}{d.id}/move/",
            {
                "stage": str(stages["VIEWING"].id),
                "reason": "Client confirmed the viewing.",
                "next_action": "Send the offer letter",
            },
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["stage"]["code"] == "VIEWING"
        assert response.data["days_in_stage"] == 0

    def test_the_stage_history_is_readable(self, auth_client, deal, stages, agent_user):
        d = deal()
        services.move_stage(
            d, stages["VIEWING"], actor=agent_user, reason="Booked.",
            next_action="Collect feedback",
        )
        response = auth_client(agent_user).get(f"{self.URL}{d.id}/stage-history/")
        assert [row["reason"] for row in response.data] == ["Booked.", "Created."]

    def test_the_board_endpoint_composes_with_filters(
        self, auth_client, deal, agent_user, make_user
    ):
        mine = deal()
        theirs = deal(owner=make_user("agent"))
        response = auth_client(agent_user).get(f"{self.URL}board/?owner={agent_user.id}")
        assert response.status_code == 200, response.data
        ids = [d["id"] for row in response.data["stages"] for d in row["deals"]]
        assert str(mine.id) in ids
        assert str(theirs.id) not in ids

    def test_the_board_carries_the_weighted_forecast(self, auth_client, deal, agent_user):
        deal(estimated_value=1_000_000)
        response = auth_client(agent_user).get(f"{self.URL}board/")
        assert float(response.data["weighted_forecast"]) == pytest.approx(100_000)

    def test_a_deal_outside_scope_is_a_404(self, auth_client, deal, make_user):
        d = deal()
        assert auth_client(make_user("agent")).get(f"{self.URL}{d.id}/").status_code == 404

    def test_properties_are_linked_through_their_own_endpoint(
        self, auth_client, deal, agent_user, make_property, make_user
    ):
        pm = make_user("property_manager")
        prop = make_property(managed_by=pm)
        d = deal()
        # The agent cannot see that property, so the link is refused rather than confirming it.
        refused = auth_client(agent_user).post(
            f"{self.URL}{d.id}/properties/", {"property": str(prop.id)}, format="json"
        )
        assert refused.status_code == 404


class TestNextActionIsMandatoryToo:
    """SRS 3.4.4 asks for "a mandatory reason **and next action**". The first pass enforced
    only the reason."""

    def test_a_move_to_an_open_stage_needs_a_next_action(self, deal, stages, agent_user):
        with pytest.raises(ValidationError, match="what happens next"):
            services.move_stage(
                deal(), stages["VIEWING"], actor=agent_user, reason="Viewing booked."
            )

    def test_a_blank_next_action_does_not_count(self, deal, stages, agent_user):
        with pytest.raises(ValidationError):
            services.move_stage(
                deal(), stages["VIEWING"], actor=agent_user, reason="Booked.", next_action="  "
            )

    def test_a_terminal_stage_needs_none(self, deal, stages, agent_user):
        """There is nothing next by definition. Demanding one would train users to type
        "n/a", which is how a mandatory field stops meaning anything."""
        d = deal()
        services.move_stage(d, stages["WON"], actor=agent_user, reason="Contract signed.")
        d.refresh_from_db()
        assert d.status == Deal.Status.WON

    def test_a_terminal_move_clears_the_standing_next_action(self, deal, stages, agent_user):
        d = deal()
        services.move_stage(
            d, stages["VIEWING"], actor=agent_user, reason="Booked.",
            next_action="Send the offer letter",
        )
        services.move_stage(d, stages["LOST"], actor=agent_user, reason="Bought elsewhere.")
        d.refresh_from_db()
        assert d.next_action is None

    def test_the_endpoint_refuses_it_too(self, auth_client, deal, stages, agent_user):
        d = deal()
        response = auth_client(agent_user).post(
            f"/api/v1/deals/{d.id}/move/",
            {"stage": str(stages["VIEWING"].id), "reason": "Booked."},
            format="json",
        )
        assert response.status_code == 400
        assert "next_action" in str(response.data)


class TestBoardQueryParameters:
    """A raw query parameter reached Django's UUID field and raised a bare
    `django.core.exceptions.ValidationError`, which DRF does not render — a 500 from any
    authenticated user with a typo in the query string."""

    def test_a_malformed_owner_is_a_400_not_a_500(self, auth_client, deal, agent_user):
        deal()
        assert auth_client(agent_user).get(
            "/api/v1/deals/board/?owner=not-a-uuid"
        ).status_code == 400

    def test_a_malformed_pipeline_is_a_400(self, auth_client, deal, agent_user):
        deal()
        assert auth_client(agent_user).get(
            "/api/v1/deals/board/?pipeline=not-a-uuid"
        ).status_code == 400

    def test_a_valid_filter_still_works(self, auth_client, deal, agent_user):
        d = deal()
        response = auth_client(agent_user).get(f"/api/v1/deals/board/?owner={agent_user.id}")
        assert response.status_code == 200
        ids = [card["id"] for row in response.data["stages"] for card in row["deals"]]
        assert str(d.id) in ids


class TestUnlinkGoesThroughTheService:
    def test_a_link_is_removed_through_its_endpoint(
        self, auth_client, deal, agent_user, make_property, make_user
    ):
        from apps.crm.models import DealProperty

        d = deal()
        link = services.link_property(
            d, property=make_property(managed_by=make_user("property_manager")), actor=agent_user
        )
        assert auth_client(agent_user).delete(
            f"/api/v1/deal-properties/{link.id}/"
        ).status_code == 204
        assert not DealProperty.objects.filter(pk=link.pk).exists()

    def test_a_link_on_an_invisible_deal_is_a_404(
        self, auth_client, deal, agent_user, make_user, make_property
    ):
        link = services.link_property(
            deal(), property=make_property(managed_by=make_user("property_manager")),
            actor=agent_user,
        )
        assert auth_client(make_user("agent")).delete(
            f"/api/v1/deal-properties/{link.id}/"
        ).status_code == 404
