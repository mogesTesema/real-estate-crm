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
    return _kpis(
        leads=crm_selectors.visible_leads(user),
        deals=crm_selectors.visible_deals(user),
        properties=inventory_selectors.visible_properties(user),
        listings=inventory_selectors.visible_listings(user),
        contacts=contacts_selectors.visible_contacts(user),
        days=days,
    )


#: How a snapshot scope narrows each domain queryset. Mirrors the BRANCH/TEAM arms of the
#: scoping registry (leads: assigned agent OR team; deals: owner; properties: manager;
#: listings and contacts: assigned agent) so a snapshot for a branch answers the same
#: question a BRANCH-scoped manager's live dashboard does.
_SCOPE_FILTERS = {
    "BRANCH": {
        "leads": lambda sid: Q(assigned_agent__branch_id=sid) | Q(assigned_team__branch_id=sid),
        "deals": lambda sid: Q(owner__branch_id=sid),
        "properties": lambda sid: Q(managed_by__branch_id=sid),
        "listings": lambda sid: Q(assigned_agent__branch_id=sid)
        | Q(co_listing_agent__branch_id=sid),
        "contacts": lambda sid: Q(assigned_agent__branch_id=sid),
    },
    "TEAM": {
        "leads": lambda sid: Q(assigned_agent__team_id=sid) | Q(assigned_team_id=sid),
        "deals": lambda sid: Q(owner__team_id=sid),
        "properties": lambda sid: Q(managed_by__team_id=sid),
        "listings": lambda sid: Q(assigned_agent__team_id=sid)
        | Q(co_listing_agent__team_id=sid),
        "contacts": lambda sid: Q(assigned_agent__team_id=sid),
    },
}


def dashboard_for_scope(scope_type, scope_id=None, *, days=DEFAULT_WINDOW_DAYS):
    """The same KPI dict, computed for an organizational scope instead of a caller — the
    snapshot builder's entry point. ORG takes everything; BRANCH/TEAM narrow per
    `_SCOPE_FILTERS`."""
    from apps.contacts.selectors import live_contacts
    from apps.crm.selectors import live_deals, live_leads
    from apps.inventory.selectors import live_listings, live_properties

    querysets = {
        "leads": live_leads(),
        "deals": live_deals(),
        "properties": live_properties(),
        "listings": live_listings(),
        "contacts": live_contacts(),
    }
    if scope_type != "ORG":
        filters = _SCOPE_FILTERS.get(scope_type)
        if filters is None:
            raise KeyError(f"No snapshot rule for scope {scope_type!r}")
        querysets = {
            name: qs.filter(filters[name](scope_id)) for name, qs in querysets.items()
        }
    return _kpis(days=days, **querysets)


def _kpis(*, leads, deals, properties, listings, contacts, days):
    """The KPI arithmetic, over whatever querysets the caller scoped. One implementation
    serves the live dashboard and the snapshot builder, so the two cannot drift."""
    since = _window(days)

    recent_leads = leads.filter(created_at__gte=since)
    open_deals = deals.filter(status=Deal.Status.OPEN)
    won_deals = deals.filter(status=Deal.Status.WON, actual_close_date__gte=since.date())

    captured = recent_leads.count()
    converted = recent_leads.filter(status=Lead.Status.CONVERTED).count()

    funnel_rows = leads.values("status").annotate(count=Count("id"))

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
            "overdue": leads.filter(
                sla_breached=True, first_response_at__isnull=True
            ).count(),
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
            "properties": properties.count(),
            "available": properties.filter(status=Property.Status.AVAILABLE).count(),
            "live_listings": listings.filter(status__in=Listing.LIVE_STATUSES).count(),
        },
        "contacts": {
            "total": contacts.count(),
            "added": contacts.filter(created_at__gte=since).count(),
        },
        "funnel": {row["status"]: row["count"] for row in funnel_rows},
    }


def build_dashboard_snapshots(as_of=None):
    """Upsert a KPI_DAILY snapshot for the org, every branch and every team (SRS 3.13.1).
    The NULLS-NOT-DISTINCT unique constraint IS the idempotency — a re-run replaces the
    day's data instead of duplicating it."""
    from apps.identity.models import Branch, Team

    from .models import DashboardSnapshot

    as_of = as_of or timezone.localdate()
    targets = [("ORG", None)]
    targets += [("BRANCH", branch.pk) for branch in Branch.objects.all()]
    targets += [("TEAM", team.pk) for team in Team.objects.all()]

    written = []
    for scope_type, scope_id in targets:
        data = dashboard_for_scope(scope_type, scope_id)
        snapshot, _ = DashboardSnapshot.objects.update_or_create(
            snapshot_type=DashboardSnapshot.SnapshotType.KPI_DAILY,
            scope_type=scope_type,
            scope_id=scope_id,
            as_of_date=as_of,
            defaults={"data": _jsonable(data)},
        )
        written.append(snapshot)
    return written


def _jsonable(value):
    """Decimals → strings so the snapshot JSON round-trips losslessly."""
    from decimal import Decimal

    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return str(value)
    return value


def snapshot_for(user, *, as_of, scope_type="ORG", scope_id=None):
    """Read a stored snapshot, gated by what the caller could see live: ALL reads any;
    BRANCH reads its own branch and the org; TEAM reads its own team and the org. Returns
    None when no snapshot exists for that day."""
    from apps.identity.models import Role
    from apps.identity.scoping import scopes_for

    from .models import DashboardSnapshot

    scopes = scopes_for(user)
    allowed = Role.DataScope.ALL in scopes or user.is_superuser
    if not allowed and scope_type == "ORG":
        allowed = bool(scopes & {Role.DataScope.BRANCH, Role.DataScope.TEAM})
    if not allowed and scope_type == "BRANCH":
        allowed = Role.DataScope.BRANCH in scopes and str(user.branch_id) == str(scope_id)
    if not allowed and scope_type == "TEAM":
        allowed = (
            Role.DataScope.TEAM in scopes or Role.DataScope.BRANCH in scopes
        ) and str(user.team_id) == str(scope_id)
    if not allowed:
        return None
    return DashboardSnapshot.objects.filter(
        snapshot_type=DashboardSnapshot.SnapshotType.KPI_DAILY,
        scope_type=scope_type,
        scope_id=scope_id,
        as_of_date=as_of,
    ).first()


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
