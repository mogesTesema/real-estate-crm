"""Listing status state machine (SRS §3.3.4)."""
import pytest

from apps.core.tenancy import tenant_context
from apps.properties.models import Listing, ListingStatus, Property
from apps.properties.services import IllegalTransition, change_listing_status

pytestmark = pytest.mark.django_db


def _listing(tenant):
    prop = Property.objects.create(
        tenant=tenant, category=Property.Category.RESIDENTIAL, title="2BR"
    )
    return Listing.objects.create(
        tenant=tenant,
        property=prop,
        listing_type=Listing.ListingType.SALE,
        price=100000,
    )


def test_legal_transition_writes_history(tenant):
    with tenant_context(tenant.id):
        listing = _listing(tenant)
        change_listing_status(listing=listing, to_status=ListingStatus.ACTIVE)
        listing.refresh_from_db()
        assert listing.status == ListingStatus.ACTIVE
        assert listing.status_history.count() == 1
        h = listing.status_history.first()
        assert h.from_status == ListingStatus.DRAFT
        assert h.to_status == ListingStatus.ACTIVE


def test_illegal_transition_raises(tenant):
    with tenant_context(tenant.id):
        listing = _listing(tenant)  # DRAFT
        with pytest.raises(IllegalTransition):
            change_listing_status(listing=listing, to_status=ListingStatus.SOLD)


def test_terminal_status_is_final(tenant):
    with tenant_context(tenant.id):
        listing = _listing(tenant)
        change_listing_status(listing=listing, to_status=ListingStatus.ACTIVE)
        change_listing_status(listing=listing, to_status=ListingStatus.UNDER_OFFER)
        change_listing_status(listing=listing, to_status=ListingStatus.SOLD)
        with pytest.raises(IllegalTransition):
            change_listing_status(listing=listing, to_status=ListingStatus.ACTIVE)
