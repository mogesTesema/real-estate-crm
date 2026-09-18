"""`inventory` — properties, units, listings, media (SRS §3.3).

The status state machine gets the most attention: it is the rule that is silently wrong when
a transition table drifts, and every legal *and* illegal move is enumerated here rather than
spot-checked, because "can a SOLD property go back to AVAILABLE?" has to have one answer.
"""
import pytest
from django.core.exceptions import ValidationError

from apps.inventory import selectors, services
from apps.inventory.models import Listing, Media, Property, PropertyStatusHistory, Unit


@pytest.fixture
def pm(make_user):
    return make_user("property_manager")


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def new_property(property_type, pm):
    def _make(actor=None, **kwargs):
        kwargs.setdefault("property_type", property_type)
        kwargs.setdefault("managed_by", pm)
        kwargs.setdefault("title", "Marina Villa")
        kwargs.setdefault("address_line_1", "12 Marina Walk")
        kwargs.setdefault("city", "Dubai")
        kwargs.setdefault("country", "AE")
        return services.create_property(actor=actor or pm, **kwargs)

    return _make


# --- reference codes ----------------------------------------------------------------------


def test_reference_codes_do_not_repeat(db, new_property, pm):
    """architecture.md §2 forbids MAX()+1 — and not theoretically. Two agents publishing in
    the same second both read the same maximum and the loser hits the partial unique index on
    reference_code: a 500 on a perfectly valid action."""
    prop = new_property()
    codes = {
        services.create_listing(
            actor=pm,
            property=prop,
            listing_type=Listing.ListingType.SALE,
            title=f"Listing {n}",
        ).reference_code
        for n in range(5)
    }
    assert len(codes) == 5
    assert all(code.startswith("LST-") for code in codes)


# --- the availability state machine (SRS 3.3.4) --------------------------------------------


class TestPropertyStatus:
    def test_creation_writes_the_opening_history_row(self, db, new_property):
        prop = new_property()
        row = prop.status_history.get()
        assert row.from_status is None
        assert row.to_status == Property.Status.DRAFT

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (Property.Status.DRAFT, Property.Status.AVAILABLE),
            (Property.Status.AVAILABLE, Property.Status.OCCUPIED),
            (Property.Status.AVAILABLE, Property.Status.SOLD),
            (Property.Status.OCCUPIED, Property.Status.AVAILABLE),
            (Property.Status.UNDER_MAINTENANCE, Property.Status.AVAILABLE),
            (Property.Status.ARCHIVED, Property.Status.AVAILABLE),
        ],
    )
    def test_legal_moves_are_allowed_and_recorded(self, db, new_property, pm, start, target):
        prop = new_property()
        Property.objects.filter(pk=prop.pk).update(status=start)
        prop.refresh_from_db()
        services.change_status(prop, target, actor=pm, reason="Test move.")
        prop.refresh_from_db()
        assert prop.status == target
        assert prop.status_history.filter(from_status=start, to_status=target).exists()

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (Property.Status.DRAFT, Property.Status.SOLD),
            (Property.Status.DRAFT, Property.Status.OCCUPIED),
            (Property.Status.SOLD, Property.Status.AVAILABLE),
            (Property.Status.SOLD, Property.Status.ARCHIVED),
            (Property.Status.ARCHIVED, Property.Status.SOLD),
        ],
    )
    def test_illegal_moves_are_refused(self, db, new_property, pm, start, target):
        prop = new_property()
        Property.objects.filter(pk=prop.pk).update(status=start)
        prop.refresh_from_db()
        with pytest.raises(ValidationError):
            services.change_status(prop, target, actor=pm)

    def test_sold_is_terminal_and_says_so(self, db, new_property, pm):
        """A sold property returning to market is a new mandate, not an edit of the old one —
        reopening it in place silently rewrites the history the commission hangs off."""
        prop = new_property()
        Property.objects.filter(pk=prop.pk).update(status=Property.Status.SOLD)
        prop.refresh_from_db()
        with pytest.raises(ValidationError, match="terminal"):
            services.change_status(prop, Property.Status.AVAILABLE, actor=pm)

    def test_a_self_transition_writes_no_history(self, db, new_property, pm):
        """Re-saving a form without touching the dropdown should not manufacture an audit row
        that says nothing happened."""
        prop = new_property()
        before = prop.status_history.count()
        services.change_status(prop, prop.status, actor=pm)
        assert prop.status_history.count() == before

    def test_an_unknown_status_is_refused(self, db, new_property, pm):
        with pytest.raises(ValidationError):
            services.change_status(new_property(), "PENDING", actor=pm)

    def test_status_cannot_be_set_through_a_plain_update(self, db, new_property, pm):
        """Otherwise the transition table and the history row are both bypassed by any client
        that simply patches the field."""
        prop = new_property()
        with pytest.raises(ValidationError):
            services.update_property(prop, actor=pm, status=Property.Status.SOLD)

    def test_the_change_is_audited(self, db, new_property, pm):
        from apps.platform.models import AuditEvent

        prop = new_property()
        services.change_status(prop, Property.Status.AVAILABLE, actor=pm, reason="Ready.")
        event = AuditEvent.objects.filter(entity_type="PROPERTY", entity_id=prop.pk).first()
        assert event.new_values["status"] == "AVAILABLE"
        assert event.new_values["reason"] == "Ready."


