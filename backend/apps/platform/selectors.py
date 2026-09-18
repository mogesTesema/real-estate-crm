"""Public reads / scoped querysets for `platform` (architecture.md §1.2).

Other apps read `platform` rows through this module. They must never import
`apps.platform.api` — that package is the HTTP surface and is private to this app.

**How the analytics here reach domain data.** Through each app's `selectors` module, never its
models or its `services` — §1.2 makes the selector the public read surface, and the satellite
contract in `pyproject.toml` bars this app from every domain `services` module, enforced by
`lint-imports` in CI. The practical consequence is that every figure below is computed over an
`apply_scope`d queryset, so a dashboard cannot become the one place row visibility does not
apply.
"""
from datetime import timedelta

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

from apps.contacts import selectors as contacts_selectors
from apps.crm import selectors as crm_selectors
from apps.crm.models import Deal, Lead
from apps.inventory import selectors as inventory_selectors
from apps.inventory.models import Listing, Property

#: The KPI window. A dashboard without one reports the company's whole history, where a bad
#: month is invisible against three good years.
DEFAULT_WINDOW_DAYS = 30


def _window(days):
    return timezone.now() - timedelta(days=days or DEFAULT_WINDOW_DAYS)


def dashboard(user, *, days=DEFAULT_WINDOW_DAYS):
    """Role-aware KPIs (SRS 3.13.1).

    > "The System shall provide role-based dashboards for agents, managers, and
    >  administrators."

    There is deliberately no branch on role here. The figures are the same questions for
    everyone — how many leads, how many converted, what is the pipeline worth — and the *scope*
    is what differs, which `apply_scope` already decides from the caller's `data_scope`. An
    agent sees their own numbers and a manager the branch's, from one code path. Branching on
    role would be a second place for visibility rules to live, and the two would drift.
    """
    since = _window(days)

    leads = crm_selectors.visible_leads(user)
    recent_leads = leads.filter(created_at__gte=since)
    deals = crm_selectors.visible_deals(user)
    open_deals = deals.filter(status=Deal.Status.OPEN)
    won_deals = deals.filter(status=Deal.Status.WON, actual_close_date__gte=since.date())

    captured = recent_leads.count()
    converted = recent_leads.filter(status=Lead.Status.CONVERTED).count()

    return {
        "window_days": days or DEFAULT_WINDOW_DAYS,
        "leads": {
            "captured": captured,
            "converted": converted,
            # Percentage points, rounded once here rather than in each client. 0 leads means
            # no rate, not a rate of zero — the distinction matters on a new agent's first day.
            "conversion_rate": round(converted / captured * 100, 1) if captured else None,
            "open": leads.exclude(
                status__in=(Lead.Status.CONVERTED, Lead.Status.LOST)
            ).count(),
            "overdue": crm_selectors.overdue_leads(user).count(),
            "unassigned": leads.filter(
                assigned_agent__isnull=True, assigned_team__isnull=True
            ).exclude(status__in=(Lead.Status.CONVERTED, Lead.Status.LOST)).count(),
            "average_score": round(recent_leads.aggregate(v=Avg("score"))["v"] or 0, 1),
        },
        "pipeline": {
            "open_deals": open_deals.count(),
            "open_value": open_deals.aggregate(v=Sum("estimated_value"))["v"] or 0,
            "weighted_forecast": crm_selectors.weighted_pipeline(open_deals),
            "won_this_period": won_deals.count(),
            "won_value": won_deals.aggregate(v=Sum("estimated_value"))["v"] or 0,
        },
        "response": {
            "median_minutes": average_response_minutes(recent_leads),
            "answered_within_sla": recent_leads.filter(
                first_response_at__isnull=False, sla_breached=False
            ).count(),
            "breached": recent_leads.filter(sla_breached=True).count(),
        },
        "inventory": {
            "properties": inventory_selectors.visible_properties(user).count(),
            "available": inventory_selectors.visible_properties(user)
            .filter(status=Property.Status.AVAILABLE)
            .count(),
            "live_listings": inventory_selectors.visible_listings(user)
            .filter(status__in=Listing.LIVE_STATUSES)
            .count(),
        },
        "contacts": {
            "total": contacts_selectors.visible_contacts(user).count(),
            "added": contacts_selectors.visible_contacts(user)
            .filter(created_at__gte=since)
            .count(),
        },
        "funnel": crm_selectors.lead_funnel(user),
    }


def average_response_minutes(leads):
    """Mean minutes from capture to first response, over the answered leads only.

    Computed as a database interval rather than in Python: the alternative pulls every lead
    across the wire to subtract two timestamps. Unanswered leads are excluded on purpose —
    including them as zero would flatter the figure, and including them as "now minus capture"
    would make it drift upward every time the dashboard is refreshed.
    """
    answered = leads.filter(first_response_at__isnull=False)
    average = answered.aggregate(
        v=Avg(
            ExpressionWrapper(
                F("first_response_at") - F("created_at"), output_field=DurationField()
            )
        )
    )["v"]
    return round(average.total_seconds() / 60, 1) if average else None


