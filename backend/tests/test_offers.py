"""Offers, transactions and closing checklists (SRS 3.6.1, 3.6.6).

The doctrine: submitted offers are immutable (withdraw or counter, never edit); acceptance
rejects every other open offer on the deal; a transaction cannot COMPLETE while a required
checklist item is open.
"""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.crm import services
from apps.crm.models import DealProperty, Offer, Transaction


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def deal_with_property(make_deal, make_property, agent_user):
    deal = make_deal(owner=agent_user)
    DealProperty.objects.create(deal=deal, property=make_property(), is_primary=True)
    return deal


def make_offer(deal, **kwargs):
    kwargs.setdefault("direction", "BUYER_TO_SELLER")
    kwargs.setdefault("amount", 950000)
    return services.create_offer(actor=deal.owner, deal=deal, **kwargs)


class TestOfferLifecycle:
    def test_an_offer_defaults_to_the_primary_property_and_contact(
        self, db, deal_with_property
    ):
        offer = make_offer(deal_with_property)
        assert offer.status == Offer.Status.SUBMITTED
        assert offer.property == deal_with_property.deal_properties.get().property
        assert offer.offered_by_contact == deal_with_property.primary_contact

    def test_a_deal_without_a_property_refuses_offers(self, db, make_deal, agent_user):
        with pytest.raises(ValidationError, match="Link a property"):
            services.create_offer(
                actor=agent_user, deal=make_deal(), direction="BUYER_TO_SELLER",
                amount=1000,
            )

    def test_counter_chains_and_flips_direction(self, db, deal_with_property, agent_user):
        offer = make_offer(deal_with_property)
        counter = services.counter_offer(offer, actor=agent_user, amount=1000000)
        offer.refresh_from_db()
        assert offer.status == Offer.Status.COUNTERED
        assert offer.responded_at is not None
        assert counter.parent_offer == offer
        assert counter.direction == Offer.Direction.SELLER_TO_BUYER
        assert counter.status == Offer.Status.SUBMITTED
        # The countered parent is terminal — it cannot be accepted afterwards.
        with pytest.raises(ValidationError, match="Cannot move"):
            services.accept_offer(offer, actor=agent_user)

    def test_acceptance_rejects_every_other_open_offer(
        self, db, deal_with_property, agent_user
    ):
        """Two live offers after an acceptance would let a second acceptance contradict
        the first."""
        chosen = make_offer(deal_with_property)
        rival = make_offer(deal_with_property, amount=900000)
        draft = make_offer(deal_with_property, amount=800000, submit=False)

        services.accept_offer(chosen, actor=agent_user)
        rival.refresh_from_db(); draft.refresh_from_db(); chosen.refresh_from_db()
        assert chosen.status == Offer.Status.ACCEPTED
        assert rival.status == Offer.Status.REJECTED
        assert draft.status == Offer.Status.REJECTED
        assert rival.responded_at is not None

    def test_acceptance_is_audited_via_the_signal(
        self, db, deal_with_property, agent_user
    ):
        from apps.platform.models import AuditEvent

        offer = make_offer(deal_with_property)
        services.accept_offer(offer, actor=agent_user)
        event = AuditEvent.objects.filter(
            entity_type="DEAL", entity_id=deal_with_property.pk,
            new_values__offer_id=str(offer.pk),
        )
        assert event.exists()

    def test_mark_deal_won_reads_the_accepted_offer(
        self, db, deal_with_property, agent_user, pipeline
    ):
        """The cross-phase seam: the transaction's gross comes from the negotiation, not
        the estimate, when an accepted offer exists."""
        offer = make_offer(deal_with_property, amount=925000)
        services.accept_offer(offer, actor=agent_user)
        won = pipeline.stages.get(code="WON")
        services.move_stage(
            deal_with_property, won, actor=agent_user, reason="Signed."
        )
        txn = deal_with_property.transactions.get()
        assert txn.gross_amount == offer.amount

    def test_sweep_expires_only_stale_submitted_offers(
        self, db, deal_with_property, agent_user
    ):
        from django.core.management import call_command

        stale = make_offer(
            deal_with_property, expires_at=timezone.now() - timedelta(days=1)
        )
        fresh = make_offer(
            deal_with_property, amount=900000,
            expires_at=timezone.now() + timedelta(days=5),
        )
        call_command("sweep_offers")
        call_command("sweep_offers")  # run-twice-changes-nothing
        stale.refresh_from_db(); fresh.refresh_from_db()
        assert stale.status == Offer.Status.EXPIRED
        assert fresh.status == Offer.Status.SUBMITTED

    def test_the_api_has_no_update_route(self, db, auth_client, deal_with_property):
        offer = make_offer(deal_with_property)
        response = auth_client(deal_with_property.owner).patch(
            f"/api/v1/offers/{offer.pk}/", {"amount": 1}
        )
        assert response.status_code == 405


