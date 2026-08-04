"""
Lead engine (SRS §3.1): dedupe, routing, scoring, SLA, acknowledgment, convert.

All functions assume the tenant context is bound (via the request or a
tenant_context() block). They operate within the current tenant only.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from apps.activities.services import log_activity
from apps.contacts.models import Contact, normalize_email, normalize_phone
from apps.contacts.services import find_duplicates
from apps.core.models import Role, User
from apps.integrations import get_email_provider

from .models import Lead

# --- Scoring (SRS §3.1.6) ---------------------------------------------------
_SOURCE_WEIGHTS = {
    "referral": 40,
    "portal": 25,
    "website": 20,
    "walk_in": 30,
    "facebook": 15,
    "manual": 10,
}


def score_lead(lead: Lead) -> int:
    """Rule-based score 0-100 from source, budget presence, and completeness."""
    from .models import LeadSource

    source_weight = _SOURCE_WEIGHTS.get(lead.source)
    if source_weight is None and lead.tenant_id:
        catalog = (
            LeadSource.objects.filter(
                tenant_id=lead.tenant_id, key=lead.source, is_active=True
            )
            .values_list("weight", flat=True)
            .first()
        )
        source_weight = catalog if catalog is not None else 10
    score = source_weight if source_weight is not None else 10
    if lead.budget_max:
        score += 20
    if lead.preferred_location:
        score += 10
    if lead.timeline in ("immediate", "1_month", "this_month"):
        score += 20
    if lead.phone:
        score += 10
    return min(score, 100)


# --- Routing (SRS §3.1.5) ---------------------------------------------------
def _tenant_agents(tenant_id):
    return User.objects.filter(
        tenant_id=tenant_id,
        is_active=True,
        role__in=[Role.AGENT, Role.PROPERTY_MANAGER],
    )


def route_lead(lead: Lead, *, strategy: str = "round_robin") -> User | None:
    """Assign the lead to an agent by tenant strategy."""
    agents = _tenant_agents(lead.tenant_id)
    if not agents.exists():
        return None
    annotated = agents.annotate(
        active_leads=Count(
            "leads",
            filter=Q(leads__status__in=["new", "contacted", "qualified"])
            & Q(leads__is_deleted=False),
        )
    )
    if strategy == "first_available":
        free = annotated.filter(active_leads=0).order_by("created_at").first()
        agent = free or annotated.order_by("active_leads", "created_at").first()
    else:
        # round_robin (default): fewest active leads first
        agent = annotated.order_by("active_leads", "created_at").first()
    lead.assigned_agent = agent
    return agent


def _notify_assignment(lead: Lead) -> None:
    if not lead.assigned_agent_id:
        return
    from apps.core.models import Notification

    Notification.objects.create(
        tenant_id=lead.tenant_id,
        user_id=lead.assigned_agent_id,
        title="New lead assigned",
        body=f"{lead.name or lead.email or 'Lead'} ({lead.lead_type})",
        link=f"/leads?id={lead.id}",
    )


# --- Dedupe (SRS §3.1.3, §3.1.10) -------------------------------------------
def detect_duplicates(lead: Lead):
    return find_duplicates(email=lead.email, phone=lead.phone, name=lead.name)


# --- Acknowledgment (SRS §3.1.11) -------------------------------------------
def send_acknowledgment(lead: Lead) -> None:
    if not lead.email:
        return
    get_email_provider().send(
        to=lead.email,
        subject="Thanks for reaching out",
        body="We received your inquiry and an agent will contact you shortly.",
    )
    lead.acknowledged = True


# --- Capture orchestration --------------------------------------------------
def capture_lead(*, tenant_id, data: dict, actor=None) -> Lead:
    """
    Create a lead end-to-end: link/create contact, dedupe, score, route, set SLA,
    acknowledge, and log to the timeline. This is the single entry point used by the
    API and any inbound webhook.
    """
    lead = Lead(
        tenant_id=tenant_id,
        name=data.get("name", ""),
        email=normalize_email(data.get("email", "")),
        phone=normalize_phone(data.get("phone", "")) or data.get("phone", ""),
        lead_type=data["lead_type"],
        source=data.get("source", "manual"),
        budget_min=data.get("budget_min"),
        budget_max=data.get("budget_max"),
        preferred_location=data.get("preferred_location", ""),
        bedrooms=data.get("bedrooms"),
        timeline=data.get("timeline", ""),
        notes=data.get("notes", ""),
    )

    # Dedupe against existing contacts; link if found, else create a contact.
    dupes = detect_duplicates(lead)
    match = dupes.first()
    if match:
        lead.contact = match
        lead.is_possible_duplicate = True
    else:
        lead.contact = Contact.objects.create(
            tenant_id=tenant_id,
            full_name=lead.name,
            email=lead.email,
            phone=lead.phone,
            source=lead.source,
        )

    lead.score = score_lead(lead)
    from apps.core.models import Tenant

    strategy = "round_robin"
    if tenant_id:
        strategy = (
            Tenant.objects.filter(pk=tenant_id)
            .values_list("lead_routing_strategy", flat=True)
            .first()
            or "round_robin"
        )
    route_lead(lead, strategy=strategy)
    lead.sla_due_at = timezone.now() + timedelta(minutes=settings.LEAD_SLA_MINUTES)
    lead.save()
    _notify_assignment(lead)

    send_acknowledgment(lead)
    if lead.acknowledged:
        lead.save(update_fields=["acknowledged", "updated_at"])

    log_activity(
        tenant_id=tenant_id,
        actor=actor,
        activity_type="note",
        body=f"Lead captured from {lead.source} and assigned to "
        f"{lead.assigned_agent or 'unassigned'}.",
        related=lead.contact,
    )
    return lead


# --- Convert to opportunity (SRS §3.1.8) ------------------------------------
def convert_lead(*, lead: Lead, pipeline, stage, actor=None):
    from apps.deals.models import Opportunity

    if lead.converted_opportunity_id:
        return lead.converted_opportunity

    opp = Opportunity.objects.create(
        tenant_id=lead.tenant_id,
        title=f"{lead.get_lead_type_display()} — {lead.contact}",
        pipeline=pipeline,
        stage=stage,
        contact=lead.contact,
        value=lead.budget_max or 0,
        probability=stage.probability,
        assigned_agent=lead.assigned_agent,
    )
    lead.converted_opportunity = opp
    lead.status = Lead.Status.CONVERTED
    lead.save(update_fields=["converted_opportunity", "status", "updated_at"])

    log_activity(
        tenant_id=lead.tenant_id,
        actor=actor,
        activity_type="note",
        body=f"Lead converted to opportunity '{opp.title}'.",
        related=lead.contact,
    )
    return opp
