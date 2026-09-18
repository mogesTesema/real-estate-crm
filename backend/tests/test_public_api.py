"""The unauthenticated /api/public/ surface (SRS 3.1.1 "Website", 3.8.2, 3.19.4).

Posture under test: ACTIVE-only rows, whitelist serialization (no agents, no commission,
no exact address, blurred coordinates), honeypot and enumeration-proof 202s on the inquiry
door, and the full capture pipeline behind a valid submission.
"""
import pytest

from apps.crm import services as crm_services
from apps.crm.models import Lead
from apps.inventory import services as inventory_services


@pytest.fixture
def active_listing(make_property, make_user):
    pm = make_user("property_manager")

    def _make(**kwargs):
        prop = make_property(
            bedrooms=kwargs.pop("bedrooms", 2),
            city=kwargs.pop("city", "Dubai"),
            latitude=kwargs.pop("latitude", None),
            longitude=kwargs.pop("longitude", None),
        )
        listing = inventory_services.create_listing(
            actor=pm, property=prop,
            listing_type=kwargs.pop("listing_type", "SALE"),
            title=kwargs.pop("title", "Marina view 2BR"),
            asking_price=kwargs.pop("asking_price", 1500000),
            **kwargs,
        )
        inventory_services.change_listing_status(listing, "ACTIVE", actor=pm)
        return listing

    return _make


class TestPublicListings:
    def test_only_active_listings_are_published(
        self, db, api_client, active_listing, make_property, make_user
    ):
        live = active_listing()
        pm = make_user("property_manager")
        inventory_services.create_listing(  # DRAFT — not public
            actor=pm, property=make_property(), listing_type="SALE",
            title="Not ready", asking_price=1,
        )
        response = api_client.get("/api/public/listings/")
        assert response.status_code == 200
        ids = {row["id"] for row in response.data["results"]}
        assert ids == {str(live.pk)}

    def test_the_card_is_a_strict_whitelist(self, db, api_client, active_listing):
        listing = active_listing(latitude="25.123456", longitude="55.654321")
        response = api_client.get(f"/api/public/listings/{listing.pk}/")
        assert response.status_code == 200
        card = response.data
        for forbidden in (
            "assigned_agent", "co_listing_agent", "commission_rate", "custom_data",
            "address_line_1", "reference_code",
        ):
            assert forbidden not in card
        # Coordinates are blurred to ~100 m.
        assert card["latitude"] == 25.123
        assert card["longitude"] == 55.654

    def test_filters_narrow_the_feed(self, db, api_client, active_listing):
        active_listing(city="Dubai", title="Dubai flat")
        active_listing(city="Sharjah", title="Sharjah flat")
        rows = api_client.get("/api/public/listings/?city=dubai").data["results"]
        assert [row["title"] for row in rows] == ["Dubai flat"]

    def test_no_authentication_is_required(self, db, api_client):
        assert api_client.get("/api/public/listings/").status_code == 200


class TestPublicInquiries:
    def test_a_valid_inquiry_runs_the_full_pipeline(
        self, db, api_client, active_listing
    ):
        listing = active_listing()
        response = api_client.post(
            "/api/public/inquiries/",
            {
                "name": "Zainab Al Farsi",
                "email": "zainab@example.test",
                "message": "Is this still available?",
                "listing": str(listing.pk),
            },
        )
        assert response.status_code == 202
        lead = Lead.objects.get()
        assert lead.contact.first_name == "Zainab"
        assert lead.contact.email == "zainab@example.test"
        assert lead.target_property == listing.property
        assert lead.source.name == "Website"
        assert lead.score > 0  # scoring ran
        assert lead.sla_due_at is not None  # the SLA clock started

    def test_the_honeypot_swallows_bots(self, db, api_client):
        response = api_client.post(
            "/api/public/inquiries/",
            {"name": "Bot", "email": "bot@spam.test", "website": "http://spam"},
        )
        assert response.status_code == 202  # same answer a human gets
        assert Lead.objects.count() == 0    # but nothing happened

    def test_an_invalid_listing_id_is_no_oracle(self, db, api_client):
        response = api_client.post(
            "/api/public/inquiries/",
            {
                "email": "curious@example.test",
                "listing": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert response.status_code == 202  # generic accept, no existence signal
        lead = Lead.objects.get()
        assert lead.target_property is None  # captured without the bogus link

    def test_no_contact_route_is_the_one_visible_refusal(self, db, api_client):
        response = api_client.post("/api/public/inquiries/", {"name": "Ghost"})
        assert response.status_code == 400

    def test_a_rental_listing_maps_to_a_rent_lead(
        self, db, api_client, active_listing
    ):
        listing = active_listing(
            listing_type="RENT", asking_price=None, rent_amount=90000,
            title="Rental unit",
        )
        api_client.post(
            "/api/public/inquiries/",
            {"email": "tenant@example.test", "listing": str(listing.pk)},
        )
        assert Lead.objects.get().lead_type == "RENT_IN"


class TestPublicLandingPages:
    @pytest.fixture
    def page(self, make_user):
        marketer = make_user("marketing")
        return crm_services.create_landing_page(
            actor=marketer, slug="dubai-launch", title="Dubai Launch",
            is_published=True,
            content={"hero": "New towers"},
            form_config={"fields": [
                {"name": "name", "required": True},
                {"name": "email", "required": True},
            ]},
        )

    def test_get_bumps_views_and_hides_internals(self, db, api_client, page):
        response = api_client.get("/api/public/pages/dubai-launch/")
        assert response.status_code == 200
        assert response.data["title"] == "Dubai Launch"
        assert "lead_source" not in response.data
        page.refresh_from_db()
        assert page.views == 1

    def test_an_unpublished_page_is_a_404(self, db, api_client, page, make_user):
        crm_services.update_landing_page(
            page, actor=make_user("marketing"), is_published=False
        )
        assert api_client.get("/api/public/pages/dubai-launch/").status_code == 404

    def test_submit_captures_and_counts(self, db, api_client, page):
        response = api_client.post(
            "/api/public/pages/dubai-launch/submit/",
            {"name": "Omar Aziz", "email": "omar@example.test"},
        )
        assert response.status_code == 202
        assert Lead.objects.count() == 1
        page.refresh_from_db()
        assert page.submissions == 1

    def test_the_forms_own_validation_is_shown(self, db, api_client, page):
        response = api_client.post(
            "/api/public/pages/dubai-launch/submit/", {"name": "No Email"}
        )
        assert response.status_code == 400
        assert "email" in response.data