class TestUnitStatus:
    def test_units_have_their_own_machine(self, db, new_property, pm):
        prop = new_property()
        unit = services.create_unit(actor=pm, property=prop, unit_number="101")
        services.change_status(unit, Unit.Status.RESERVED, actor=pm)
        unit.refresh_from_db()
        assert unit.status == Unit.Status.RESERVED
        assert PropertyStatusHistory.objects.filter(unit=unit, to_status="RESERVED").exists()

    def test_a_sold_unit_is_terminal(self, db, new_property, pm):
        prop = new_property()
        unit = services.create_unit(actor=pm, property=prop, unit_number="102")
        services.change_status(unit, Unit.Status.SOLD, actor=pm)
        with pytest.raises(ValidationError):
            services.change_status(unit, Unit.Status.AVAILABLE, actor=pm)

    def test_adding_a_unit_marks_the_property_multi_unit(self, db, new_property, pm):
        """A property with units is multi-unit by definition; leaving the flag off makes the
        unit invisible to anything that branches on it."""
        prop = new_property()
        assert prop.is_multi_unit is False
        services.create_unit(actor=pm, property=prop, unit_number="103")
        prop.refresh_from_db()
        assert prop.is_multi_unit is True


# --- mandatory property manager (SRS 3.3.10) -----------------------------------------------


class TestPropertyManager:
    def test_a_property_without_a_manager_is_refused(self, db, property_type, pm):
        with pytest.raises(ValidationError, match="Property Manager"):
            services.create_property(
                actor=pm,
                property_type=property_type,
                title="Orphan",
                address_line_1="1 Nowhere",
                city="Dubai",
                country="AE",
            )

    def test_a_manager_cannot_be_removed_by_an_update(self, db, new_property, pm):
        prop = new_property()
        with pytest.raises(ValidationError):
            services.update_property(prop, actor=pm, managed_by=None)


# --- geography ----------------------------------------------------------------------------


class TestGeoPoint:
    def test_the_geography_column_follows_the_decimal_pair(self, db, new_property):
        """Both are stored on purpose — forms carry the decimals, queries use the geography —
        which means one of them can drift. A property findable by address but invisible on the
        map is the failure that produces."""
        prop = new_property(latitude=25.0800, longitude=55.1400)
        prop.refresh_from_db()
        assert prop.geo_point is not None
        assert round(prop.geo_point.x, 4) == 55.14
        assert round(prop.geo_point.y, 4) == 25.08

    def test_clearing_the_coordinates_clears_the_geography(self, db, new_property, pm):
        prop = new_property(latitude=25.08, longitude=55.14)
        services.update_property(prop, actor=pm, latitude=None, longitude=None)
        prop.refresh_from_db()
        assert prop.geo_point is None


# --- search (SRS 3.3.7, 5.1) ---------------------------------------------------------------


