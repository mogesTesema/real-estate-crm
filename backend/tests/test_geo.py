"""PostGIS wiring for inventory search (architecture.md §2 "Geospatial", SRS 3.3.7).

Proves the whole chain works end to end: the postgis extension is installed, the geography
column round-trips, GeoDjango's distance lookups translate, and a radius query discriminates.
Without this, a broken geo setup would not surface until the search endpoint is built.
"""
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D

from apps.inventory.models import Property

# Dubai Marina, and a point ~11km away near Downtown Dubai.
MARINA = Point(55.1400, 25.0800, srid=4326)
DOWNTOWN = Point(55.2744, 25.1972, srid=4326)


def test_geo_point_round_trips(make_property):
    prop = make_property(geo_point=MARINA)
    prop.refresh_from_db()
    assert prop.geo_point.srid == 4326
    assert round(prop.geo_point.x, 4) == 55.1400


def test_radius_search_includes_near_and_excludes_far(make_property):
    near = make_property(title="Marina tower", geo_point=MARINA)
    far = make_property(title="Downtown loft", geo_point=DOWNTOWN)

    within_5km = Property.objects.filter(geo_point__distance_lte=(MARINA, D(km=5)))

    assert near in within_5km
    assert far not in within_5km


def test_a_property_with_no_coordinates_is_simply_not_matched(make_property):
    """geo_point is nullable — an un-geocoded property must not break the query."""
    make_property(title="No coordinates yet", geo_point=None)
    assert Property.objects.filter(geo_point__distance_lte=(MARINA, D(km=5))).count() == 0


def test_amenities_containment_query_works(make_property):
    """The GIN index exists for exactly this lookup shape."""
    pool = make_property(amenities={"pool": True, "gym": True})
    make_property(amenities={"parking": True})

    assert list(Property.objects.filter(amenities__contains={"pool": True})) == [pool]
