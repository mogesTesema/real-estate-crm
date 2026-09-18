"""Public write API for `inventory` (architecture.md §1.2).

Projects, buildings, properties, units, listings, media.

This is the ONLY module another app may import to mutate `inventory`-owned rows. Calling
`Model.objects.create/update/delete` on an `inventory` model from another app is a forbidden
pattern, as is reacting to `post_save` signals to do it.

The status state machine (SRS 3.3.4) lives here rather than on the model, because a legal
transition is a business rule that also has to write a history row and do both atomically —
which a `save()` override cannot promise a caller that forgets to pass the actor.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.services import next_reference

from . import signals
from .models import (
    Building,
    Listing,
    Media,
    Project,
    Property,
    PropertyOwner,
    PropertyStatusHistory,
    Unit,
)

logger = logging.getLogger(__name__)


# --- The availability state machine (SRS 3.3.4) -------------------------------------------
#
# "The System shall track property availability status (Available, Reserved, Under Offer,
# Sold, Rented, Off-Market) with automatic status history."
#
# Expressed as an explicit table rather than as a chain of ifs. A missing key is a terminal
# state; an absent target is an illegal move. Both read off the page, which is what makes a
# reviewer able to check the rules against the requirement.

PROPERTY_TRANSITIONS = {
    Property.Status.DRAFT: {Property.Status.AVAILABLE, Property.Status.ARCHIVED},
    Property.Status.AVAILABLE: {
        Property.Status.OCCUPIED,
        Property.Status.UNDER_MAINTENANCE,
        Property.Status.SOLD,
        Property.Status.ARCHIVED,
    },
    Property.Status.OCCUPIED: {
        Property.Status.AVAILABLE,
        Property.Status.UNDER_MAINTENANCE,
        Property.Status.SOLD,
    },
    Property.Status.UNDER_MAINTENANCE: {
        Property.Status.AVAILABLE,
        Property.Status.OCCUPIED,
        Property.Status.ARCHIVED,
    },
    # SOLD is terminal. A sold property that comes back to market is a new mandate, not an
    # edit of the old one — reopening it in place would silently rewrite the history that
    # the commission and the transaction both hang off.
    Property.Status.SOLD: set(),
    Property.Status.ARCHIVED: {Property.Status.AVAILABLE},
}

UNIT_TRANSITIONS = {
    Unit.Status.AVAILABLE: {
        Unit.Status.RESERVED,
        Unit.Status.OCCUPIED,
        Unit.Status.SOLD,
        Unit.Status.UNDER_MAINTENANCE,
        Unit.Status.OFF_MARKET,
    },
    Unit.Status.RESERVED: {
        Unit.Status.AVAILABLE,
        Unit.Status.OCCUPIED,
        Unit.Status.SOLD,
        Unit.Status.OFF_MARKET,
    },
    Unit.Status.OCCUPIED: {
        Unit.Status.AVAILABLE,
        Unit.Status.UNDER_MAINTENANCE,
        Unit.Status.SOLD,
    },
    Unit.Status.UNDER_MAINTENANCE: {Unit.Status.AVAILABLE, Unit.Status.OFF_MARKET},
    Unit.Status.OFF_MARKET: {Unit.Status.AVAILABLE},
    Unit.Status.SOLD: set(),
}


@transaction.atomic
def change_status(target, new_status, *, actor, reason=None):
    """Move a property or a unit to `new_status`, recording why (SRS 3.3.4).

    Self-transition is a no-op that writes no history: re-saving a form without touching the
    dropdown should not manufacture an audit row that says nothing happened.
    """
    is_property = isinstance(target, Property)
    table = PROPERTY_TRANSITIONS if is_property else UNIT_TRANSITIONS
    if new_status not in table:
        raise ValidationError({"status": f"Unknown status {new_status!r}."})

    current = target.status
    if current == new_status:
        return target

    allowed = table.get(current, set())
    if new_status not in allowed:
        raise ValidationError(
            {
                "status": (
                    f"Cannot move from {current} to {new_status}."
                    + (
                        f" Allowed: {sorted(allowed)}."
                        if allowed
                        else f" {current} is a terminal status."
                    )
                )
            }
        )

    target.status = new_status
    target.updated_by = actor
    target.save(update_fields=["status", "updated_by", "updated_at"])
    PropertyStatusHistory.objects.create(
        property=target if is_property else None,
        unit=None if is_property else target,
        from_status=current,
        to_status=new_status,
        changed_by=actor,
        reason=reason,
    )
    signals.status_changed.send_robust(
        sender=None,
        target=target,
        from_status=current,
        to_status=new_status,
        actor=actor,
        reason=reason,
    )
    return target


# --- Properties ----------------------------------------------------------------------------

PROPERTY_WRITABLE = frozenset(
    {
        "property_type",
        "project",
        "building",
        "is_multi_unit",
        "title",
        "description",
        "address_line_1",
        "address_line_2",
        "city",
        "state",
        "country",
        "postal_code",
        "latitude",
        "longitude",
        "land_area",
        "built_area",
        "bedrooms",
        "bathrooms",
        "parking_spaces",
        "year_built",
        "floor_number",
        "total_floors",
        "amenities",
        "custom_data",
        "managed_by",
    }
)


def _reject_unknown(fields, allowed, what):
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationError(
            {name: f"This field cannot be set through the {what} service." for name in unknown}
        )


def _sync_geo_point(instance, fields):
    """Keep `geo_point` in step with the decimal pair (§8).

    Both are stored on purpose — forms and imports carry the decimals, queries use the
    geography — which means one of them can drift. Deriving the geography from the decimals on
    every write is what stops a property being findable by address but invisible on the map.
    """
    from django.contrib.gis.geos import Point

    if "latitude" not in fields and "longitude" not in fields:
        return
    lat = instance.latitude
    lon = instance.longitude
    instance.geo_point = (
        Point(float(lon), float(lat), srid=4326) if lat is not None and lon is not None else None
    )


@transaction.atomic
def create_property(*, actor, status=None, **fields):
    """Create a property. `managed_by` is mandatory — SRS 3.3.10, and the anchor the
    MANAGED_PROPERTIES data scope resolves through."""
    _reject_unknown(fields, PROPERTY_WRITABLE, "property")
    if not fields.get("managed_by"):
        raise ValidationError(
            {"managed_by": "Every property needs a Property Manager (SRS 3.3.10)."}
        )
    prop = Property(
        status=status or Property.Status.DRAFT, created_by=actor, updated_by=actor, **fields
    )
    _sync_geo_point(prop, fields)
    prop.save()
    PropertyStatusHistory.objects.create(
        property=prop, from_status=None, to_status=prop.status, changed_by=actor,
        reason="Created.",
    )
    return prop


@transaction.atomic
def update_property(prop, *, actor, **fields):
    """Patch a property. `status` is not settable here — it goes through `change_status`,
    which is what guarantees the history row and the legality check happen together."""
    _reject_unknown(fields, PROPERTY_WRITABLE, "property")
    if "managed_by" in fields and not fields["managed_by"]:
        raise ValidationError(
            {"managed_by": "A property cannot be left without a Property Manager."}
        )
    for name, value in fields.items():
        setattr(prop, name, value)
    _sync_geo_point(prop, fields)
    prop.updated_by = actor
    prop.save()
    return prop


@transaction.atomic
def delete_property(prop, *, actor):
    if prop.deleted_at:
        return prop
    if prop.listings.filter(
        deleted_at__isnull=True, status__in=Listing.LIVE_STATUSES
    ).exists():
        raise ValidationError(
            "Withdraw the live listings on this property before archiving it."
        )
    prop.deleted_at = timezone.now()
    prop.updated_by = actor
    prop.save(update_fields=["deleted_at", "updated_by", "updated_at"])
    return prop


# --- Units, projects, buildings ------------------------------------------------------------

UNIT_WRITABLE = frozenset(
    {
        "property",
        "building",
        "unit_number",
        "floor_number",
        "unit_type",
        "bedrooms",
        "bathrooms",
        "built_area",
        "parking_spaces",
        "sale_price",
        "rent_amount",
        "amenities",
        "custom_data",
    }
)


@transaction.atomic
def create_unit(*, actor, status=None, **fields):
    """Add an addressable unit to a property (SRS 3.3.6)."""
    _reject_unknown(fields, UNIT_WRITABLE, "unit")
    unit = Unit(
        status=status or Unit.Status.AVAILABLE, created_by=actor, updated_by=actor, **fields
    )
    unit.save()
    PropertyStatusHistory.objects.create(
        unit=unit, from_status=None, to_status=unit.status, changed_by=actor, reason="Created."
    )
    if not unit.property.is_multi_unit:
        # A property with units is multi-unit by definition; leaving the flag off makes the
        # unit invisible to anything that branches on it.
        unit.property.is_multi_unit = True
        unit.property.save(update_fields=["is_multi_unit", "updated_at"])
    return unit


@transaction.atomic
def update_unit(unit, *, actor, **fields):
    _reject_unknown(fields, UNIT_WRITABLE, "unit")
    for name, value in fields.items():
        setattr(unit, name, value)
    unit.updated_by = actor
    unit.save()
    return unit


def create_project(*, actor, **fields):
    return Project.objects.create(created_by=actor, updated_by=actor, **fields)


def create_building(*, actor, **fields):
    return Building.objects.create(created_by=actor, updated_by=actor, **fields)


# --- Listings (SRS 3.3.1, 3.3.5, 3.3.9) ---------------------------------------------------

LISTING_WRITABLE = frozenset(
    {
        "property",
        "unit",
        "listing_type",
        "title",
        "description",
        "asking_price",
        "rent_amount",
        "security_deposit",
        "commission_rate",
        "available_from",
        "expires_at",
        "is_exclusive",
        "assigned_agent",
        "co_listing_agent",
        "custom_data",
    }
)

LISTING_TRANSITIONS = {
    Listing.Status.DRAFT: {Listing.Status.ACTIVE, Listing.Status.OFF_MARKET},
    Listing.Status.ACTIVE: {
        Listing.Status.UNDER_OFFER,
        Listing.Status.RESERVED,
        Listing.Status.SOLD,
        Listing.Status.RENTED,
        Listing.Status.OFF_MARKET,
        Listing.Status.EXPIRED,
    },
    Listing.Status.UNDER_OFFER: {
        Listing.Status.ACTIVE,
        Listing.Status.RESERVED,
        Listing.Status.SOLD,
        Listing.Status.RENTED,
        Listing.Status.OFF_MARKET,
    },
    Listing.Status.RESERVED: {
        Listing.Status.ACTIVE,
        Listing.Status.SOLD,
        Listing.Status.RENTED,
        Listing.Status.OFF_MARKET,
    },
    # Terminal: the mandate is discharged. Relisting is a new listing with a new reference.
    Listing.Status.SOLD: set(),
    Listing.Status.RENTED: set(),
    Listing.Status.OFF_MARKET: {Listing.Status.ACTIVE},
    Listing.Status.EXPIRED: {Listing.Status.ACTIVE},
}


@transaction.atomic
def create_listing(*, actor, status=None, reference_code=None, **fields):
    """Put a property (or one of its units) on the market.

    The reference code comes from `core.services.next_reference`, which takes a row lock —
    §2 forbids MAX()+1, and not theoretically: two agents publishing in the same second would
    both read the same maximum and the loser would hit the partial unique index.
    """
    _reject_unknown(fields, LISTING_WRITABLE, "listing")
    _validate_agents(fields.get("assigned_agent"), fields.get("co_listing_agent"))
    _validate_unit_belongs(fields.get("property"), fields.get("unit"))

    listing = Listing(
        reference_code=reference_code or next_reference("listing", prefix="LST"),
        status=status or Listing.Status.DRAFT,
        created_by=actor,
        updated_by=actor,
        **fields,
    )
    if listing.status == Listing.Status.ACTIVE:
        listing.published_at = timezone.now()
    listing.save()
    return listing


@transaction.atomic
def update_listing(listing, *, actor, **fields):
    _reject_unknown(fields, LISTING_WRITABLE, "listing")
    _validate_agents(
        fields.get("assigned_agent", listing.assigned_agent),
        fields.get("co_listing_agent", listing.co_listing_agent),
    )
    _validate_unit_belongs(
        fields.get("property", listing.property), fields.get("unit", listing.unit)
    )
    for name, value in fields.items():
        setattr(listing, name, value)
    listing.updated_by = actor
    listing.save()
    return listing


def _validate_agents(assigned, co_listing):
    """SRS 3.3.5's co-listing, checked before the database does.

    The CHECK constraint holds the same rule, but an IntegrityError surfaces as a 500 with no
    field name; this produces a 400 that points at the field the user has to fix.
    """
    if co_listing and assigned and co_listing.pk == assigned.pk:
        raise ValidationError(
            {"co_listing_agent": "The co-listing agent must be a second agent."}
        )
    if co_listing and not assigned:
        raise ValidationError(
            {"assigned_agent": "A co-listed mandate needs a listing agent of record."}
        )


def _validate_unit_belongs(prop, unit):
    """A listing that names a unit of a different property is silently wrong in every report
    downstream — the unit's rent under the wrong building's inventory."""
    if unit and prop and unit.property_id != prop.pk:
        raise ValidationError({"unit": "That unit belongs to a different property."})


@transaction.atomic
def change_listing_status(listing, new_status, *, actor, reason=None):
    """Move a listing through its lifecycle (SRS 3.3.1, 3.3.4)."""
    if new_status not in LISTING_TRANSITIONS:
        raise ValidationError({"status": f"Unknown listing status {new_status!r}."})
    current = listing.status
    if current == new_status:
        return listing
    allowed = LISTING_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValidationError(
            {
                "status": (
                    f"Cannot move a listing from {current} to {new_status}."
                    + (
                        f" Allowed: {sorted(allowed)}."
                        if allowed
                        else f" {current} is a terminal status."
                    )
                )
            }
        )

    fields = ["status", "updated_by", "updated_at"]
    listing.status = new_status
    if new_status == Listing.Status.ACTIVE and listing.published_at is None:
        listing.published_at = timezone.now()
        fields.append("published_at")
    listing.updated_by = actor
    listing.save(update_fields=fields)

    signals.listing_status_changed.send_robust(
        sender=None, listing=listing, actor=actor, from_status=current, to_status=new_status
    )
    return listing


@transaction.atomic
def set_owners(prop, owners, *, actor):
    """Replace a property's owner records, with their mandate terms (SRS 3.3.9).

    `owners` is a list of dicts: contact, ownership_percentage, is_primary_owner, start_date,
    end_date, commission_rate, mandate_type, mandate_expires_at.
    """
    total_primary = sum(1 for row in owners if row.get("is_primary_owner"))
    if total_primary > 1:
        raise ValidationError({"owners": "Only one owner can be the primary owner."})
    for row in owners:
        pct = row.get("ownership_percentage")
        if pct is None or not (0 < float(pct) <= 100):
            raise ValidationError(
                {"ownership_percentage": "Must be greater than 0 and at most 100."}
            )
    prop.owners.all().delete()
    return PropertyOwner.objects.bulk_create(
        [PropertyOwner(property=prop, **row) for row in owners]
    )


# --- Media (SRS 3.3.3) ---------------------------------------------------------------------


@transaction.atomic
def add_media(*, actor, is_primary=False, **fields):
    """Attach a photo, floor plan, video, brochure or virtual tour to the gallery.

    Exactly one target — property, unit or listing — even though the CHECK constraint only
    demands at least one: a media row on two parents appears twice in one carousel and is
    ambiguous to re-order.
    """
    targets = [key for key in ("property", "unit", "listing") if fields.get(key)]
    if len(targets) != 1:
        raise ValidationError(
            {"detail": "Attach media to exactly one of property, unit or listing."}
        )
    if not (fields.get("file") or fields.get("storage_key")):
        raise ValidationError({"storage_key": "Media needs a stored file or a storage key."})

    parent_filter = {targets[0]: fields[targets[0]]}
    if "sort_order" not in fields:
        # Append, rather than defaulting every row to 0 and leaving the gallery's order to
        # whatever the database returns.
        last = (
            Media.objects.filter(deleted_at__isnull=True, **parent_filter)
            .order_by("-sort_order")
            .values_list("sort_order", flat=True)
            .first()
        )
        fields["sort_order"] = 0 if last is None else last + 1

    media = Media.objects.create(created_by=actor, is_primary=False, **fields)
    if is_primary or not Media.objects.filter(
        deleted_at__isnull=True, is_primary=True, **parent_filter
    ).exists():
        # The first image attached becomes the cover by default: a gallery with no primary
        # renders a blank card in every list that shows one.
        set_primary_media(media, actor=actor)
    return media


@transaction.atomic
def set_primary_media(media, *, actor):
    """Make this the cover image. Exactly one primary per parent."""
    parent_filter = _media_parent_filter(media)
    Media.objects.filter(deleted_at__isnull=True, is_primary=True, **parent_filter).exclude(
        pk=media.pk
    ).update(is_primary=False)
    media.is_primary = True
    media.save(update_fields=["is_primary", "updated_at"])
    return media


def _media_parent_filter(media):
    for key in ("property", "unit", "listing"):
        if getattr(media, f"{key}_id"):
            return {f"{key}_id": getattr(media, f"{key}_id")}
    raise ValidationError({"detail": "This media row has no parent."})


@transaction.atomic
def reorder_media(parent_filter, ordered_ids, *, actor):
    """Apply a drag-and-drop ordering. `ordered_ids` is the gallery, front to back.

    A partial list is allowed and the rest keep their relative order *after* it. Renumbering
    only the listed rows left the others on their old positions — reordering two of three
    images produced two rows both claiming position 0, and a gallery whose order then depended
    on whatever the database happened to return.
    """
    rows = {
        str(m.pk): m
        for m in Media.objects.filter(deleted_at__isnull=True, **parent_filter).order_by(
            "sort_order", "created_at"
        )
    }
    unknown = set(map(str, ordered_ids)) - set(rows)
    if unknown:
        raise ValidationError({"order": f"Not media of this item: {sorted(unknown)}"})

    listed = [str(media_id) for media_id in ordered_ids]
    remainder = [key for key in rows if key not in set(listed)]
    for position, key in enumerate([*listed, *remainder]):
        rows[key].sort_order = position
    Media.objects.bulk_update(rows.values(), ["sort_order"])
    return sorted(rows.values(), key=lambda m: m.sort_order)


@transaction.atomic
def delete_media(media, *, actor):
    was_primary = media.is_primary
    media.deleted_at = timezone.now()
    media.is_primary = False
    media.save(update_fields=["deleted_at", "is_primary", "updated_at"])
    if was_primary:
        # Promote the next image rather than leaving the gallery with no cover.
        successor = (
            Media.objects.filter(deleted_at__isnull=True, **_media_parent_filter(media))
            .order_by("sort_order")
            .first()
        )
        if successor:
            set_primary_media(successor, actor=actor)
    return media