class TestSearch:
    """The radius half is PostGIS, not arithmetic. The previous implementation computed a
    haversine distance in Python over every row, which cannot use an index and reads the whole
    table to answer "within 5km"."""

    @pytest.fixture
    def scattered(self, db, new_property):
        # Dubai Marina, then ~4km and ~40km away. Distinct addresses as well as titles: the
        # shared fixture address would otherwise make every text search match everything.
        return {
            "near": new_property(
                title="Marina Tower", address_line_1="1 Dock Road",
                latitude=25.0800, longitude=55.1400,
            ),
            "mid": new_property(
                title="JBR Walk", address_line_1="2 Beach Road",
                latitude=25.0760, longitude=55.1740,
            ),
            "far": new_property(
                title="Deira Souk", address_line_1="3 Creek Street",
                latitude=25.2700, longitude=55.3100,
            ),
            "nowhere": new_property(title="Unplaced Plot", address_line_1="4 Elsewhere"),
        }

    def test_a_radius_includes_the_near_and_excludes_the_far(self, scattered, owner):
        results = selectors.search_properties(
            selectors.visible_properties(owner),
            latitude=25.0800,
            longitude=55.1400,
            radius_km=5,
        )
        ids = {p.id for p in results}
        assert scattered["near"].id in ids
        assert scattered["mid"].id in ids
        assert scattered["far"].id not in ids

    def test_a_property_with_no_coordinates_never_matches_a_radius(self, scattered, owner):
        results = selectors.search_properties(
            selectors.visible_properties(owner),
            latitude=25.08, longitude=55.14, radius_km=500,
        )
        assert scattered["nowhere"].id not in {p.id for p in results}

    def test_distance_is_annotated_and_sorted_nearest_first(self, scattered, owner):
        """The old serializer computed a distance and then never exposed it, so a map view had
        no way to sort or label by nearest — the work was done and thrown away."""
        results = list(
            selectors.search_properties(
                selectors.visible_properties(owner), latitude=25.0800, longitude=55.1400,
                radius_km=50,
            )
        )
        assert results[0].id == scattered["near"].id
        assert results[0].distance.km < results[1].distance.km

    def test_a_radius_without_a_centre_is_refused(self, scattered, owner):
        with pytest.raises(ValueError):
            selectors.search_properties(selectors.visible_properties(owner), radius_km=5)

    def test_text_search_matches_in_the_middle(self, scattered, owner):
        results = selectors.search_properties(
            selectors.visible_properties(owner), text="arina"
        )
        assert {p.id for p in results} == {scattered["near"].id}

    def test_amenity_filters_use_containment(self, db, new_property, owner):
        pool = new_property(title="With Pool", amenities={"pool": True, "gym": True})
        new_property(title="No Pool", amenities={"gym": True})
        results = selectors.search_properties(
            selectors.visible_properties(owner), amenities=["pool"]
        )
        assert {p.id for p in results} == {pool.id}

    def test_a_price_filter_reaches_through_to_listings(self, db, new_property, owner, pm):
        cheap, dear = new_property(title="Cheap"), new_property(title="Dear")
        for prop, price in ((cheap, 500_000), (dear, 5_000_000)):
            services.create_listing(
                actor=pm, property=prop, listing_type=Listing.ListingType.SALE,
                title=prop.title, asking_price=price,
            )
        results = selectors.search_properties(
            selectors.visible_properties(owner), max_price=1_000_000
        )
        assert {p.id for p in results} == {cheap.id}

    def test_a_property_with_two_matching_listings_appears_once(
        self, db, new_property, owner, pm
    ):
        """The listing join is to-many. apply_scope deliberately issues no DISTINCT, so this
        filter owns the one it needs — otherwise the property is paginated twice."""
        prop = new_property(title="Twice Listed")
        for n in range(2):
            services.create_listing(
                actor=pm, property=prop, listing_type=Listing.ListingType.SALE,
                title=f"L{n}", asking_price=500_000,
            )
        results = list(
            selectors.search_properties(
                selectors.visible_properties(owner), max_price=1_000_000
            )
        )
        assert [p.id for p in results] == [prop.id]

    def test_search_cannot_widen_what_the_caller_may_see(self, db, new_property, agent_user):
        """The search runs over the scoped queryset, never over the table."""
        new_property(title="Marina Tower", latitude=25.08, longitude=55.14)
        results = selectors.search_properties(
            selectors.visible_properties(agent_user), text="Marina"
        )
        assert list(results) == []


# --- listings (SRS 3.3.1, 3.3.5) -----------------------------------------------------------


