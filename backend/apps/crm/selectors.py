"""Public reads / scoped querysets for `crm` (architecture.md §1.2).

Other apps read `crm` rows through this module. They must never import `apps.crm.api` — that
package is the HTTP surface and is private to this app.
"""
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

from apps.identity.selectors import apply_scope

from .models import Deal, Lead, Viewing


def live_leads():
    return Lead.objects.filter(deleted_at__isnull=True)


def live_deals():
    return Deal.objects.filter(deleted_at__isnull=True)


def visible_leads(user):
    return apply_scope(live_leads(), user, "lead")


def visible_deals(user):
    return apply_scope(live_deals(), user, "deal")


def visible_viewings(user):
    return apply_scope(Viewing.objects.all(), user, "viewing")


def overdue_leads(user):
    """Leads past their SLA with no response yet (SRS 3.1.9), scoped to the caller."""
    return visible_leads(user).filter(sla_breached=True, first_response_at__isnull=True)


def weighted_pipeline(queryset):
    """Forecast value: sum of `estimated_value * probability / 100` (SRS 3.4.3).

    Annotated in SQL rather than summed in Python. A forecast is computed for every board, and
    pulling a thousand deals across the wire to multiply two columns is the difference between
    a dashboard that loads and one that times out.
    """
    return queryset.aggregate(
        weighted=Sum(
            ExpressionWrapper(
                F("estimated_value") * F("probability") / 100,
                output_field=DecimalField(max_digits=18, decimal_places=2),
            )
        )
    )["weighted"] or 0


def board(user, *, pipeline=None, filters=None):
    """The Kanban board (SRS 3.4.6): one bucket per stage, with its deals.

    Composes with row scoping and the caller's filters — a board is a *view* of the deals the
    caller may already see, never a second way to reach them.

    `select_related` on everything a card renders. The old board was an N+1 per card because
    "days in stage" walked the stage history; `stage_entered_at` (v3.3) makes it a
    subtraction, and prefetching the rest keeps a fifty-card board at a handful of queries.
    """
    from .models import Pipeline

    pipeline = pipeline or Pipeline.objects.filter(is_default=True, is_active=True).first()
    if pipeline is None:
        return {"pipeline": None, "stages": []}

    deals = (
        visible_deals(user)
        .filter(pipeline=pipeline)
        .select_related("stage", "owner", "primary_contact", "pipeline")
        .prefetch_related("deal_properties__property")
    )
    if filters:
        deals = deals.filter(**filters)

    now = timezone.now()
    buckets = {stage.pk: [] for stage in pipeline.stages.order_by("sort_order")}
    for deal in deals:
        deal.days_in_stage = (
            (now - deal.stage_entered_at).days if deal.stage_entered_at else None
        )
        buckets.setdefault(deal.stage_id, []).append(deal)

    return {
        "pipeline": pipeline,
        "stages": [
            {
                "stage": stage,
                "deals": buckets.get(stage.pk, []),
                "count": len(buckets.get(stage.pk, [])),
                "value": sum(d.estimated_value for d in buckets.get(stage.pk, [])),
            }
            for stage in pipeline.stages.order_by("sort_order")
        ],
    }


def lead_funnel(user):
    """Counts by lead status over the caller's visible set — the conversion funnel (3.13.1)."""
    rows = visible_leads(user).values("status").annotate(count=Count("id"))
    return {row["status"]: row["count"] for row in rows}


def source_performance(user):
    """Leads and conversions per source — lead-source ROI (SRS 3.13.2)."""
    return list(
        visible_leads(user)
        .values("source__id", "source__name", "source__source_type")
        .annotate(
            leads=Count("id"),
            converted=Count("id", filter=Q(status=Lead.Status.CONVERTED)),
        )
        .order_by("-leads")
    )


# --- Matching engine (SRS 3.3.8) ------------------------------------------------------------
#
# Forward: what should we show this lead? Reverse: who should hear about this listing?
# Both sides tolerate missing data — a lead with no budget still matches on type and area,
# because "no answer yet" is not "matches nothing".

#: Which listing types serve which lead types. SELL and RENT_OUT supply stock — they have
#: no demand side to match against, and return empty by design.
LEAD_TYPE_TO_LISTING_TYPES = {
    "BUY": ("SALE", "SALE_AND_RENT"),
    "INVEST": ("SALE", "SALE_AND_RENT"),
    "RENT_IN": ("RENT", "SALE_AND_RENT"),
}

#: Budget tolerance: a 1.05M listing is still worth showing a 1M-budget buyer (SRS 3.3.8).
BUDGET_TOLERANCE = 0.10


