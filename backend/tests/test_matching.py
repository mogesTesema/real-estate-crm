"""The matching engine (SRS 3.3.8): lead → listings and listing → leads.

Both sides tolerate missing data — "no answer yet" is not "matches nothing" — and the
reverse path is apply_scope'd, so an agent only sees matches among their own leads.
"""
import pytest

from apps.crm import selectors, services
from apps.inventory import services as inventory_services


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def active_listing(make_property, make_user):
    pm = make_user("property_manager")

    def _make(price=1000000, listing_type="SALE", bedrooms=2, city="Dubai", **kwargs):
        prop = make_property(bedrooms=bedrooms, city=city)
        price_field = "rent_amount" if listing_type == "RENT" else "asking_price"
        listing = inventory_services.create_listing(
            actor=pm, property=prop, listing_type=listing_type,
            title=kwargs.pop("title", f"{city} {bedrooms}BR"),
            **{price_field: price}, **kwargs,
        )
        inventory_services.change_listing_status(listing, "ACTIVE", actor=pm)
        return listing

    return _make


def make_lead(agent, make_contact, **kwargs):
    kwargs.setdefault("lead_type", "BUY")
    return services.capture_lead(
        actor=agent, contact=make_contact(), acknowledge=False, route=False,
        assigned_agent=agent, **kwargs,
    )


class TestForward:
    def test_budget_window_has_ten_percent_tolerance(
        self, db, agent_user, make_contact, active_listing
    ):
        inside = active_listing(price=1050000)   # 5% over the 1M max — still shown
        outside = active_listing(price=1200000)  # 20% over — not shown
        lead = make_lead(
            agent_user, make_contact, budget_min=500000, budget_max=1000000
        )
        matches = set(selectors.matching_listings_for_lead(lead))
        assert inside in matches
        assert outside not in matches

    def test_supply_side_leads_match_nothing(
        self, db, agent_user, make_contact, active_listing
    ):
        active_listing()
        seller = make_lead(agent_user, make_contact, lead_type="SELL")
        assert list(selectors.matching_listings_for_lead(seller)) == []

    def test_rent_leads_match_rent_listings_on_rent_price(
        self, db, agent_user, make_contact, active_listing
    ):
        rental = active_listing(price=80000, listing_type="RENT")
        sale = active_listing(price=80000, listing_type="SALE")
        lead = make_lead(agent_user, make_contact, lead_type="RENT_IN", budget_max=90000)
        matches = set(selectors.matching_listings_for_lead(lead))
        assert rental in matches
        assert sale not in matches

    def test_bedrooms_are_a_floor_and_location_text_filters(
        self, db, agent_user, make_contact, active_listing
    ):
        big = active_listing(bedrooms=3, city="Dubai")
        small = active_listing(bedrooms=1, city="Dubai")
        elsewhere = active_listing(bedrooms=3, city="Sharjah")
        lead = make_lead(
            agent_user, make_contact, preferred_bedrooms=2,
            locations=[{"location": "Dubai"}],
        )
        matches = set(selectors.matching_listings_for_lead(lead))
        assert big in matches
        assert small not in matches
        assert elsewhere not in matches

    def test_a_draft_listing_never_matches(
        self, db, agent_user, make_contact, make_property, make_user
    ):
        pm = make_user("property_manager")
        inventory_services.create_listing(
            actor=pm, property=make_property(), listing_type="SALE",
            title="Still drafting", asking_price=1,
        )
        lead = make_lead(agent_user, make_contact)
        assert list(selectors.matching_listings_for_lead(lead)) == []


class TestReverse:
    def test_matching_leads_are_scoped_to_the_caller(
        self, db, agent_user, make_user, make_contact, active_listing
    ):
        listing = active_listing(price=900000)
        mine = make_lead(agent_user, make_contact, budget_max=1000000)
        other_agent = make_user("agent")
        make_lead(other_agent, make_contact, budget_max=1000000)  # not mine

        found = selectors.matching_leads_for_listing(listing, agent_user)
        assert mine in found
        assert len(found) == 1  # the other agent's lead is invisible to OWN scope

    def test_closed_leads_are_out_of_the_market(
        self, db, agent_user, make_contact, active_listing
    ):
        listing = active_listing()
        lead = make_lead(agent_user, make_contact)
        services.change_lead_status(lead, "CONTACTED", actor=agent_user)
        services.change_lead_status(lead, "LOST", actor=agent_user, reason="Went quiet.")
        assert selectors.matching_leads_for_listing(listing, agent_user) == []


class TestEndpoints:
    def test_the_match_card_is_the_safe_card(
        self, db, auth_client, agent_user, make_contact, active_listing
    ):
        active_listing(price=950000)
        lead = make_lead(agent_user, make_contact, budget_max=1000000)
        response = auth_client(agent_user).get(f"/api/v1/leads/{lead.pk}/matches/")
        assert response.status_code == 200
        assert len(response.data) == 1
        card = response.data[0]
        assert "asking_price" in card and "city" in card
        # Nothing scope-sensitive leaks on the card.
        for forbidden in ("assigned_agent", "commission_rate", "custom_data",
                          "address_line_1"):
            assert forbidden not in card

    def test_matching_leads_endpoint_requires_listing_visibility(
        self, db, auth_client, agent_user, owner, active_listing
    ):
        """The listing itself is scoped: an OWN-scope agent with no claim on it gets a
        404, an ALL-scope owner gets the (scoped) lead list."""
        listing = active_listing()
        assert auth_client(agent_user).get(
            f"/api/v1/listings/{listing.pk}/matching-leads/"
        ).status_code == 404
        assert auth_client(owner).get(
            f"/api/v1/listings/{listing.pk}/matching-leads/"
        ).status_code == 200