class TestListings:
    def test_a_co_listing_agent_must_be_a_second_agent(self, db, new_property, pm, agent_user):
        """The CHECK constraint holds the same rule, but an IntegrityError surfaces as a 500
        with no field name; this is a 400 that points at the field to fix."""
        with pytest.raises(ValidationError, match="second agent"):
            services.create_listing(
                actor=pm,
                property=new_property(),
                listing_type=Listing.ListingType.SALE,
                title="Self co-listed",
                assigned_agent=agent_user,
                co_listing_agent=agent_user,
            )

    def test_a_co_listed_mandate_needs_a_listing_agent(self, db, new_property, pm, agent_user):
        with pytest.raises(ValidationError, match="listing agent of record"):
            services.create_listing(
                actor=pm,
                property=new_property(),
                listing_type=Listing.ListingType.SALE,
                title="Co-lister only",
                co_listing_agent=agent_user,
            )

    def test_a_unit_from_another_property_is_refused(self, db, new_property, pm):
        """Silently wrong in every report downstream — the unit's rent under the wrong
        building's inventory."""
        a, b = new_property(), new_property()
        unit = services.create_unit(actor=pm, property=b, unit_number="201")
        with pytest.raises(ValidationError, match="different property"):
            services.create_listing(
                actor=pm, property=a, unit=unit,
                listing_type=Listing.ListingType.RENT, title="Mismatched",
            )

    def test_publishing_stamps_published_at(self, db, new_property, pm):
        listing = services.create_listing(
            actor=pm, property=new_property(),
            listing_type=Listing.ListingType.SALE, title="Draft first",
        )
        assert listing.published_at is None
        services.change_listing_status(listing, Listing.Status.ACTIVE, actor=pm)
        listing.refresh_from_db()
        assert listing.published_at is not None

    def test_republishing_does_not_reset_published_at(self, db, new_property, pm):
        listing = services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="On and off", status=Listing.Status.ACTIVE,
        )
        first = listing.published_at
        services.change_listing_status(listing, Listing.Status.OFF_MARKET, actor=pm)
        services.change_listing_status(listing, Listing.Status.ACTIVE, actor=pm)
        listing.refresh_from_db()
        assert listing.published_at == first

    def test_sold_is_terminal_for_a_listing(self, db, new_property, pm):
        listing = services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="Done", status=Listing.Status.ACTIVE,
        )
        services.change_listing_status(listing, Listing.Status.SOLD, actor=pm)
        with pytest.raises(ValidationError, match="terminal"):
            services.change_listing_status(listing, Listing.Status.ACTIVE, actor=pm)

    def test_a_property_with_a_live_listing_cannot_be_archived(self, db, new_property, pm):
        prop = new_property()
        services.create_listing(
            actor=pm, property=prop, listing_type=Listing.ListingType.SALE,
            title="Live", status=Listing.Status.ACTIVE,
        )
        with pytest.raises(ValidationError, match="Withdraw the live listings"):
            services.delete_property(prop, actor=pm)


class TestOwners:
    def test_mandate_terms_are_stored_on_the_owner_record(self, db, new_property, pm, make_contact):
        """SRS 3.3.9 — the terms belong to the ownership agreement, so they survive a listing
        being withdrawn and relisted."""
        from apps.inventory.models import PropertyOwner

        prop = new_property()
        services.set_owners(
            prop,
            [
                {
                    "contact": make_contact(),
                    "ownership_percentage": 100,
                    "is_primary_owner": True,
                    "start_date": "2026-01-01",
                    "commission_rate": 2.5,
                    "mandate_type": PropertyOwner.MandateType.EXCLUSIVE,
                    "mandate_expires_at": "2026-12-31",
                }
            ],
            actor=pm,
        )
        owner = prop.owners.get()
        assert owner.mandate_type == "EXCLUSIVE"
        assert float(owner.commission_rate) == 2.5

    def test_only_one_owner_can_be_primary(self, db, new_property, pm, make_contact):
        with pytest.raises(ValidationError, match="primary owner"):
            services.set_owners(
                new_property(),
                [
                    {"contact": make_contact(), "ownership_percentage": 50,
                     "is_primary_owner": True, "start_date": "2026-01-01"},
                    {"contact": make_contact(), "ownership_percentage": 50,
                     "is_primary_owner": True, "start_date": "2026-01-01"},
                ],
                actor=pm,
            )

    def test_a_zero_share_is_refused(self, db, new_property, pm, make_contact):
        with pytest.raises(ValidationError):
            services.set_owners(
                new_property(),
                [{"contact": make_contact(), "ownership_percentage": 0,
                  "start_date": "2026-01-01"}],
                actor=pm,
            )