class TestTransactionGate:
    @pytest.fixture
    def won_transaction(self, deal_with_property, agent_user, pipeline):
        won = pipeline.stages.get(code="WON")
        services.move_stage(deal_with_property, won, actor=agent_user, reason="Signed.")
        return deal_with_property.transactions.get()

    def test_the_checklist_gate_blocks_completion(
        self, db, won_transaction, agent_user
    ):
        checklist = services.create_checklist(
            actor=agent_user, checklist_type="SALE", name="Closing",
            transaction_obj=won_transaction,
            items=[
                {"title": "Signed contract on file", "is_required": True},
                {"title": "Congratulatory basket", "is_required": False},
            ],
        )
        services.change_transaction_status(
            won_transaction, "CONTRACTED", actor=agent_user
        )
        with pytest.raises(ValidationError, match="Signed contract"):
            services.change_transaction_status(
                won_transaction, "COMPLETED", actor=agent_user
            )
        # Optional items do not gate; completing the required one opens the door.
        required = checklist.items.get(title="Signed contract on file")
        services.complete_checklist_item(required, actor=agent_user)
        services.change_transaction_status(
            won_transaction, "COMPLETED", actor=agent_user
        )
        won_transaction.refresh_from_db()
        assert won_transaction.status == Transaction.Status.COMPLETED
        assert won_transaction.closing_date is not None

    def test_a_cancelled_checklist_stops_gating(self, db, won_transaction, agent_user):
        checklist = services.create_checklist(
            actor=agent_user, checklist_type="SALE", name="Abandoned",
            transaction_obj=won_transaction,
            items=[{"title": "Never done", "is_required": True}],
        )
        services.cancel_checklist(checklist, actor=agent_user)
        services.change_transaction_status(
            won_transaction, "CONTRACTED", actor=agent_user
        )
        services.change_transaction_status(
            won_transaction, "COMPLETED", actor=agent_user
        )

    def test_cancelling_requires_a_reason_and_keeps_it(
        self, db, won_transaction, agent_user
    ):
        with pytest.raises(ValidationError, match="reason"):
            services.change_transaction_status(
                won_transaction, "CANCELLED", actor=agent_user
            )
        services.change_transaction_status(
            won_transaction, "CANCELLED", actor=agent_user, reason="Buyer financing fell."
        )
        won_transaction.refresh_from_db()
        assert "Buyer financing fell." in won_transaction.notes
        with pytest.raises(ValidationError, match="Cannot move"):
            services.change_transaction_status(
                won_transaction, "COMPLETED", actor=agent_user
            )

    def test_the_api_cannot_create_transactions(self, db, auth_client, agent_user):
        # 405 (no create route) or 403 (matrix code checked first) — either way, closed.
        assert auth_client(agent_user).post(
            "/api/v1/transactions/", {}
        ).status_code in (403, 405)


class TestChecklists:
    def test_checklist_completion_enforces_required_items(
        self, db, deal_with_property, agent_user
    ):
        checklist = services.create_checklist(
            actor=agent_user, checklist_type="SALE", name="Deal-side",
            deal=deal_with_property,
            items=[{"title": "KYC", "is_required": True}],
        )
        with pytest.raises(ValidationError, match="KYC"):
            services.complete_checklist(checklist, actor=agent_user)
        services.complete_checklist_item(checklist.items.get(), actor=agent_user)
        services.complete_checklist(checklist, actor=agent_user)
        checklist.refresh_from_db()
        assert checklist.status == "COMPLETED"
        # A closed checklist refuses new items and re-ticks.
        with pytest.raises(ValidationError, match="closed"):
            services.add_checklist_item(checklist, actor=agent_user, title="Late")

    def test_an_item_can_attach_its_document(
        self, db, auth_client, deal_with_property, agent_user, make_contact
    ):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.collaboration import services as collab

        file = collab.store_file(
            actor=agent_user,
            uploaded_file=SimpleUploadedFile(
                "contract.pdf", b"%PDF", content_type="application/pdf"
            ),
        )
        document = collab.create_document(
            actor=agent_user, file=file, title="Signed contract",
            document_type="SALE_CONTRACT", links=[{"deal": deal_with_property}],
        )
        checklist = services.create_checklist(
            actor=agent_user, checklist_type="SALE", name="Docs",
            deal=deal_with_property,
            items=[{"title": "Contract uploaded"}],
        )
        item = checklist.items.get()
        response = auth_client(agent_user).post(
            f"/api/v1/closing-checklists/{checklist.pk}/items/{item.pk}/complete/",
            {"document": str(document.pk)},
        )
        assert response.status_code == 200, response.data
        item.refresh_from_db()
        assert item.document == document
        assert item.completed_by == agent_user

    def test_scoping_follows_the_deal(
        self, db, auth_client, deal_with_property, agent_user, make_user
    ):
        services.create_checklist(
            actor=agent_user, checklist_type="SALE", name="Private",
            deal=deal_with_property,
        )
        make_offer(deal_with_property)
        stranger = auth_client(make_user("agent"))
        assert stranger.get("/api/v1/closing-checklists/").data["results"] == []
        assert stranger.get("/api/v1/offers/").data["results"] == []
        mine = auth_client(agent_user)
        assert len(mine.get("/api/v1/closing-checklists/").data["results"]) == 1
        assert len(mine.get("/api/v1/offers/").data["results"]) == 1
