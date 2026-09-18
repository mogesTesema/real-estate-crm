"""The saved-report executor (SRS 3.13.3) — a safe interpreter for user-authored JSON.

A report definition is data written by one user and executed for many, possibly years
later. So nothing in a definition ever reaches `filter()` raw: `base` picks from a
registry, and filters/columns/group_by pass per-dataset whitelists that map to ORM lookups
this module owns. Two halves of the model doctrine:

* **visibility** gates who may open the DEFINITION (owner | TEAM/BRANCH of the owner | ORG);
* **execution re-scopes rows through the caller** — an ORG-shared report opened by an agent
  renders only the agent's rows, because the dataset callables take the requesting user
  through `apply_scope`d selectors.

Analytic bases wrap the existing report selectors; tabular bases (leads/deals/listings)
return rows from scoped querysets. `ROW_CAP` bounds every run; every run is audited EXPORT.
"""
import logging

from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

ROW_CAP = 5000


def _leads_queryset(user):
    from apps.crm.selectors import visible_leads

    return visible_leads(user).select_related("contact", "source", "assigned_agent")


def _deals_queryset(user):
    from apps.crm.selectors import visible_deals

    return visible_deals(user).select_related("owner", "stage", "primary_contact")


def _listings_queryset(user):
    from apps.inventory.selectors import visible_listings

    return visible_listings(user).select_related("property", "assigned_agent")


#: dataset -> (queryset builder, {filter name -> ORM lookup}, {column -> ORM path})
TABULAR = {
    "leads": (
        _leads_queryset,
        {
            "status": "status", "lead_type": "lead_type", "priority": "priority",
            "source": "source__name", "assigned_agent": "assigned_agent_id",
            "min_score": "score__gte", "max_score": "score__lte",
            "created_after": "created_at__gte", "created_before": "created_at__lte",
        },
        {
            "id": "id", "status": "status", "lead_type": "lead_type",
            "priority": "priority", "score": "score",
            "contact": "contact__first_name", "contact_email": "contact__email",
            "source": "source__name", "assigned_agent": "assigned_agent__email",
            "budget_min": "budget_min", "budget_max": "budget_max",
            "created_at": "created_at",
        },
    ),
    "deals": (
        _deals_queryset,
        {
            "status": "status", "deal_type": "deal_type", "owner": "owner_id",
            "stage": "stage__code", "min_value": "estimated_value__gte",
            "max_value": "estimated_value__lte",
            "created_after": "created_at__gte", "created_before": "created_at__lte",
        },
        {
            "id": "id", "reference_code": "reference_code", "title": "title",
            "status": "status", "stage": "stage__name",
            "estimated_value": "estimated_value", "probability": "probability",
            "owner": "owner__email", "contact": "primary_contact__first_name",
            "currency": "currency", "created_at": "created_at",
            "actual_close_date": "actual_close_date",
        },
    ),
    "listings": (
        _listings_queryset,
        {
            "status": "status", "listing_type": "listing_type",
            "city": "property__city__icontains",
            "min_price": "asking_price__gte", "max_price": "asking_price__lte",
            "assigned_agent": "assigned_agent_id",
        },
        {
            "id": "id", "reference_code": "reference_code", "title": "title",
            "status": "status", "listing_type": "listing_type",
            "asking_price": "asking_price", "rent_amount": "rent_amount",
            "city": "property__city", "bedrooms": "property__bedrooms",
            "assigned_agent": "assigned_agent__email", "published_at": "published_at",
        },
    ),
}

#: Analytic bases: the pre-built report selectors, executed for the requesting user.
ANALYTIC = {"lead-source-roi", "agent-leaderboard", "inventory-aging"}


def validate_definition(definition):
    """Refuse bad definitions at save time, not at 2 AM in the scheduler."""
    if not isinstance(definition, dict):
        raise ValidationError({"definition": "A report definition is an object."})
    base = definition.get("base")
    if base in ANALYTIC:
        return
    if base not in TABULAR:
        known = sorted(ANALYTIC) + sorted(TABULAR)
        raise ValidationError({"definition": f"Unknown base {base!r}; pick one of {known}."})
    _, filters, columns = TABULAR[base]
    unknown = set(definition.get("filters", {})) - set(filters)
    if unknown:
        raise ValidationError({"definition": f"Unknown filters: {sorted(unknown)}"})
    unknown = set(definition.get("columns", [])) - set(columns)
    if unknown:
        raise ValidationError({"definition": f"Unknown columns: {sorted(unknown)}"})
    group_by = definition.get("group_by")
    if group_by and group_by not in columns:
        raise ValidationError({"definition": f"Cannot group by {group_by!r}."})


def execute(definition, user):
    """Run a definition as `user`. Returns {"columns": [...], "rows": [...]} for tabular
    bases, or the analytic selector's own payload."""
    validate_definition(definition)
    base = definition.get("base")
    days = definition.get("days")

    if base in ANALYTIC:
        from .selectors import run_report

        return {"base": base, "data": run_report(
            base, user, **({"days": days} if days else {})
        )}

    builder, filter_map, column_map = TABULAR[base]
    queryset = builder(user)
    if days:
        from datetime import timedelta

        from django.utils import timezone

        queryset = queryset.filter(created_at__gte=timezone.now() - timedelta(days=int(days)))
    for name, value in (definition.get("filters") or {}).items():
        queryset = queryset.filter(**{filter_map[name]: value})

    columns = definition.get("columns") or list(column_map)
    group_by = definition.get("group_by")
    if group_by:
        from django.db.models import Count

        rows = list(
            queryset.values(column_map[group_by])
            .annotate(count=Count("id"))
            .order_by("-count")[:ROW_CAP]
        )
        return {
            "base": base,
            "columns": [group_by, "count"],
            "rows": [
                {group_by: row[column_map[group_by]], "count": row["count"]}
                for row in rows
            ],
        }

    paths = [column_map[name] for name in columns]
    rows = [
        dict(zip(columns, values, strict=False))
        for values in queryset.values_list(*paths)[:ROW_CAP]
    ]
    return {"base": base, "columns": columns, "rows": rows}


def execute_and_audit(saved_report, user):
    from .services import record_event

    result = execute(saved_report.definition, user)
    record_event(
        action="EXPORT",
        entity_type="SAVED_REPORT",
        entity_id=saved_report.pk,
        actor=user,
        new_values={"name": saved_report.name, "base": result.get("base")},
    )
    return result
