"""Public reads / scoped querysets for `inventory` (architecture.md §1.2).

Other apps read `inventory` rows through this module. They must never import
`apps.inventory.api` — that package is the HTTP surface and is private to this app.
"""
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db.models import Q

from apps.identity.selectors import apply_scope

from .models import Listing, Media, Property, Unit


def live_properties():
    return Property.objects.filter(deleted_at__isnull=True)


def live_listings():
    return Listing.objects.filter(deleted_at__isnull=True)


def live_units():
    return Unit.objects.filter(deleted_at__isnull=True)


def live_media():
    return Media.objects.filter(deleted_at__isnull=True)


def visible_properties(user):
    return apply_scope(live_properties(), user, "property")


def visible_listings(user):
    return apply_scope(live_listings(), user, "listing")


def visible_media(user):
    return apply_scope(live_media(), user, "media")


def search_properties(
    queryset,
    *,
    text=None,
    latitude=None,
    longitude=None,
    radius_km=None,
    min_price=None,
    max_price=None,
    bedrooms=None,
    bathrooms=None,
    amenities=None,
):
    """Advanced property search (SRS 3.3.7), text and geography together.

    **The radius half is PostGIS, not arithmetic.** The previous implementation computed a
    haversine distance in Python over every row, which cannot use an index and reads the whole
    table to answer "within 5km". `geo_point` is `geography(Point, 4326)` with a GiST index
    already built, so `__distance_lte` is an index scan — the difference between SRS 5.1's
    one second over a million rows and not meeting it at all.

    `distance_km` is annotated and **returned**: the old serializer computed the distance and
    then never exposed it, so a map view had no way to sort by nearest.
    """
    if text:
        queryset = queryset.filter(
            Q(title__icontains=text)
            | Q(address_line_1__icontains=text)
            | Q(city__icontains=text)
            | Q(description__icontains=text)
        )

    if latitude is not None and longitude is not None:
        centre = Point(float(longitude), float(latitude), srid=4326)
        queryset = queryset.annotate(distance=Distance("geo_point", centre))
        if radius_km:
            queryset = queryset.filter(geo_point__distance_lte=(centre, D(km=float(radius_km))))
        queryset = queryset.order_by("distance")
    elif radius_km:
        raise ValueError("radius_km needs a latitude and longitude to measure from.")

    # Price lives on the listing, not the property — a property may carry a sale and a rental
    # listing at different figures — so the filter reaches through.
    if min_price is not None:
        queryset = queryset.filter(
            Q(listings__asking_price__gte=min_price)
            | Q(listings__rent_amount__gte=min_price)
        )
    if max_price is not None:
        queryset = queryset.filter(
            Q(listings__asking_price__lte=max_price)
            | Q(listings__rent_amount__lte=max_price)
        )
    if min_price is not None or max_price is not None:
        # The listing join is to-many, so a property with two matching listings would come
        # back twice. The scoping layer deliberately issues no DISTINCT, so this filter owns
        # the one it needs.
        queryset = queryset.distinct()

    if bedrooms is not None:
        queryset = queryset.filter(bedrooms__gte=bedrooms)
    if bathrooms is not None:
        queryset = queryset.filter(bathrooms__gte=bathrooms)
    if amenities:
        # JSONB containment, served by the GIN index on `amenities`. A chain of
        # `amenities__<key>=True` would be one index lookup per key.
        queryset = queryset.filter(amenities__contains={key: True for key in amenities})

    return queryset


def gallery(parent):
    """A property's, unit's or listing's media, cover first then in drag order."""
    key = (
        "property"
        if isinstance(parent, Property)
        else "unit"
        if isinstance(parent, Unit)
        else "listing"
    )
    return live_media().filter(**{key: parent}).order_by("-is_primary", "sort_order", "created_at")
