"""
Listing status state machine (SRS §3.3.4, plan §2.6).

Transitions go through here so every change writes a history row — never a naked
field assignment. Illegal transitions raise, keeping the lifecycle honest.
"""
from django.db import transaction

from .models import Listing, ListingStatus, ListingStatusHistory

S = ListingStatus

ALLOWED: dict[str, set[str]] = {
    S.DRAFT: {S.ACTIVE, S.OFF_MARKET},
    S.ACTIVE: {S.UNDER_OFFER, S.RESERVED, S.OFF_MARKET, S.EXPIRED},
    S.UNDER_OFFER: {S.RESERVED, S.SOLD, S.RENTED, S.ACTIVE},
    S.RESERVED: {S.SOLD, S.RENTED, S.ACTIVE},
    S.SOLD: set(),
    S.RENTED: set(),
    S.OFF_MARKET: {S.ACTIVE, S.EXPIRED},
    S.EXPIRED: {S.ACTIVE, S.OFF_MARKET},
}


class IllegalTransition(Exception):
    pass


@transaction.atomic
def change_listing_status(
    *, listing: Listing, to_status: str, user=None, reason: str = ""
) -> Listing:
    current = listing.status
    if to_status == current:
        return listing
    if to_status not in ALLOWED.get(current, set()):
        raise IllegalTransition(
            f"Cannot move listing from '{current}' to '{to_status}'."
        )
    ListingStatusHistory.objects.create(
        tenant_id=listing.tenant_id,
        listing=listing,
        from_status=current,
        to_status=to_status,
        changed_by=user,
        reason=reason,
    )
    listing.status = to_status
    listing.save(update_fields=["status", "updated_at"])
    return listing