# --- media (SRS 3.3.3) ----------------------------------------------------------------------


class TestMedia:
    @pytest.fixture
    def listing(self, db, new_property, pm):
        return services.create_listing(
            actor=pm, property=new_property(),
            listing_type=Listing.ListingType.SALE, title="Gallery test",
        )

    def test_the_first_image_becomes_the_cover(self, listing, pm):
        """A gallery with no primary renders a blank card in every list that shows one."""
        media = services.add_media(
            actor=pm, listing=listing, media_type=Media.MediaType.PHOTO,
            storage_key="a.jpg",
        )
        assert media.is_primary is True

    def test_later_images_do_not_steal_the_cover(self, listing, pm):
        first = services.add_media(
            actor=pm, listing=listing, media_type=Media.MediaType.PHOTO, storage_key="a.jpg"
        )
        second = services.add_media(
            actor=pm, listing=listing, media_type=Media.MediaType.PHOTO, storage_key="b.jpg"
        )
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.is_primary and not second.is_primary

    def test_setting_a_new_cover_demotes_the_old_one(self, listing, pm):
        first = services.add_media(
            actor=pm, listing=listing, media_type=Media.MediaType.PHOTO, storage_key="a.jpg"
        )
        second = services.add_media(
            actor=pm, listing=listing, media_type=Media.MediaType.PHOTO, storage_key="b.jpg"
        )
        services.set_primary_media(second, actor=pm)
        first.refresh_from_db()
        second.refresh_from_db()
        assert second.is_primary and not first.is_primary

    def test_media_is_appended_in_order(self, listing, pm):
        """Defaulting every row to sort_order 0 leaves the gallery's order to whatever the
        database happens to return."""
        keys = ["a.jpg", "b.jpg", "c.jpg"]
        for key in keys:
            services.add_media(
                actor=pm, listing=listing, media_type=Media.MediaType.PHOTO, storage_key=key
            )
        assert [m.sort_order for m in selectors.gallery(listing)] == [0, 1, 2]

    def test_reordering_applies_the_given_sequence(self, listing, pm):
        rows = [
            services.add_media(
                actor=pm, listing=listing, media_type=Media.MediaType.PHOTO,
                storage_key=f"{n}.jpg",
            )
            for n in range(3)
        ]
        services.reorder_media(
            {"listing": listing}, [rows[2].id, rows[0].id, rows[1].id], actor=pm
        )
        assert [m.storage_key for m in selectors.gallery(listing).order_by("sort_order")] == [
            "2.jpg", "0.jpg", "1.jpg"
        ]

    def test_reordering_with_a_foreign_id_is_refused(self, listing, pm, new_property):
        other = services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="Other",
        )
        theirs = services.add_media(
            actor=pm, listing=other, media_type=Media.MediaType.PHOTO, storage_key="x.jpg"
        )
        with pytest.raises(ValidationError, match="Not media of this item"):
            services.reorder_media({"listing": listing}, [theirs.id], actor=pm)

    def test_deleting_the_cover_promotes_the_next(self, listing, pm):
        first = services.add_media(
            actor=pm, listing=listing, media_type=Media.MediaType.PHOTO, storage_key="a.jpg"
        )
        second = services.add_media(
            actor=pm, listing=listing, media_type=Media.MediaType.PHOTO, storage_key="b.jpg"
        )
        services.delete_media(first, actor=pm)
        second.refresh_from_db()
        assert second.is_primary is True

    def test_media_needs_exactly_one_parent(self, listing, pm, new_property):
        """The CHECK constraint demands at least one; two parents puts the row twice in one
        carousel and makes it ambiguous to re-order."""
        with pytest.raises(ValidationError, match="exactly one"):
            services.add_media(
                actor=pm, listing=listing, property=listing.property,
                media_type=Media.MediaType.PHOTO, storage_key="a.jpg",
            )
        with pytest.raises(ValidationError, match="exactly one"):
            services.add_media(
                actor=pm, media_type=Media.MediaType.PHOTO, storage_key="a.jpg"
            )

    def test_media_needs_a_blob_reference(self, listing, pm):
        with pytest.raises(ValidationError):
            services.add_media(actor=pm, listing=listing, media_type=Media.MediaType.PHOTO)