def matching_listings_for_lead(lead, *, since=None):
    """ACTIVE listings this lead should see. `since` powers saved-search alerts — only
    what appeared after the last run."""
    from decimal import Decimal

    from django.contrib.gis.geos import Point
    from django.contrib.gis.measure import D

    from apps.inventory.models import Listing
    from apps.inventory.selectors import live_listings

    types = LEAD_TYPE_TO_LISTING_TYPES.get(lead.lead_type)
    if not types:
        return Listing.objects.none()

    queryset = (
        live_listings()
        .filter(status=Listing.Status.ACTIVE, listing_type__in=types)
        .select_related("property", "property__property_type")
    )

    low = Decimal(1) - Decimal(str(BUDGET_TOLERANCE))
    high = Decimal(1) + Decimal(str(BUDGET_TOLERANCE))
    price_field = "rent_amount" if lead.lead_type == "RENT_IN" else "asking_price"
    if lead.budget_min:
        queryset = queryset.filter(
            **{f"{price_field}__gte": Decimal(lead.budget_min) * low}
        )
    if lead.budget_max:
        queryset = queryset.filter(
            **{f"{price_field}__lte": Decimal(lead.budget_max) * high}
        )
    if lead.preferred_bedrooms:
        queryset = queryset.filter(property__bedrooms__gte=lead.preferred_bedrooms)
    if lead.preferred_property_type:
        queryset = queryset.filter(
            property__property_type__name__icontains=lead.preferred_property_type
        )

    # Location: geo preferences use the GiST index (dwithin); text preferences fall back to
    # city/address containment. All preferences OR together — the lead is interested in ANY
    # of their areas.
    area = Q()
    for pref in lead.location_preferences.all():
        if pref.latitude is not None and pref.longitude is not None and pref.radius_km:
            centre = Point(float(pref.longitude), float(pref.latitude), srid=4326)
            area |= Q(
                property__geo_point__dwithin=(centre, D(km=float(pref.radius_km)))
            )
        elif pref.location:
            area |= (
                Q(property__city__icontains=pref.location)
                | Q(property__address_line_1__icontains=pref.location)
            )
    if not area and lead.preferred_location:
        area = (
            Q(property__city__icontains=lead.preferred_location)
            | Q(property__address_line_1__icontains=lead.preferred_location)
        )
    if area:
        queryset = queryset.filter(area)

    if since:
        queryset = queryset.filter(
            Q(published_at__gte=since) | Q(created_at__gte=since)
        )
    return queryset.order_by("-published_at", "-created_at")


#: The demand side of each listing type.
LISTING_TYPE_TO_LEAD_TYPES = {
    "SALE": ("BUY", "INVEST"),
    "RENT": ("RENT_IN",),
    "SALE_AND_RENT": ("BUY", "INVEST", "RENT_IN"),
}

#: Lead statuses still in the market.
_OPEN_LEAD_STATUSES = ("NEW", "CONTACTED", "QUALIFIED", "NURTURING")


def matching_leads_for_listing(listing, user):
    """Open leads — **scoped to the caller** — whose requirements this listing satisfies.

    SQL narrows on type, status, budget window and bedrooms; the per-preference radius test
    runs in Python because the coordinates live as decimals on the preference rows, not as
    a geography column. The scoped, narrowed set is small; the arithmetic is not the cost.
    """
    from decimal import Decimal

    lead_types = LISTING_TYPE_TO_LEAD_TYPES.get(listing.listing_type, ())
    queryset = apply_scope(live_leads(), user, "lead").filter(
        lead_type__in=lead_types, status__in=_OPEN_LEAD_STATUSES
    ).select_related("contact").prefetch_related("location_preferences")

    low = Decimal(1) - Decimal(str(BUDGET_TOLERANCE))
    high = Decimal(1) + Decimal(str(BUDGET_TOLERANCE))
    sale_price, rent_price = listing.asking_price, listing.rent_amount
    prop = listing.property

    matches = []
    for lead in queryset:
        price = rent_price if lead.lead_type == "RENT_IN" else sale_price
        if price is not None:
            if lead.budget_max is not None and price > Decimal(lead.budget_max) * high:
                continue
            if lead.budget_min is not None and price < Decimal(lead.budget_min) * low:
                continue
        if (
            lead.preferred_bedrooms is not None
            and prop.bedrooms is not None
            and prop.bedrooms < lead.preferred_bedrooms
        ):
            continue

        prefs = list(lead.location_preferences.all())
        if prefs:
            def _pref_ok(pref):
                if (
                    pref.latitude is not None and pref.longitude is not None
                    and pref.radius_km and prop.latitude is not None
                    and prop.longitude is not None
                ):
                    return _haversine_km(
                        float(pref.latitude), float(pref.longitude),
                        float(prop.latitude), float(prop.longitude),
                    ) <= float(pref.radius_km)
                if pref.location:
                    needle = pref.location.lower()
                    return (
                        needle in (prop.city or "").lower()
                        or needle in (prop.address_line_1 or "").lower()
                    )
                return False

            if not any(_pref_ok(pref) for pref in prefs):
                continue
        matches.append(lead)
    return matches


def _haversine_km(lat1, lon1, lat2, lon2):
    import math

    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