# --- Reports (SRS 3.13.2) -------------------------------------------------------------------


def lead_source_roi(user, *, days=DEFAULT_WINDOW_DAYS):
    """Which sources actually produce business, not just volume.

    Volume alone ranks the cheapest channel first. Pairing it with conversions and won value
    is what makes the report answer the question it is named for.
    """
    since = _window(days)
    rows = (
        crm_selectors.visible_leads(user)
        .filter(created_at__gte=since)
        .values("source__id", "source__name", "source__source_type")
        .annotate(
            leads=Count("id"),
            converted=Count("id", filter=Q(status=Lead.Status.CONVERTED)),
            won_value=Sum(
                "deals__estimated_value", filter=Q(deals__status=Deal.Status.WON)
            ),
        )
        .order_by("-leads")
    )
    return [
        {
            "source_id": str(row["source__id"]) if row["source__id"] else None,
            "source": row["source__name"] or "Unattributed",
            "source_type": row["source__source_type"],
            "leads": row["leads"],
            "converted": row["converted"],
            "conversion_rate": (
                round(row["converted"] / row["leads"] * 100, 1) if row["leads"] else None
            ),
            "won_value": row["won_value"] or 0,
        }
        for row in rows
    ]


def agent_leaderboard(user, *, days=DEFAULT_WINDOW_DAYS):
    """Per-agent performance over the caller's visible set.

    Scoped like everything else: an agent running this sees one row — their own — which is the
    correct answer, not an empty report.
    """
    since = _window(days)
    leads = (
        crm_selectors.visible_leads(user)
        .filter(created_at__gte=since, assigned_agent__isnull=False)
        .values("assigned_agent__id", "assigned_agent__first_name", "assigned_agent__last_name")
        .annotate(
            leads=Count("id"),
            converted=Count("id", filter=Q(status=Lead.Status.CONVERTED)),
            breached=Count("id", filter=Q(sla_breached=True)),
        )
    )
    by_agent = {
        row["assigned_agent__id"]: {
            "agent_id": str(row["assigned_agent__id"]),
            "agent": " ".join(
                filter(
                    None,
                    [row["assigned_agent__first_name"], row["assigned_agent__last_name"]],
                )
            ),
            "leads": row["leads"],
            "converted": row["converted"],
            "sla_breaches": row["breached"],
            "deals_won": 0,
            "won_value": 0,
        }
        for row in leads
    }

    deals = (
        crm_selectors.visible_deals(user)
        .filter(status=Deal.Status.WON, actual_close_date__gte=since.date())
        .values("owner__id", "owner__first_name", "owner__last_name")
        .annotate(won=Count("id"), value=Sum("estimated_value"))
    )
    for row in deals:
        entry = by_agent.setdefault(
            row["owner__id"],
            {
                "agent_id": str(row["owner__id"]),
                "agent": " ".join(
                    filter(None, [row["owner__first_name"], row["owner__last_name"]])
                ),
                "leads": 0,
                "converted": 0,
                "sla_breaches": 0,
                "deals_won": 0,
                "won_value": 0,
            },
        )
        entry["deals_won"] = row["won"]
        entry["won_value"] = row["value"] or 0

    return sorted(by_agent.values(), key=lambda r: (-r["won_value"], -r["converted"]))


def inventory_aging(user):
    """How long live listings have been on the market (SRS 3.13.2).

    Bucketed rather than listed: "eleven listings over 90 days old" is the number a manager
    acts on, where a thousand rows sorted by date is not.
    """
    now = timezone.now()
    listings = (
        inventory_selectors.visible_listings(user)
        .filter(status__in=Listing.LIVE_STATUSES)
        .select_related("property", "assigned_agent")
    )
    buckets = {"0-30": 0, "31-60": 0, "61-90": 0, "90+": 0, "unpublished": 0}
    stale = []
    for listing in listings:
        if listing.published_at is None:
            buckets["unpublished"] += 1
            continue
        days = (now - listing.published_at).days
        if days <= 30:
            buckets["0-30"] += 1
        elif days <= 60:
            buckets["31-60"] += 1
        elif days <= 90:
            buckets["61-90"] += 1
        else:
            buckets["90+"] += 1
            stale.append(
                {
                    "id": str(listing.pk),
                    "reference_code": listing.reference_code,
                    "title": listing.title,
                    "days_live": days,
                    "agent": listing.assigned_agent.full_name
                    if listing.assigned_agent
                    else None,
                }
            )
    return {
        "buckets": buckets,
        "total": sum(buckets.values()),
        "oldest": sorted(stale, key=lambda r: -r["days_live"])[:25],
    }


#: report name -> (callable, whether it accepts a `days` window)
REPORTS = {
    "lead-source-roi": (lead_source_roi, True),
    "agent-leaderboard": (agent_leaderboard, True),
    "inventory-aging": (inventory_aging, False),
}


def run_report(name, user, *, days=DEFAULT_WINDOW_DAYS):
    if name not in REPORTS:
        raise KeyError(name)
    fn, takes_window = REPORTS[name]
    return fn(user, days=days) if takes_window else fn(user)