# --- the endpoints ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPropertyApi:
    URL = "/api/v1/properties/"

    def test_a_property_manager_creates_and_sees_their_own(self, auth_client, pm, property_type):
        response = auth_client(pm).post(
            self.URL,
            {
                "property_type": str(property_type.id),
                "managed_by": str(pm.id),
                "title": "Marina Villa",
                "address_line_1": "12 Marina Walk",
                "city": "Dubai",
                "country": "AE",
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        assert response.data["status"] == "DRAFT"

        listed = auth_client(pm).get(self.URL)
        assert listed.data["count"] == 1

    def test_creating_without_a_manager_is_a_400(self, auth_client, pm, property_type):
        response = auth_client(pm).post(
            self.URL,
            {
                "property_type": str(property_type.id),
                "title": "Orphan",
                "address_line_1": "1 Nowhere",
                "city": "Dubai",
                "country": "AE",
            },
            format="json",
        )
        assert response.status_code == 400

    def test_another_managers_property_is_invisible(self, auth_client, new_property, make_user):
        new_property()
        other_pm = make_user("property_manager")
        assert auth_client(other_pm).get(self.URL).data["count"] == 0

    def test_status_moves_through_its_own_endpoint(self, auth_client, new_property, pm):
        prop = new_property()
        response = auth_client(pm).post(
            f"{self.URL}{prop.id}/status/",
            {"status": "AVAILABLE", "reason": "Ready to market."},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["status"] == "AVAILABLE"

    def test_an_illegal_status_move_is_a_400_with_the_allowed_set(
        self, auth_client, new_property, pm
    ):
        prop = new_property()
        response = auth_client(pm).post(
            f"{self.URL}{prop.id}/status/", {"status": "SOLD"}, format="json"
        )
        assert response.status_code == 400
        assert "AVAILABLE" in str(response.data)

    def test_status_history_is_readable(self, auth_client, new_property, pm):
        prop = new_property()
        services.change_status(prop, Property.Status.AVAILABLE, actor=pm, reason="Ready.")
        response = auth_client(pm).get(f"{self.URL}{prop.id}/status-history/")
        assert [row["to_status"] for row in response.data] == ["AVAILABLE", "DRAFT"]

    def test_the_radius_search_runs_over_the_scoped_set(self, auth_client, new_property, pm):
        new_property(title="Marina Tower", latitude=25.0800, longitude=55.1400)
        new_property(title="Deira Souk", latitude=25.2700, longitude=55.3100)
        response = auth_client(pm).get(
            f"{self.URL}search/?latitude=25.08&longitude=55.14&radius_km=5"
        )
        assert response.status_code == 200, response.data
        assert response.data["count"] == 1
        assert response.data["results"][0]["distance_km"] is not None

    def test_a_radius_without_a_centre_is_a_400(self, auth_client, pm):
        assert auth_client(pm).get(f"{self.URL}search/?radius_km=5").status_code == 400

    def test_units_are_added_through_the_property(self, auth_client, new_property, pm):
        prop = new_property()
        response = auth_client(pm).post(
            f"{self.URL}{prop.id}/units/", {"unit_number": "101", "bedrooms": 2}, format="json"
        )
        assert response.status_code == 201, response.data
        assert response.data["status"] == "AVAILABLE"


@pytest.mark.django_db
class TestListingApi:
    URL = "/api/v1/listings/"

    def test_an_agent_sees_the_listing_they_carry(self, auth_client, new_property, pm, agent_user):
        mine = services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="Mine", assigned_agent=agent_user,
        )
        services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="Theirs",
        )
        response = auth_client(agent_user).get(self.URL)
        assert [r["id"] for r in response.data["results"]] == [str(mine.id)]

    def test_a_co_listing_agent_sees_it_too(self, auth_client, new_property, pm, agent_user, make_user):
        """SRS 3.3.5. A second agent on the mandate who cannot see the mandate is not on it."""
        co = make_user("agent")
        listing = services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="Shared", assigned_agent=agent_user, co_listing_agent=co,
        )
        response = auth_client(co).get(self.URL)
        assert [r["id"] for r in response.data["results"]] == [str(listing.id)]

    def test_the_reference_code_is_generated_not_supplied(self, auth_client, new_property, pm):
        response = auth_client(pm).post(
            self.URL,
            {
                "property": str(new_property().id),
                "listing_type": "SALE",
                "title": "Auto reference",
                "reference_code": "MINE-0001",
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        assert response.data["reference_code"].startswith("LST-")

    def test_media_posts_as_json(self, auth_client, new_property, pm):
        """The old implementation called `request.data.dict()`, which raises on any JSON
        body — so the endpoint worked only from a form."""
        listing = services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="Gallery",
        )
        response = auth_client(pm).post(
            f"{self.URL}{listing.id}/media/",
            {"media_type": "PHOTO", "storage_key": "listings/a.jpg", "caption": "Front"},
            format="json",
        )
        assert response.status_code == 201, response.data
        assert response.data["is_primary"] is True

    def test_the_gallery_reorders(self, auth_client, new_property, pm):
        listing = services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="Gallery",
        )
        rows = [
            services.add_media(
                actor=pm, listing=listing, media_type=Media.MediaType.PHOTO,
                storage_key=f"{n}.jpg",
            )
            for n in range(3)
        ]
        response = auth_client(pm).post(
            f"{self.URL}{listing.id}/media/reorder/",
            {"order": [str(rows[2].id), str(rows[0].id), str(rows[1].id)]},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert [m["storage_key"] for m in response.data][0] in ("2.jpg", "0.jpg")


class TestChildrenOfAnArchivedProperty:
    """`_default_manager` on a SoftDeleteModel is unfiltered, so a nested scope rule matched
    children whose parent had been archived — a visible gallery for an invisible property."""

    def test_media_disappears_with_its_property(self, db, new_property, pm):
        from apps.identity.selectors import apply_scope

        prop = new_property()
        services.add_media(
            actor=pm, property=prop, media_type=Media.MediaType.PHOTO, storage_key="a.jpg"
        )
        assert selectors.visible_media(pm).count() == 1
        services.delete_property(prop, actor=pm)
        assert selectors.visible_media(pm).count() == 0
        assert apply_scope(Media.objects.all(), pm, "media").count() == 0

    def test_units_disappear_with_their_property(self, db, new_property, pm):
        from apps.identity.selectors import apply_scope

        prop = new_property()
        services.create_unit(actor=pm, property=prop, unit_number="101")
        assert apply_scope(Unit.objects.all(), pm, "unit").count() == 1
        services.delete_property(prop, actor=pm)
        assert apply_scope(Unit.objects.all(), pm, "unit").count() == 0

    def test_an_agency_wide_scope_does_not_see_them_either(self, db, new_property, pm, owner):
        from apps.identity.selectors import apply_scope

        prop = new_property()
        services.add_media(
            actor=pm, property=prop, media_type=Media.MediaType.PHOTO, storage_key="a.jpg"
        )
        services.delete_property(prop, actor=pm)
        assert apply_scope(Media.objects.all(), owner, "media").count() == 0


class TestGalleryOrderingIsTotal:
    def test_a_partial_reorder_leaves_no_two_rows_on_one_position(
        self, db, new_property, pm
    ):
        """Renumbering only the listed rows left the others where they were — reordering two
        of three images produced two rows both claiming position 0, and a gallery whose order
        then depended on whatever the database happened to return."""
        listing = services.create_listing(
            actor=pm, property=new_property(), listing_type=Listing.ListingType.SALE,
            title="Gallery",
        )
        rows = [
            services.add_media(
                actor=pm, listing=listing, media_type=Media.MediaType.PHOTO,
                storage_key=f"{n}.jpg",
            )
            for n in range(3)
        ]
        services.reorder_media({"listing": listing}, [rows[2].id], actor=pm)
        positions = list(
            Media.objects.filter(listing=listing).values_list("sort_order", flat=True)
        )
        assert sorted(positions) == [0, 1, 2]
        assert selectors.gallery(listing).order_by("sort_order").first().storage_key == "2.jpg"


class TestCoverImage:
    def test_a_soft_deleted_row_is_never_served_as_the_cover(self, db, new_property, pm):
        """The serializer is used on a bare instance too — from another view, a management
        command, a test — where the viewset's filtered prefetch is not there to save it."""
        from apps.inventory.api.serializers import PropertySerializer

        prop = new_property()
        media = services.add_media(
            actor=pm, property=prop, media_type=Media.MediaType.PHOTO, storage_key="a.jpg"
        )
        assert PropertySerializer(prop).data["primary_media"] is not None
        Media.objects.filter(pk=media.pk).update(deleted_at="2026-01-01T00:00:00Z")
        prop.refresh_from_db()
        assert PropertySerializer(prop).data["primary_media"] is None
