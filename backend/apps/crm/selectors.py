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
