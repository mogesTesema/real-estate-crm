"""Public write API for `crm` (architecture.md §1.2).

Marketing, lead sources, leads, pipelines, deals, viewings, offers, transactions.

This is the ONLY module another app may import to mutate `crm`-owned rows.

**Cross-app calls made from here, and why they are legal.** `crm` sits above `contacts` and
`inventory` in the import DAG (§1.2), so `capture_lead` creating a contact through
`contacts.services.find_or_create_contact` is the arrow pointing the right way. It must never
be `Contact.objects.create`: normalisation, de-duplication and the role set all live behind
that service, and a second creation path would quietly bypass all three.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.contacts import services as contacts_services

from . import signals
from .models import (
    Deal,
    DealStageHistory,
    Lead,
    LeadAssignment,
    LeadLocationPreference,
    LeadRoutingRule,
    LeadSource,
    LeadStatusHistory,
    Offer,
    Transaction,
)

logger = logging.getLogger(__name__)


# --- Lead scoring (SRS 3.1.6) -------------------------------------------------------------
#
# "The System shall support lead scoring based on configurable criteria."
#
# The weights are ported from the implementation at tag `phase1-flat-layout`, where they were
# tuned against real captured leads. Source weight dominates because it is the strongest
# single predictor in this market: a referral converts several times more often than a cold
# portal enquiry, so the ordering below is the substance, not the exact numbers.

SOURCE_WEIGHTS = {
    LeadSource.SourceType.REFERRAL: 40,
    LeadSource.SourceType.WALK_IN: 30,
    LeadSource.SourceType.PROPERTY_PORTAL: 25,
    LeadSource.SourceType.WEBSITE: 20,
    LeadSource.SourceType.CAMPAIGN: 20,
    LeadSource.SourceType.WHATSAPP: 18,
    LeadSource.SourceType.PHONE: 18,
    LeadSource.SourceType.FACEBOOK: 15,
    LeadSource.SourceType.INSTAGRAM: 15,
    LeadSource.SourceType.MANUAL: 10,
}

#: Timeframes that mean "this quarter or sooner". Compared case-insensitively against the
#: free-text `expected_timeframe`, because the field is filled by web forms we do not control.
URGENT_TIMEFRAMES = ("immediate", "urgent", "asap", "1 month", "this month", "1-3 months")

MAX_SCORE = 100


def score_lead(lead):
    """The lead's score out of 100 — how qualified, not how valuable.

    Each component answers a different question about whether this person will transact:
    where they came from, whether they have told us a budget, whether they have told us where,
    how soon, and whether we can reach them.
    """
    score = 0
    if lead.source_id:
        score += SOURCE_WEIGHTS.get(lead.source.source_type, 10)
    else:
        score += SOURCE_WEIGHTS[LeadSource.SourceType.MANUAL]

    # A stated budget is the single clearest signal that someone has decided to buy.
    if lead.budget_min or lead.budget_max:
        score += 20
    if lead.preferred_location or lead.location_preferences.exists():
        score += 10
    timeframe = (lead.expected_timeframe or "").lower()
    if any(marker in timeframe for marker in URGENT_TIMEFRAMES):
        score += 20
    # A reachable lead is worth more than an unreachable one, however qualified.
    if lead.contact and lead.contact.phone:
        score += 10

    return min(score, MAX_SCORE)


# --- De-duplication (SRS 3.1.10, 3.1.11) --------------------------------------------------


def is_possible_duplicate(contact, lead_type, target_property):
    """SRS 3.1.11's rule, exactly.

    > "flag potential duplicate inquiries when the same contact details are submitted for the
    >  same property and the same inquiry type"

    Note what it is *not*: the same person enquiring about a second property is a second
    genuine lead, and flagging that would train agents to ignore the flag. The point is to
    stop two agents independently contacting the same prospect about the same thing — hence a
    flag rather than a refusal, so the lead is never lost.
    """
    if contact is None:
        return False
    open_statuses = (Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED,
                     Lead.Status.NURTURING)
    return (
        Lead.objects.filter(
            deleted_at__isnull=True,
            contact=contact,
            lead_type=lead_type,
            target_property=target_property,
            status__in=open_statuses,
        )
        .exists()
    )


# --- Routing (SRS 3.1.5) --------------------------------------------------------------------


def matching_rule(lead):
    """The first active routing rule whose criteria the lead satisfies, by ascending priority.

    `criteria` is JSON of the form `{"lead_type": "BUY", "source_type": "REFERRAL",
    "min_score": 50, "city": "Dubai"}`. Every key present must match; an empty criteria object
    matches everything, which is how a catch-all rule is written.
    """
    for rule in LeadRoutingRule.objects.filter(is_active=True).order_by("priority", "created_at"):
        if _criteria_match(rule.criteria or {}, lead):
            return rule
    return None


def _criteria_match(criteria, lead):
    for key, expected in criteria.items():
        if key == "lead_type" and lead.lead_type != expected:
            return False
        if key == "source_type":
            actual = lead.source.source_type if lead.source_id else None
            if actual != expected:
                return False
        if key == "source_id" and str(lead.source_id) != str(expected):
            return False
        if key == "campaign_id" and str(lead.campaign_id) != str(expected):
            return False
        if key == "min_score" and lead.score < int(expected):
            return False
        if key == "max_score" and lead.score > int(expected):
            return False
        if key == "city":
            locations = [lead.preferred_location or ""] + [
                p.location for p in lead.location_preferences.all()
            ]
            if not any(str(expected).lower() in loc.lower() for loc in locations if loc):
                return False
    return True


def least_loaded_agent(candidate_ids):
    """The candidate holding the fewest open leads — round-robin that respects real workload.

    **Why the candidate ids are resolved first.** The obvious query annotates a `User` queryset
    filtered by role with `Count("assigned_leads")`. But roles live in a through-table, so
    filtering on `user_roles__role__code` joins it — and an agent holding two roles then
    appears twice, doubling their counted load and permanently protecting them from
    assignment. Resolving the id set first, then annotating over a plain `pk__in`, is what the
    previous implementation got wrong.
    """
    from apps.identity.models import User

    if not candidate_ids:
        return None
    open_statuses = (Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED,
                     Lead.Status.NURTURING)
    ranked = (
        User.objects.filter(pk__in=list(candidate_ids), is_active=True, deleted_at__isnull=True)
        .annotate(
            open_leads=Count(
                "assigned_leads",
                filter=Q(
                    assigned_leads__status__in=open_statuses,
                    assigned_leads__deleted_at__isnull=True,
                ),
            )
        )
        .order_by("open_leads", "created_at")
    )
    return ranked.first()


def _team_member_ids(team):
    from apps.identity.models import User

    return list(
        User.objects.filter(
            team=team, is_active=True, deleted_at__isnull=True
        ).values_list("pk", flat=True)
    )


@transaction.atomic
def assign_lead(lead, *, to_user=None, to_team=None, actor, reason=None):
    """Hand a lead to a person or a team pool, recording the handover.

    Assigning to a team leaves `assigned_agent` null on purpose: the lead sits in the pool,
    visible to every member under OWN scope (§2's second anchor), until someone claims it.
    """
    if to_user is None and to_team is None:
        raise ValidationError("Assign a lead to a user or to a team.")

    previous = lead.assigned_agent
    lead.assigned_agent = to_user
    lead.assigned_team = to_team if to_user is None else (to_team or lead.assigned_team)
    lead.save(update_fields=["assigned_agent", "assigned_team", "updated_at"])

    if to_user is not None:
        LeadAssignment.objects.create(
            lead=lead, from_user=previous, to_user=to_user, assigned_by=actor, reason=reason
        )
        if to_user.pk != actor.pk:
            # §1.2's notify orchestration. never raises, so the assignment cannot fail over
            # a notification.
            from apps.collaboration import services as collaboration_services

            collaboration_services.notify(
                recipient=to_user,
                type="LEAD_ASSIGNED",
                title=f"Lead assigned: {lead.title}",
                body=reason,
                entity_type="LEAD",
                entity_id=lead.pk,
                actor=actor,
            )
    signals.lead_assigned.send_robust(
        sender=None, lead=lead, actor=actor, from_user=previous, to_user=to_user, reason=reason
    )
    return lead


def route_lead(lead, *, actor):
    """Apply the first matching routing rule (SRS 3.1.5). Returns the rule, or None."""
    rule = matching_rule(lead)
    if rule is None:
        return None

    if rule.assign_to_user_id:
        assign_lead(lead, to_user=rule.assign_to_user, actor=actor, reason=f"Rule: {rule.name}")
    elif rule.round_robin:
        chosen = least_loaded_agent(_team_member_ids(rule.assign_to_team))
        if chosen:
            assign_lead(
                lead,
                to_user=chosen,
                to_team=rule.assign_to_team,
                actor=actor,
                reason=f"Rule: {rule.name} (round-robin)",
            )
        else:
            # An empty or fully inactive team still has to hold the lead: dropping it on the
            # floor because nobody is available is worse than a visible pool nobody has
            # claimed.
            assign_lead(lead, to_team=rule.assign_to_team, actor=actor, reason=f"Rule: {rule.name}")
    else:
        assign_lead(lead, to_team=rule.assign_to_team, actor=actor, reason=f"Rule: {rule.name}")
    return rule


# --- Capture (SRS 3.1.1–3.1.12) ------------------------------------------------------------


@transaction.atomic
def capture_lead(
    *,
    actor,
    contact=None,
    contact_data=None,
    locations=(),
    acknowledge=True,
    route=True,
    **fields,
):
    """The one way a lead enters the system.

    Web form, portal feed, walk-in, phone, manual entry and CSV all arrive here, because every
    one of SRS 3.1's guarantees — de-duplication, scoring, routing, the SLA clock, the instant
    acknowledgment — has to hold for all of them. A second creation path is a set of
    requirements that silently apply to some leads and not others.

    Order matters: the contact is resolved first (scoring reads their phone), the score is
    computed before routing (rules match on `min_score`), and the SLA clock starts only once
    the lead is actually assigned to someone who could answer it.
    """
    if contact is None:
        if not contact_data:
            raise ValidationError({"contact": "A lead needs a contact or contact details."})
        # Cross-app call, not Contact.objects.create: normalisation, de-duplication and the
        # role set all live behind this service (§1.2).
        contact, _ = contacts_services.find_or_create_contact(
            actor=actor, roles=_roles_for(fields.get("lead_type")), **contact_data
        )

    if "custom_data" in fields:
        # SRS 3.17.4 — validated against the core registry; a no-op while it is empty.
        from apps.core.services import validate_custom_data

        validate_custom_data("LEAD", fields["custom_data"], partial=False)
    lead = Lead(contact=contact, created_by=actor, updated_by=actor, **fields)
    lead.is_possible_duplicate = is_possible_duplicate(
        contact, lead.lead_type, lead.target_property
    )
    lead.save()

    for entry in locations:
        LeadLocationPreference.objects.create(lead=lead, **entry)

    lead.score = score_lead(lead)
    lead.save(update_fields=["score", "updated_at"])

    rule = route_lead(lead, actor=actor) if route else None

    _start_sla_clock(lead)
    if actor is not None:
        # `changed_by` is NOT NULL by design — history rows name a person. An anonymous
        # public capture has no person; the CREATE audit event (actor null) is its record.
        LeadStatusHistory.objects.create(
            lead=lead, from_status=None, to_status=lead.status, changed_by=actor,
            reason="Captured.",
        )
    if acknowledge:
        send_acknowledgment(lead)

    if lead.campaign_id:
        # SRS 3.8.5 — campaign attribution counts at the moment of capture.
        record_campaign_metric(
            lead.campaign, timezone.localdate(), leads_generated=1
        )

    signals.lead_captured.send_robust(
        sender=None, lead=lead, actor=actor, assigned_to=lead.assigned_agent, rule=rule
    )
    lead.refresh_from_db()
    return lead


def _roles_for(lead_type):
    """The contact role a lead of this type implies (SRS 3.2.2)."""
    return {
        Lead.LeadType.BUY: ["BUYER"],
        Lead.LeadType.SELL: ["SELLER"],
        Lead.LeadType.RENT_IN: ["TENANT"],
        Lead.LeadType.RENT_OUT: ["LANDLORD"],
        Lead.LeadType.INVEST: ["INVESTOR"],
    }.get(lead_type, [])


def _start_sla_clock(lead):
    """SRS 3.1.9 — "flag and alert on leads with no follow-up activity within a configurable
    SLA window". Configurable is the operative word: it varies by market and by team."""
    minutes = getattr(settings, "LEAD_SLA_MINUTES", 60)
    lead.sla_due_at = timezone.now() + timezone.timedelta(minutes=minutes)
    lead.sla_breached = False
    lead.save(update_fields=["sla_due_at", "sla_breached", "updated_at"])


def send_acknowledgment(lead):
    """SRS 3.1.12 — "send an instant, automated acknowledgment ... immediately upon capture".

    Sent inline through `django.core.mail`, the same path password reset already uses. No
    Celery: Render's free tier blocks background workers, and a queued acknowledgment that
    never runs is worse than a synchronous one that costs a few hundred milliseconds.

    Never raises. A mail server being down must not lose the lead — `acknowledged` stays false
    so a later sweep can retry without double-sending.
    """
    email = lead.contact.email if lead.contact_id else None
    if not email or lead.acknowledged:
        return False
    try:
        send_mail(
            subject="We have received your enquiry",
            message=(
                f"Thank you for your enquiry about {lead.title}.\n\n"
                "One of our agents will be in touch shortly."
            ),
            from_email=None,
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Lead acknowledgment failed for lead=%s", lead.pk)
        return False
    lead.acknowledged = True
    lead.save(update_fields=["acknowledged", "updated_at"])
    return True


@transaction.atomic
def record_first_response(lead, *, actor, note=None):
    """Stop the SLA clock (SRS 3.1.9).

    Only the *first* response counts — the requirement is about time-to-first-contact, so a
    later call must not reset the measurement and hide an original breach.
    """
    if lead.first_response_at is not None:
        return lead
    lead.first_response_at = timezone.now()
    lead.last_contacted_at = lead.first_response_at
    fields = ["first_response_at", "last_contacted_at", "updated_at"]
    if lead.status == Lead.Status.NEW:
        lead.status = Lead.Status.CONTACTED
        fields.append("status")
        LeadStatusHistory.objects.create(
            lead=lead, from_status=Lead.Status.NEW, to_status=Lead.Status.CONTACTED,
            changed_by=actor, reason=note or "First response.",
        )
    lead.save(update_fields=fields)
    return lead


def sweep_sla(now=None):
    """Mark every overdue, unanswered, still-open lead as breached (SRS 3.1.9).

    A management command an external scheduler calls, not a Celery beat task: Render's free
    tier blocks background workers. `crm_lead_sla_sweep_idx` on (sla_breached, sla_due_at) is
    the access path.

    Idempotent — already-breached leads are excluded — so running it twice, or a scheduler
    that retries, costs nothing.
    """
    now = now or timezone.now()
    open_statuses = (Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED,
                     Lead.Status.NURTURING)
    overdue = Lead.objects.filter(
        deleted_at__isnull=True,
        sla_breached=False,
        sla_due_at__lt=now,
        first_response_at__isnull=True,
        status__in=open_statuses,
    )
    ids = list(overdue.values_list("pk", flat=True))
    if ids:
        Lead.objects.filter(pk__in=ids).update(sla_breached=True, updated_at=now)
    return ids


# --- Status and conversion (SRS 3.1.4, 3.1.8) ----------------------------------------------

LEAD_TRANSITIONS = {
    Lead.Status.NEW: {Lead.Status.CONTACTED, Lead.Status.QUALIFIED, Lead.Status.LOST},
    Lead.Status.CONTACTED: {
        Lead.Status.QUALIFIED, Lead.Status.NURTURING, Lead.Status.LOST
    },
    Lead.Status.QUALIFIED: {
        Lead.Status.NURTURING, Lead.Status.CONVERTED, Lead.Status.LOST
    },
    Lead.Status.NURTURING: {
        Lead.Status.QUALIFIED, Lead.Status.CONVERTED, Lead.Status.LOST
    },
    # CONVERTED is terminal: the deal is the record now, and moving the lead afterwards would
    # leave two places claiming to say where the opportunity stands.
    Lead.Status.CONVERTED: set(),
    Lead.Status.LOST: {Lead.Status.NEW},
}


@transaction.atomic
def change_lead_status(lead, new_status, *, actor, reason=None):
    if new_status not in LEAD_TRANSITIONS:
        raise ValidationError({"status": f"Unknown lead status {new_status!r}."})
    current = lead.status
    if current == new_status:
        return lead
    allowed = LEAD_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValidationError(
            {
                "status": (
                    f"Cannot move a lead from {current} to {new_status}."
                    + (f" Allowed: {sorted(allowed)}." if allowed else f" {current} is terminal.")
                )
            }
        )
    if new_status == Lead.Status.LOST and not reason:
        raise ValidationError({"reason": "A lost lead needs a reason."})

    lead.status = new_status
    fields = ["status", "updated_by", "updated_at"]
    if new_status == Lead.Status.LOST:
        lead.lost_reason = reason
        fields.append("lost_reason")
    elif lead.lost_reason:
        # Reopening a lost lead must clear the old reason, or every report downstream still
        # shows why it was lost while it is visibly open.
        lead.lost_reason = None
        fields.append("lost_reason")
    lead.updated_by = actor
    lead.save(update_fields=fields)
    LeadStatusHistory.objects.create(
        lead=lead, from_status=current, to_status=new_status, changed_by=actor, reason=reason
    )
    return lead


LEAD_WRITABLE = frozenset(
    {
        "title",
        "description",
        "priority",
        "budget_min",
        "budget_max",
        "preferred_property_type",
        "target_property",
        "preferred_location",
        "preferred_bedrooms",
        "preferred_bathrooms",
        "financing_status",
        "expected_timeframe",
        "next_follow_up_at",
        "custom_data",
    }
)


@transaction.atomic
def update_lead(lead, *, actor, **fields):
    """Patch a lead's descriptive fields.

    `status` and the assignment are deliberately absent: both have their own service
    (`change_lead_status`, `assign_lead`) because both write a history row, and a patch that
    could set them would bypass the trail. Re-scores afterwards, since budget, location and
    timeframe all feed the score (SRS 3.1.6) — a lead edited to add a budget that kept its
    original score would be ranked on stale information.
    """
    unknown = set(fields) - LEAD_WRITABLE
    if unknown:
        raise ValidationError(
            {name: "This field cannot be set through update_lead()." for name in unknown}
        )
    if "custom_data" in fields:
        from apps.core.services import validate_custom_data

        validate_custom_data("LEAD", fields["custom_data"], partial=True)
    for name, value in fields.items():
        setattr(lead, name, value)
    lead.updated_by = actor
    lead.save()

    rescored = score_lead(lead)
    if rescored != lead.score:
        lead.score = rescored
        lead.save(update_fields=["score", "updated_at"])
    return lead


@transaction.atomic
def convert_lead(lead, *, actor, pipeline, stage=None, owner=None, **deal_fields):
    """Turn a qualified lead into a deal (SRS 3.1.8).

    Creates the deal **inside crm only** — no cross-app orchestration. The commission, the
    transaction and the lease that a *won* deal produces belong to the apps that own them and
    arrive with those passes.

    Idempotent: a lead already converted returns its existing deal rather than creating a
    second one. A double-clicked "Convert" button is the normal way that happens.
    """
    from apps.core.services import next_reference

    if lead.status == Lead.Status.CONVERTED:
        existing = lead.deals.filter(deleted_at__isnull=True).first()
        if existing:
            return existing

    stage = stage or pipeline.stages.order_by("sort_order").first()
    if stage is None:
        raise ValidationError({"stage": "That pipeline has no stages."})
    # The old implementation took the stage on trust. A stage from another pipeline puts the
    # deal on a board it will never appear on.
    if stage.pipeline_id != pipeline.pk:
        raise ValidationError({"stage": "That stage belongs to a different pipeline."})

    deal = Deal.objects.create(
        reference_code=next_reference("deal", prefix="DL"),
        lead=lead,
        primary_contact=lead.contact,
        pipeline=pipeline,
        stage=stage,
        owner=owner or lead.assigned_agent or actor,
        title=deal_fields.pop("title", lead.title),
        deal_type=deal_fields.pop("deal_type", lead.lead_type),
        estimated_value=deal_fields.pop("estimated_value", lead.budget_max or lead.budget_min or 0),
        currency=deal_fields.pop("currency", "AED"),
        probability=deal_fields.pop("probability", stage.probability),
        stage_entered_at=timezone.now(),
        created_by=actor,
        updated_by=actor,
        **deal_fields,
    )
    DealStageHistory.objects.create(
        deal=deal, from_stage=None, to_stage=stage, changed_by=actor,
        reason="Converted from lead.",
    )
    if lead.target_property_id:
        from .models import DealProperty

        DealProperty.objects.create(
            deal=deal, property=lead.target_property, is_primary=True
        )

    # The status the lead actually held. Hardcoding QUALIFIED here made the trail claim a
    # transition that never happened for any lead converted straight from NEW or CONTACTED —
    # a status history that invents states is worse than none, because it is believed.
    previous_status = lead.status
    lead.status = Lead.Status.CONVERTED
    lead.converted_at = timezone.now()
    lead.updated_by = actor
    lead.save(update_fields=["status", "converted_at", "updated_by", "updated_at"])
    LeadStatusHistory.objects.create(
        lead=lead, from_status=previous_status, to_status=Lead.Status.CONVERTED,
        changed_by=actor, reason=f"Converted to deal {deal.reference_code}.",
    )
    signals.lead_converted.send_robust(sender=None, lead=lead, actor=actor, deal=deal)
    return deal


# --- Pipeline and deals (SRS §3.4) ----------------------------------------------------------


@transaction.atomic
def create_deal(*, actor, pipeline, stage=None, **fields):
    from apps.core.services import next_reference

    stage = stage or pipeline.stages.order_by("sort_order").first()
    if stage is None:
        raise ValidationError({"stage": "That pipeline has no stages."})
    if "custom_data" in fields:
        # SRS 3.17.4 — validated against the core registry; a no-op while it is empty.
        from apps.core.services import validate_custom_data

        validate_custom_data("DEAL", fields["custom_data"], partial=False)
    if stage.pipeline_id != pipeline.pk:
        raise ValidationError({"stage": "That stage belongs to a different pipeline."})

    deal = Deal.objects.create(
        reference_code=fields.pop("reference_code", None) or next_reference("deal", prefix="DL"),
        pipeline=pipeline,
        stage=stage,
        probability=fields.pop("probability", stage.probability),
        stage_entered_at=timezone.now(),
        created_by=actor,
        updated_by=actor,
        **fields,
    )
    DealStageHistory.objects.create(
        deal=deal, from_stage=None, to_stage=stage, changed_by=actor, reason="Created."
    )
    return deal


@transaction.atomic
def move_stage(deal, stage, *, actor, reason, next_action=None):
    """Move a deal to another stage of its pipeline (SRS 3.4.4).

    > "The System shall require users to log a mandatory reason and next action when a deal
    >  moves stage or is marked Lost."

    Mandatory means mandatory: a blank or whitespace-only reason is refused here, and the
    CHECK constraint on `crm_deal_stage_history.reason` refuses it again at the database, so
    an import or a future endpoint cannot get past it either.
    """
    if not (reason or "").strip():
        raise ValidationError({"reason": "A stage move needs a reason (SRS 3.4.4)."})
    if stage.pipeline_id != deal.pipeline_id:
        raise ValidationError({"stage": "That stage belongs to a different pipeline."})
    # SRS 3.4.4 asks for a reason *and a next action*. Required on every move except onto a
    # terminal stage, where there is nothing next by definition — demanding one there would
    # train users to type "n/a", which is how a mandatory field stops meaning anything.
    if not stage.is_won and not stage.is_lost and not (next_action or "").strip():
        raise ValidationError(
            {"next_action": "Say what happens next (SRS 3.4.4)."}
        )

    previous = deal.stage
    if previous.pk == stage.pk:
        return deal

    deal.stage = stage
    # The stage's probability is the pipeline's own forecast for anything sitting there, so it
    # follows the move rather than being re-typed.
    deal.probability = stage.probability
    deal.stage_entered_at = timezone.now()
    # Keep the standing next action when a terminal move supplies none, rather than silently
    # blanking whatever the last open stage set.
    terminal = stage.is_won or stage.is_lost
    deal.next_action = next_action or (None if terminal else deal.next_action)
    fields = [
        "stage", "probability", "stage_entered_at", "next_action", "updated_by", "updated_at",
    ]

    if stage.is_won:
        deal.status = Deal.Status.WON
        deal.actual_close_date = timezone.now().date()
        fields += ["status", "actual_close_date"]
    elif stage.is_lost:
        deal.status = Deal.Status.LOST
        deal.lost_reason = reason
        deal.actual_close_date = timezone.now().date()
        fields += ["status", "lost_reason", "actual_close_date"]
    else:
        deal.status = Deal.Status.OPEN
        fields.append("status")
        if previous.is_lost or deal.lost_reason:
            # Moving *out* of a lost stage must clear the reason, or the deal reads as open
            # while every report still shows why it was lost. The old implementation left it.
            deal.lost_reason = None
            deal.actual_close_date = None
            fields += ["lost_reason", "actual_close_date"]

    deal.updated_by = actor
    deal.save(update_fields=fields)
    DealStageHistory.objects.create(
        deal=deal, from_stage=previous, to_stage=stage, changed_by=actor,
        reason=reason, next_action=next_action,
    )
    signals.deal_stage_moved.send_robust(
        sender=None, deal=deal, actor=actor, from_stage=previous, to_stage=stage,
        reason=reason,
    )
    if stage.is_won:
        # §1.2: "Sale deal won: crm.services.mark_deal_won → finance". Same transaction as
        # the stage save; idempotent and self-degrading, so re-winning a churned deal or a
        # missing commission plan can never fail the move itself.
        mark_deal_won(deal, actor=actor)
    if (next_action or "").strip() and not terminal:
        # SRS 3.4.5 — the mandatory next action becomes a real task on the owner's list,
        # not a string that scrolls away in the stage history.
        from apps.collaboration import services as collaboration_services

        collaboration_services.create_task_for_stage_move(
            deal=deal, subject=next_action, actor=actor
        )
    return deal


@transaction.atomic
def update_deal(deal, *, actor, **fields):
    """Patch a deal. `stage` and `status` are not settable here — they go through
    `move_stage`, which is what guarantees the mandatory reason and the history row."""
    for forbidden in ("stage", "status", "reference_code", "lost_reason"):
        if forbidden in fields:
            raise ValidationError(
                {forbidden: "Use move_stage() — a stage change needs a reason (SRS 3.4.4)."}
            )
    if "custom_data" in fields:
        from apps.core.services import validate_custom_data

        validate_custom_data("DEAL", fields["custom_data"], partial=True)
    for name, value in fields.items():
        setattr(deal, name, value)
    deal.updated_by = actor
    deal.save()
    return deal


@transaction.atomic
def mark_deal_won(deal, *, actor, gross_amount=None, commission_plan=None):
    """§1.2's orchestration keystone: a won deal becomes a Transaction, and for a sale, a
    commission. Called from `move_stage`'s is_won branch in the SAME transaction — never a
    post_save signal, which §1.2 forbids for money.

    Three properties make it safe to fire on every stage churn through a won stage:
    * **Idempotent** — a deal with a live transaction returns it untouched (a deal dragged
      out of Won and back in must not mint a second sale).
    * **Degrading** — commission-plan ambiguity logs and defers rather than failing; a stage
      move must never break over back-office configuration (create the commission later via
      POST /commissions with an explicit plan).
    * **Refusing quietly** — a deal with no linked property returns None with a warning:
      `crm_transaction.property` is NOT NULL, and blocking the move over a missing link
      would hold a sale hostage to data entry. The gap is visible in the audit trail.

    For LETTING deals this creates the RENTAL transaction only; the lease follows via the
    explicit `POST /deals/{id}/create-lease/` — lease terms are a property manager's
    decision, not something to derive from a deal's estimate.
    """
    from apps.core.services import next_reference

    from .models import DealProperty, Transaction

    existing = deal.transactions.exclude(status=Transaction.Status.CANCELLED).first()
    if existing is not None:
        return existing

    primary = (
        DealProperty.objects.filter(deal=deal)
        .order_by("-is_primary", "created_at")
        .select_related("property")
        .first()
    )
    if primary is None:
        logger.warning(
            "mark_deal_won: deal %s has no linked property; transaction deferred.", deal.pk
        )
        return None

    rental = str(deal.deal_type).upper() in ("RENT", "RENTAL", "RENT_IN", "RENT_OUT",
                                             "LEASE", "LEASING")
    accepted_offer = (
        deal.offers.filter(status="ACCEPTED").order_by("-responded_at").first()
        if hasattr(deal, "offers")
        else None
    )
    amount = (
        gross_amount
        if gross_amount is not None
        else (accepted_offer.amount if accepted_offer else deal.estimated_value)
    )

    txn = Transaction.objects.create(
        deal=deal,
        property=primary.property,
        transaction_type=(
            Transaction.TransactionType.RENTAL if rental
            else Transaction.TransactionType.SALE
        ),
        reference_code=next_reference("transaction", prefix="TXN"),
        gross_amount=amount,
        currency=deal.currency,
        transaction_date=timezone.localdate(),
        status=Transaction.Status.PENDING,
        created_by=actor,
        updated_by=actor,
    )

    if not rental:
        from apps.finance import services as finance_services

        try:
            finance_services.create_commission_for_transaction(
                actor=actor, transaction_obj=txn, plan=commission_plan
            )
        except ValidationError as exc:
            logger.warning(
                "mark_deal_won: commission deferred for deal %s: %s", deal.pk, exc
            )
    return txn


@transaction.atomic
def link_property(deal, *, property, unit=None, is_primary=False, actor=None):
    """Attach a property to an opportunity (SRS 3.4.7).

    > "linking one or more properties to an opportunity, and one opportunity to a specific
    >  matched property once identified"
    """
    from .models import DealProperty

    if unit and unit.property_id != property.pk:
        raise ValidationError({"unit": "That unit belongs to a different property."})
    link, created = DealProperty.objects.get_or_create(
        deal=deal, property=property, unit=unit
    )
    if is_primary:
        DealProperty.objects.filter(deal=deal, is_primary=True).exclude(pk=link.pk).update(
            is_primary=False
        )
        link.is_primary = True
        link.save(update_fields=["is_primary"])
    return link


@transaction.atomic
def unlink_property(link, *, actor=None):
    link.delete()


# --- Viewings (SRS 3.3.11, 3.16.4) ------------------------------------------------------------


VIEWING_TRANSITIONS = {
    "SCHEDULED": {"CONFIRMED", "COMPLETED", "CANCELLED", "NO_SHOW"},
    "CONFIRMED": {"COMPLETED", "CANCELLED", "NO_SHOW"},
    "COMPLETED": set(),
    "CANCELLED": set(),
    "NO_SHOW": set(),
}


@transaction.atomic
def schedule_viewing(*, actor, property, contact, agent, scheduled_start, scheduled_end, **fields):
    """Book a viewing and put it on the calendar (SRS 3.3.11).

    The calendar entry is **not optional**. architecture.md §1.2 lists this as a required
    orchestration and §13 forbids a standalone VIEWING activity, so the activity is created
    here through `collaboration.services.upsert_activity_for_source` — one function, so a
    reschedule updates the entry rather than adding a second one, and so the same rule holds
    for inspections in a later pass without being re-implemented.
    """
    from apps.collaboration import services as collaboration_services

    from .models import Viewing

    if scheduled_end <= scheduled_start:
        raise ValidationError({"scheduled_end": "A viewing must end after it starts."})

    viewing = Viewing.objects.create(
        property=property,
        contact=contact,
        agent=agent,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        created_by=actor,
        updated_by=actor,
        **fields,
    )
    _sync_viewing_activity(viewing, actor=actor)
    if agent.pk != actor.pk:
        collaboration_services.notify(
            recipient=agent,
            type="VIEWING_SCHEDULED",
            title=f"Viewing booked: {property.title}",
            body=f"{scheduled_start:%Y-%m-%d %H:%M} with {contact}",
            entity_type="VIEWING",
            entity_id=viewing.pk,
            actor=actor,
        )
    return viewing


@transaction.atomic
def reschedule_viewing(viewing, *, actor, scheduled_start, scheduled_end, location=None):
    if scheduled_end <= scheduled_start:
        raise ValidationError({"scheduled_end": "A viewing must end after it starts."})
    if viewing.status in ("COMPLETED", "CANCELLED", "NO_SHOW"):
        raise ValidationError({"status": f"A {viewing.status.lower()} viewing cannot move."})

    viewing.scheduled_start = scheduled_start
    viewing.scheduled_end = scheduled_end
    fields = ["scheduled_start", "scheduled_end", "updated_by", "updated_at"]
    if location is not None:
        viewing.location = location
        fields.append("location")
    viewing.updated_by = actor
    viewing.save(update_fields=fields)
    _sync_viewing_activity(viewing, actor=actor)
    return viewing


def _sync_viewing_activity(viewing, *, actor):
    """Mirror the viewing onto the unified calendar. Upsert, never insert."""
    from apps.collaboration import services as collaboration_services
    from apps.collaboration.models import Activity

    collaboration_services.upsert_activity_for_source(
        source_type=Activity.SourceType.VIEWING,
        source_id=viewing.pk,
        activity_type=Activity.ActivityType.VIEWING,
        subject=f"Viewing: {viewing.property.title}",
        assigned_to=viewing.agent,
        actor=actor,
        start_at=viewing.scheduled_start,
        due_at=viewing.scheduled_end,
        contact=viewing.contact,
        lead=viewing.lead,
        deal=viewing.deal,
        property=viewing.property,
    )


@transaction.atomic
def complete_viewing(viewing, *, actor, feedback=None, rating=None, status=None):
    """Close a viewing out with the client's feedback (SRS 3.3.11).

    Feedback is the point of the requirement — "record viewing feedback and ratings" — so it
    is captured on the same call that closes the appointment rather than left to a separate
    step nobody takes.
    """
    from apps.collaboration import services as collaboration_services
    from apps.collaboration.models import Activity

    from .models import Viewing

    status = status or Viewing.Status.COMPLETED
    allowed = VIEWING_TRANSITIONS.get(viewing.status, set())
    if status not in allowed:
        raise ValidationError(
            {
                "status": (
                    f"Cannot move a viewing from {viewing.status} to {status}."
                    + (f" Allowed: {sorted(allowed)}." if allowed else " It is already closed.")
                )
            }
        )
    if rating is not None and not (1 <= int(rating) <= 5):
        raise ValidationError({"rating": "A rating is 1 to 5."})

    viewing.status = status
    fields = ["status", "updated_by", "updated_at"]
    if feedback is not None:
        viewing.feedback = feedback
        fields.append("feedback")
    if rating is not None:
        viewing.rating = rating
        fields.append("rating")
    viewing.updated_by = actor
    viewing.save(update_fields=fields)

    collaboration_services.close_activity_for_source(
        source_type=Activity.SourceType.VIEWING,
        source_id=viewing.pk,
        status=(
            Activity.Status.COMPLETED
            if status == Viewing.Status.COMPLETED
            else Activity.Status.CANCELLED
        ),
        completed_at=timezone.now(),
    )
    return viewing


@transaction.atomic
def check_in_to_viewing(viewing, *, actor, latitude=None, longitude=None):
    """Stamp the agent's arrival, with coordinates when the device offered them (SRS 3.16.4)."""
    if viewing.agent_id != actor.pk:
        raise ValidationError("Only the agent running a viewing can check in to it.")
    if viewing.check_in_at:
        return viewing
    viewing.check_in_at = timezone.now()
    viewing.check_in_latitude = latitude
    viewing.check_in_longitude = longitude
    viewing.save(
        update_fields=[
            "check_in_at", "check_in_latitude", "check_in_longitude", "updated_at",
        ]
    )
    return viewing


# --- GPS field tracking (SRS 3.16.5–3.16.7) -----------------------------------------------------


@transaction.atomic
def start_field_session(*, actor, agent=None, gps_enabled=False, **fields):
    """Open a GPS-tracked field trip (SRS 3.16.5).

    > "The System shall require sales officers to enable GPS tracking during field visits."

    Refused when `gps_required` is set and the device has not enabled GPS. That is the
    requirement's whole force: a session that opens without it is an untracked field visit
    wearing a tracked visit's name, which is worse than no session at all — the record implies
    a trail that does not exist.

    An agent may only open a session for themselves; supervisors read trails, they do not
    manufacture them.
    """
    from .models import AgentFieldSession

    agent = agent or actor
    if agent.pk != actor.pk:
        raise ValidationError("A field session is opened by the agent making the visit.")

    gps_required = fields.pop("gps_required", True)
    if gps_required and not gps_enabled:
        raise ValidationError(
            {"gps_enabled": "Enable GPS before starting a field visit (SRS 3.16.5)."}
        )
    if AgentFieldSession.objects.filter(
        agent=agent, status=AgentFieldSession.Status.ACTIVE
    ).exists():
        raise ValidationError("That agent already has an open field session.")

    return AgentFieldSession.objects.create(
        agent=agent,
        started_at=fields.pop("started_at", None) or timezone.now(),
        gps_required=gps_required,
        gps_enabled=gps_enabled,
        created_by=actor,
        updated_by=actor,
        **fields,
    )


@transaction.atomic
def end_field_session(session, *, actor, notes=None):
    from .models import AgentFieldSession

    if session.status != AgentFieldSession.Status.ACTIVE:
        return session
    session.status = AgentFieldSession.Status.COMPLETED
    session.ended_at = timezone.now()
    fields = ["status", "ended_at", "updated_by", "updated_at"]
    if notes is not None:
        session.notes = notes
        fields.append("notes")
    session.updated_by = actor
    session.save(update_fields=fields)
    return session


@transaction.atomic
def record_location_points(session, *, actor, points):
    """Append breadcrumbs to a session's trail (SRS 3.16.6).

    Append-only at the database-role level: UPDATE and DELETE on `crm_agent_location_point`
    are revoked from the application role, so a recorded trail cannot be quietly rewritten.
    That is a guarantee about the *employee's* record as much as the company's.

    Only the tracked agent may append. A supervisor writing points into someone else's trail
    would make the trail evidence of nothing.
    """
    from django.contrib.gis.geos import Point

    from .models import AgentFieldSession, AgentLocationPoint

    if session.agent_id != actor.pk:
        raise ValidationError("Only the tracked agent can add points to their own trail.")
    if session.status != AgentFieldSession.Status.ACTIVE:
        raise ValidationError("That field session is closed.")

    rows = []
    for point in points:
        latitude = point["latitude"]
        longitude = point["longitude"]
        rows.append(
            AgentLocationPoint(
                session=session,
                recorded_at=point.get("recorded_at") or timezone.now(),
                latitude=latitude,
                longitude=longitude,
                accuracy_m=point.get("accuracy_m"),
                geo_point=Point(float(longitude), float(latitude), srid=4326),
                created_by=actor,
            )
        )
    return AgentLocationPoint.objects.bulk_create(rows)


def read_location_trail(session, *, actor):
    """The session's breadcrumbs, recording that they were read (SRS 3.16.7).

    > "Access to GPS tracks shall be role-restricted and audited."

    Row scoping is the restriction; this is the audit, and without it the requirement is half
    met. An agent reading their own trail is not audited — the control exists to make
    *supervisory* access to an employee's location visible, and logging self-reads would bury
    the ones that matter.
    """
    points = list(session.location_points.order_by("recorded_at"))
    if session.agent_id != actor.pk:
        signals.gps_track_accessed.send_robust(
            sender=None, session=session, actor=actor, point_count=len(points)
        )
    return points


# --- Offers (SRS 3.6.1) ---------------------------------------------------------------------
#
# An offer is never edited into a new amount — that would erase the negotiation history.
# A wrong offer is withdrawn or countered. COUNTERED is terminal: the child supersedes it.

OFFER_TRANSITIONS = {
    Offer.Status.DRAFT: {Offer.Status.SUBMITTED, Offer.Status.WITHDRAWN},
    Offer.Status.SUBMITTED: {
        Offer.Status.COUNTERED, Offer.Status.ACCEPTED, Offer.Status.REJECTED,
        Offer.Status.EXPIRED, Offer.Status.WITHDRAWN,
    },
    Offer.Status.COUNTERED: set(),
    Offer.Status.ACCEPTED: set(),
    Offer.Status.REJECTED: set(),
    Offer.Status.EXPIRED: set(),
    Offer.Status.WITHDRAWN: set(),
}

#: Statuses in which a negotiation is still live on the deal.
OPEN_OFFER_STATUSES = (Offer.Status.DRAFT, Offer.Status.SUBMITTED)


def _move_offer(offer, new_status):
    allowed = OFFER_TRANSITIONS.get(offer.status, set())
    if new_status not in allowed:
        raise ValidationError(
            {"status": f"Cannot move an offer from {offer.status} to {new_status}."}
        )
    offer.status = new_status


@transaction.atomic
def create_offer(*, actor, deal, amount, direction, property=None,
                 offered_by_contact=None, deposit_amount=None, conditions=None,
                 expires_at=None, submit=True):
    """A new offer on a deal. `submit=False` keeps it DRAFT while terms are typed up."""
    from .models import DealProperty

    if amount is None or Decimal(amount) <= 0:
        raise ValidationError({"amount": "An offer is a positive amount."})
    if property is None:
        primary = (
            DealProperty.objects.filter(deal=deal)
            .order_by("-is_primary", "created_at")
            .select_related("property")
            .first()
        )
        if primary is None:
            raise ValidationError(
                {"property": "Link a property to the deal before recording offers on it."}
            )
        property = primary.property
    offered_by_contact = offered_by_contact or deal.primary_contact
    if offered_by_contact is None:
        raise ValidationError({"offered_by_contact": "Who is making this offer?"})

    offer = Offer.objects.create(
        deal=deal,
        property=property,
        offered_by_contact=offered_by_contact,
        direction=direction,
        amount=amount,
        deposit_amount=deposit_amount,
        conditions=conditions or {},
        expires_at=expires_at,
        status=Offer.Status.SUBMITTED if submit else Offer.Status.DRAFT,
        submitted_at=timezone.now(),
    )
    return offer


@transaction.atomic
def submit_offer(offer, *, actor):
    _move_offer(offer, Offer.Status.SUBMITTED)
    offer.submitted_at = timezone.now()
    offer.save(update_fields=["status", "submitted_at"])
    return offer


@transaction.atomic
def withdraw_offer(offer, *, actor):
    _move_offer(offer, Offer.Status.WITHDRAWN)
    offer.responded_at = timezone.now()
    offer.save(update_fields=["status", "responded_at"])
    return offer


@transaction.atomic
def reject_offer(offer, *, actor):
    _move_offer(offer, Offer.Status.REJECTED)
    offer.responded_at = timezone.now()
    offer.save(update_fields=["status", "responded_at"])
    return offer


@transaction.atomic
def counter_offer(offer, *, actor, amount, deposit_amount=None, conditions=None,
                  expires_at=None, offered_by_contact=None):
    """Respond to a SUBMITTED offer with a counter: a NEW row chained by `parent_offer`,
    direction flipped, parent marked COUNTERED (terminal — the child supersedes it)."""
    _move_offer(offer, Offer.Status.COUNTERED)
    offer.responded_at = timezone.now()
    offer.save(update_fields=["status", "responded_at"])

    flipped = (
        Offer.Direction.SELLER_TO_BUYER
        if offer.direction == Offer.Direction.BUYER_TO_SELLER
        else Offer.Direction.BUYER_TO_SELLER
    )
    if amount is None or Decimal(amount) <= 0:
        raise ValidationError({"amount": "A counter is a positive amount."})
    return Offer.objects.create(
        deal=offer.deal,
        property=offer.property,
        parent_offer=offer,
        offered_by_contact=offered_by_contact or offer.offered_by_contact,
        direction=flipped,
        amount=amount,
        deposit_amount=deposit_amount,
        conditions=conditions or {},
        expires_at=expires_at,
        status=Offer.Status.SUBMITTED,
        submitted_at=timezone.now(),
    )


@transaction.atomic
def accept_offer(offer, *, actor):
    """Accept a SUBMITTED offer — and reject every other open offer on the deal. Two live
    offers after an acceptance would let a second acceptance contradict the first; chain
    ancestors are already terminal (COUNTERED).

    Acceptance does NOT move the deal stage (3.4.4's mandatory reason stays a human act)
    and does NOT create the Transaction — `mark_deal_won` owns that and reads the accepted
    offer for the gross amount.
    """
    _move_offer(offer, Offer.Status.ACCEPTED)
    offer.responded_at = timezone.now()
    offer.save(update_fields=["status", "responded_at"])
    now = timezone.now()
    Offer.objects.filter(
        deal=offer.deal, status__in=OPEN_OFFER_STATUSES
    ).exclude(pk=offer.pk).update(status=Offer.Status.REJECTED, responded_at=now)
    signals.offer_accepted.send_robust(sender=None, offer=offer, actor=actor)
    return offer


def sweep_offers(now=None):
    """Expire stale SUBMITTED offers. Status is the idempotency key."""
    now = now or timezone.now()
    expired = list(
        Offer.objects.filter(
            status=Offer.Status.SUBMITTED, expires_at__isnull=False, expires_at__lt=now
        ).values_list("pk", flat=True)
    )
    if expired:
        Offer.objects.filter(pk__in=expired).update(
            status=Offer.Status.EXPIRED, responded_at=now
        )
    return expired


# --- Transactions (SRS 3.6.6) ---------------------------------------------------------------

TRANSACTION_TRANSITIONS = {
    Transaction.Status.PENDING: {
        Transaction.Status.CONTRACTED, Transaction.Status.CANCELLED,
    },
    Transaction.Status.CONTRACTED: {
        Transaction.Status.PARTIALLY_PAID, Transaction.Status.COMPLETED,
        Transaction.Status.CANCELLED,
    },
    Transaction.Status.PARTIALLY_PAID: {
        Transaction.Status.COMPLETED, Transaction.Status.CANCELLED,
    },
    Transaction.Status.COMPLETED: set(),
    Transaction.Status.CANCELLED: set(),
}


def _incomplete_required_items(transaction_obj):
    from .models import ClosingChecklist, ClosingChecklistItem

    checklists = ClosingChecklist.objects.filter(
        Q(transaction=transaction_obj)
        | Q(deal__isnull=False, deal=transaction_obj.deal)
    ).exclude(status=ClosingChecklist.Status.CANCELLED)
    return ClosingChecklistItem.objects.filter(
        checklist__in=checklists, is_required=True, is_completed=False
    )


@transaction.atomic
def change_transaction_status(transaction_obj, new_status, *, actor, reason=None):
    """The transaction lifecycle. COMPLETED is gated (SRS 3.6.6): every required item on
    every attached non-cancelled closing checklist must be complete first. Cancellation
    requires a reason, appended to the notes so the record explains itself."""
    if new_status not in TRANSACTION_TRANSITIONS:
        raise ValidationError({"status": f"Unknown transaction status {new_status!r}."})
    allowed = TRANSACTION_TRANSITIONS.get(transaction_obj.status, set())
    if new_status not in allowed:
        raise ValidationError(
            {"status": (
                f"Cannot move a transaction from {transaction_obj.status} to {new_status}."
            )}
        )

    if new_status == Transaction.Status.COMPLETED:
        missing = _incomplete_required_items(transaction_obj)
        if missing.exists():
            titles = list(missing.values_list("title", flat=True)[:5])
            raise ValidationError(
                {"status": (
                    "Closing checklist items are still open: " + ", ".join(titles)
                )}
            )
    if new_status == Transaction.Status.CANCELLED and not (reason or "").strip():
        raise ValidationError({"reason": "Cancelling a transaction needs a reason."})

    previous = transaction_obj.status
    transaction_obj.status = new_status
    fields = ["status", "updated_by", "updated_at"]
    if new_status == Transaction.Status.CONTRACTED and not transaction_obj.contract_date:
        transaction_obj.contract_date = timezone.localdate()
        fields.append("contract_date")
    if new_status == Transaction.Status.COMPLETED and not transaction_obj.closing_date:
        transaction_obj.closing_date = timezone.localdate()
        fields.append("closing_date")
    if new_status == Transaction.Status.CANCELLED:
        stamp = f"[{timezone.localdate().isoformat()}] Cancelled: {reason.strip()}"
        transaction_obj.notes = (
            f"{transaction_obj.notes}\n{stamp}" if transaction_obj.notes else stamp
        )
        fields.append("notes")
    transaction_obj.updated_by = actor
    transaction_obj.save(update_fields=fields)
    signals.transaction_status_changed.send_robust(
        sender=None, transaction_obj=transaction_obj, actor=actor,
        from_status=previous, to_status=new_status,
    )
    return transaction_obj


# --- Closing checklists (SRS 3.6.6) ---------------------------------------------------------

_CHECKLIST_ITEM_FIELDS = frozenset(
    {"title", "description", "is_required", "due_date", "sort_order"}
)


@transaction.atomic
def create_checklist(*, actor, checklist_type, name, deal=None, transaction_obj=None,
                     items=()):
    from .models import ClosingChecklist, ClosingChecklistItem

    if deal is None and transaction_obj is None:
        raise ValidationError(
            {"deal": "A checklist attaches to a deal or a transaction."}
        )
    if checklist_type not in set(ClosingChecklist.ChecklistType.values):
        raise ValidationError({"checklist_type": "Unknown checklist type."})
    checklist = ClosingChecklist.objects.create(
        deal=deal, transaction=transaction_obj, checklist_type=checklist_type, name=name,
        created_by=actor, updated_by=actor,
    )
    for index, item in enumerate(items):
        unknown = set(item) - _CHECKLIST_ITEM_FIELDS
        if unknown:
            raise ValidationError(
                {"items": f"Unknown item fields: {sorted(unknown)}"}
            )
        ClosingChecklistItem.objects.create(
            checklist=checklist, sort_order=item.get("sort_order", index), **{
                k: v for k, v in item.items() if k != "sort_order"
            },
        )
    return checklist


@transaction.atomic
def add_checklist_item(checklist, *, actor, **fields):
    from .models import ClosingChecklist, ClosingChecklistItem

    if checklist.status != ClosingChecklist.Status.OPEN:
        raise ValidationError({"detail": "This checklist is closed."})
    unknown = set(fields) - _CHECKLIST_ITEM_FIELDS
    if unknown:
        raise ValidationError({name: "Not an item field." for name in unknown})
    if not (fields.get("title") or "").strip():
        raise ValidationError({"title": "An item needs a title."})
    if "sort_order" not in fields:
        fields["sort_order"] = checklist.items.count()
    return ClosingChecklistItem.objects.create(checklist=checklist, **fields)


@transaction.atomic
def complete_checklist_item(item, *, actor, document=None):
    """Tick one line, optionally attaching the document that satisfies it."""
    from .models import ClosingChecklist

    if item.checklist.status != ClosingChecklist.Status.OPEN:
        raise ValidationError({"detail": "This checklist is closed."})
    item.is_completed = True
    item.completed_by = actor
    item.completed_at = timezone.now()
    if document is not None:
        item.document = document
    item.save(update_fields=["is_completed", "completed_by", "completed_at", "document"])
    return item


@transaction.atomic
def reopen_checklist_item(item, *, actor):
    from .models import ClosingChecklist

    if item.checklist.status != ClosingChecklist.Status.OPEN:
        raise ValidationError({"detail": "This checklist is closed."})
    item.is_completed = False
    item.completed_by = None
    item.completed_at = None
    item.save(update_fields=["is_completed", "completed_by", "completed_at"])
    return item


@transaction.atomic
def complete_checklist(checklist, *, actor):
    from .models import ClosingChecklist

    if checklist.status != ClosingChecklist.Status.OPEN:
        raise ValidationError({"status": "Only an open checklist completes."})
    open_required = checklist.items.filter(is_required=True, is_completed=False)
    if open_required.exists():
        titles = list(open_required.values_list("title", flat=True)[:5])
        raise ValidationError(
            {"status": "Required items are still open: " + ", ".join(titles)}
        )
    checklist.status = ClosingChecklist.Status.COMPLETED
    checklist.updated_by = actor
    checklist.save(update_fields=["status", "updated_by", "updated_at"])
    return checklist


@transaction.atomic
def cancel_checklist(checklist, *, actor):
    from .models import ClosingChecklist

    if checklist.status != ClosingChecklist.Status.OPEN:
        raise ValidationError({"status": "Only an open checklist cancels."})
    checklist.status = ClosingChecklist.Status.CANCELLED
    checklist.updated_by = actor
    checklist.save(update_fields=["status", "updated_by", "updated_at"])
    return checklist


# --- Marketing: campaigns, drip, landing pages, alerts (SRS §3.8) ---------------------------

_CAMPAIGN_FIELDS = frozenset(
    {"name", "campaign_type", "description", "budget", "start_date", "end_date",
     "status", "owner"}
)
_STEP_FIELDS = frozenset(
    {"step_order", "channel", "delay_days", "template", "subject", "body", "is_active"}
)


@transaction.atomic
def create_campaign(*, actor, name, campaign_type, start_date, **fields):
    from .models import Campaign

    unknown = set(fields) - (_CAMPAIGN_FIELDS - {"name", "campaign_type", "start_date"})
    if unknown:
        raise ValidationError({name_: "Not a campaign field." for name_ in unknown})
    fields.setdefault("status", "DRAFT")
    fields.setdefault("owner", actor)
    return Campaign.objects.create(
        name=name, campaign_type=campaign_type, start_date=start_date, **fields
    )


@transaction.atomic
def update_campaign(campaign, *, actor, **fields):
    unknown = set(fields) - _CAMPAIGN_FIELDS
    if unknown:
        raise ValidationError({name: "Not a campaign field." for name in unknown})
    for name, value in fields.items():
        setattr(campaign, name, value)
    campaign.save()
    return campaign


@transaction.atomic
def add_campaign_step(campaign, *, actor, step_order, channel, **fields):
    from .models import CampaignStep

    unknown = set(fields) - (_STEP_FIELDS - {"step_order", "channel"})
    if unknown:
        raise ValidationError({name: "Not a step field." for name in unknown})
    template = fields.get("template")
    if template is not None and template.channel != channel:
        raise ValidationError({"template": "That template is for a different channel."})
    if not template and not (fields.get("body") or "").strip():
        raise ValidationError({"body": "A step sends a template or an inline body."})
    return CampaignStep.objects.create(
        campaign=campaign, step_order=step_order, channel=channel, **fields
    )


@transaction.atomic
def enroll_lead(campaign, lead, *, actor):
    """Put a lead on a drip. Re-enrolling an EXITED/COMPLETED lead reactivates from the
    top — a deliberate restart, not an error."""
    from .models import CampaignEnrollment

    first_step = campaign.steps.filter(is_active=True).order_by("step_order").first()
    if first_step is None:
        raise ValidationError({"campaign": "This campaign has no active steps."})
    if lead.status in (Lead.Status.CONVERTED, Lead.Status.LOST):
        raise ValidationError({"lead": "A closed lead does not enter a drip."})
    enrollment, created = CampaignEnrollment.objects.get_or_create(
        campaign=campaign, lead=lead,
        defaults={
            "status": CampaignEnrollment.Status.ACTIVE,
            "current_step": 0,
            "next_send_at": timezone.now() + timedelta(days=first_step.delay_days),
        },
    )
    if not created and enrollment.status != CampaignEnrollment.Status.ACTIVE:
        enrollment.status = CampaignEnrollment.Status.ACTIVE
        enrollment.current_step = 0
        enrollment.completed_at = None
        enrollment.next_send_at = timezone.now() + timedelta(days=first_step.delay_days)
        enrollment.save(
            update_fields=["status", "current_step", "completed_at", "next_send_at"]
        )
    return enrollment


@transaction.atomic
def exit_enrollment(enrollment, *, actor):
    from .models import CampaignEnrollment

    enrollment.status = CampaignEnrollment.Status.EXITED
    enrollment.next_send_at = None
    enrollment.save(update_fields=["status", "next_send_at"])
    return enrollment


def record_campaign_metric(campaign, metric_date, **deltas):
    """Day-unique upsert with F() deltas — a re-run adds nothing twice because callers
    pass deltas, and concurrent bumps do not lose updates."""
    from django.db.models import F

    from .models import CampaignMetric

    allowed = {"impressions", "clicks", "leads_generated", "conversions", "cost", "revenue"}
    unknown = set(deltas) - allowed
    if unknown:
        raise ValidationError({name: "Not a metric field." for name in unknown})
    row, created = CampaignMetric.objects.get_or_create(
        campaign=campaign, metric_date=metric_date, defaults=deltas
    )
    if not created:
        CampaignMetric.objects.filter(pk=row.pk).update(
            **{name: F(name) + value for name, value in deltas.items()}
        )
        row.refresh_from_db()
    return row


#: How long a failed drip send waits before the runner retries it.
DRIP_RETRY = timedelta(hours=1)


def run_drip(now=None):
    """Advance every due drip enrollment one step. The claim is `FOR UPDATE SKIP LOCKED`
    on the `(status, next_send_at)` index, so overlapping runs never double-send.

    A closed lead (CONVERTED/LOST) exits instead of receiving marketing. A send failure
    advances `next_send_at` by `DRIP_RETRY` without moving `current_step` — the step will
    be retried, and a poison enrollment cannot wedge the queue.
    """
    from apps.collaboration import services as collaboration_services

    from .models import CampaignEnrollment

    now = now or timezone.now()
    processed = []
    while True:
        with transaction.atomic():
            enrollment = (
                CampaignEnrollment.objects.select_for_update(skip_locked=True)
                .filter(
                    status=CampaignEnrollment.Status.ACTIVE,
                    next_send_at__isnull=False,
                    next_send_at__lte=now,
                )
                .select_related("campaign", "lead", "lead__contact")
                .order_by("next_send_at")
                .first()
            )
            if enrollment is None:
                break

            lead = enrollment.lead
            if lead.status in (Lead.Status.CONVERTED, Lead.Status.LOST) or lead.deleted_at:
                enrollment.status = CampaignEnrollment.Status.EXITED
                enrollment.next_send_at = None
                enrollment.save(update_fields=["status", "next_send_at"])
                processed.append((enrollment.pk, "EXITED"))
                continue

            step = (
                enrollment.campaign.steps.filter(
                    is_active=True, step_order__gt=enrollment.current_step
                )
                .order_by("step_order")
                .first()
            )
            if step is None:
                enrollment.status = CampaignEnrollment.Status.COMPLETED
                enrollment.completed_at = now
                enrollment.next_send_at = None
                enrollment.save(update_fields=["status", "completed_at", "next_send_at"])
                processed.append((enrollment.pk, "COMPLETED"))
                continue

            try:
                message = collaboration_services.send_message(
                    actor=None,
                    channel=step.channel,
                    contact=lead.contact,
                    template=step.template,
                    subject=step.subject if not step.template else None,
                    body=step.body if not step.template else None,
                )
                failed = message.status == "FAILED"
            except Exception:  # noqa: BLE001 - a broken step must not wedge the runner
                logger.exception(
                    "run_drip: send failed for enrollment %s step %s",
                    enrollment.pk, step.step_order,
                )
                failed = True

            if failed:
                enrollment.next_send_at = now + DRIP_RETRY
                enrollment.save(update_fields=["next_send_at"])
                processed.append((enrollment.pk, "RETRY"))
                continue

            enrollment.current_step = step.step_order
            following = (
                enrollment.campaign.steps.filter(
                    is_active=True, step_order__gt=step.step_order
                )
                .order_by("step_order")
                .first()
            )
            if following is None:
                enrollment.status = CampaignEnrollment.Status.COMPLETED
                enrollment.completed_at = now
                enrollment.next_send_at = None
                enrollment.save(
                    update_fields=["current_step", "status", "completed_at", "next_send_at"]
                )
            else:
                enrollment.next_send_at = now + timedelta(days=following.delay_days)
                enrollment.save(update_fields=["current_step", "next_send_at"])
            processed.append((enrollment.pk, f"SENT:{step.step_order}"))
    return processed


# --- Landing pages (SRS 3.8.2) --------------------------------------------------------------

_LANDING_FIELDS = frozenset(
    {"campaign", "slug", "title", "content", "form_config", "lead_source", "is_published"}
)


@transaction.atomic
def create_landing_page(*, actor, slug, title, **fields):
    from .models import LandingPage

    unknown = set(fields) - (_LANDING_FIELDS - {"slug", "title"})
    if unknown:
        raise ValidationError({name: "Not a landing-page field." for name in unknown})
    return LandingPage.objects.create(
        slug=slug, title=title, created_by=actor, updated_by=actor, **fields
    )


@transaction.atomic
def update_landing_page(page, *, actor, **fields):
    unknown = set(fields) - _LANDING_FIELDS
    if unknown:
        raise ValidationError({name: "Not a landing-page field." for name in unknown})
    for name, value in fields.items():
        setattr(page, name, value)
    page.updated_by = actor
    page.save()
    return page


@transaction.atomic
def submit_landing_page(page, *, form_data):
    """A public form submission → the full `capture_lead` pipeline (dedupe, scoring,
    routing, SLA, acknowledgment). Counters move by F() so concurrent submissions add up."""
    from django.db.models import F

    from .models import LandingPage

    required = [
        field["name"]
        for field in (page.form_config or {}).get("fields", [])
        if field.get("required")
    ]
    missing = [name for name in required if not (form_data.get(name) or "").strip()]
    if missing:
        raise ValidationError({name: "This field is required." for name in missing})

    name = (form_data.get("name") or "").strip()
    contact_data = {
        "contact_type": "PERSON",
        "first_name": form_data.get("first_name")
        or (name.split(" ")[0] if name else "Web"),
        "last_name": form_data.get("last_name")
        or (" ".join(name.split(" ")[1:]) if " " in name else "Visitor"),
        "email": form_data.get("email") or None,
        "phone": form_data.get("phone") or None,
    }
    if not (contact_data["email"] or contact_data["phone"]):
        raise ValidationError({"email": "Leave an email or a phone number."})

    lead = capture_lead(
        actor=None,
        contact_data=contact_data,
        lead_type=form_data.get("lead_type") or Lead.LeadType.BUY,
        source=page.lead_source,
        campaign=page.campaign,
        description=form_data.get("message") or None,
    )
    LandingPage.objects.filter(pk=page.pk).update(submissions=F("submissions") + 1)
    return lead


# --- Saved-search alerts (SRS 3.8.4) --------------------------------------------------------

#: The criteria keys an alert may save. A whitelist because criteria get replayed into
#: queryset filters for years after the UI that wrote them has changed.
ALERT_CRITERIA_KEYS = frozenset(
    {"listing_type", "min_price", "max_price", "bedrooms", "city", "property_type",
     "latitude", "longitude", "radius_km"}
)

_ALERT_FIELDS = frozenset({"name", "criteria", "frequency", "channel", "is_active"})

_ALERT_INTERVALS = {"INSTANT": timedelta(0), "DAILY": timedelta(days=1),
                    "WEEKLY": timedelta(weeks=1)}


def _validate_alert_criteria(criteria):
    unknown = set(criteria or {}) - ALERT_CRITERIA_KEYS
    if unknown:
        raise ValidationError({"criteria": f"Unknown criteria: {sorted(unknown)}"})


@transaction.atomic
def create_saved_search_alert(*, actor, contact, name, frequency, channel, criteria=None):
    from .models import SavedSearchAlert

    _validate_alert_criteria(criteria)
    return SavedSearchAlert.objects.create(
        contact=contact, name=name, frequency=frequency, channel=channel,
        criteria=criteria or {},
    )


@transaction.atomic
def update_saved_search_alert(alert, *, actor, **fields):
    unknown = set(fields) - _ALERT_FIELDS
    if unknown:
        raise ValidationError({name: "Not an alert field." for name in unknown})
    if "criteria" in fields:
        _validate_alert_criteria(fields["criteria"])
    for name, value in fields.items():
        setattr(alert, name, value)
    alert.save()
    return alert


def _alert_matches(alert, since):
    """ACTIVE listings matching the saved criteria, newer than `since`."""
    from decimal import Decimal as D

    from apps.inventory.models import Listing
    from apps.inventory.selectors import live_listings

    criteria = alert.criteria or {}
    queryset = live_listings().filter(status=Listing.Status.ACTIVE)
    if criteria.get("listing_type"):
        queryset = queryset.filter(listing_type=criteria["listing_type"])
    if criteria.get("min_price") is not None:
        floor = D(str(criteria["min_price"]))
        queryset = queryset.filter(
            Q(asking_price__gte=floor) | Q(rent_amount__gte=floor)
        )
    if criteria.get("max_price") is not None:
        ceiling = D(str(criteria["max_price"]))
        queryset = queryset.filter(
            Q(asking_price__lte=ceiling) | Q(rent_amount__lte=ceiling)
        )
    if criteria.get("bedrooms") is not None:
        queryset = queryset.filter(property__bedrooms__gte=criteria["bedrooms"])
    if criteria.get("city"):
        queryset = queryset.filter(property__city__icontains=criteria["city"])
    if criteria.get("property_type"):
        queryset = queryset.filter(
            property__property_type__name__icontains=criteria["property_type"]
        )
    if (
        criteria.get("latitude") is not None
        and criteria.get("longitude") is not None
        and criteria.get("radius_km")
    ):
        from django.contrib.gis.geos import Point
        from django.contrib.gis.measure import D as Dist

        centre = Point(
            float(criteria["longitude"]), float(criteria["latitude"]), srid=4326
        )
        queryset = queryset.filter(
            property__geo_point__dwithin=(centre, Dist(km=float(criteria["radius_km"])))
        )
    if since:
        queryset = queryset.filter(
            Q(published_at__gte=since) | Q(created_at__gte=since)
        )
    return queryset.select_related("property")


def run_saved_search_alerts(now=None):
    """Send new-listing alerts. **`last_run_at` ALWAYS advances**, matches or not, send
    failure or not — at-most-once delivery, because an alert that retries a window is an
    alert that spams a client."""
    from apps.collaboration.gateways import get_gateway

    from .models import SavedSearchAlert

    now = now or timezone.now()
    sent = []
    for alert in SavedSearchAlert.objects.filter(is_active=True).select_related("contact"):
        interval = _ALERT_INTERVALS.get(alert.frequency, timedelta(days=1))
        if alert.last_run_at and alert.last_run_at + interval > now:
            continue
        since = alert.last_run_at
        matches = list(_alert_matches(alert, since)[:10])
        # The window closes before the send is attempted: at-most-once.
        alert.last_run_at = now
        alert.save(update_fields=["last_run_at"])
        if not matches:
            continue
        address = (
            alert.contact.email if alert.channel == "EMAIL" else alert.contact.phone
        )
        if not address:
            logger.warning(
                "saved-search alert %s has no %s address on file", alert.pk, alert.channel
            )
            continue
        lines = [
            f"- {listing.title} ({listing.asking_price or listing.rent_amount})"
            for listing in matches
        ]
        try:
            get_gateway(alert.channel).send(
                to_address=address,
                subject=f"New listings for your search: {alert.name}",
                body="New matches:\n" + "\n".join(lines),
            )
            sent.append(alert.pk)
        except Exception:  # noqa: BLE001 - the window already advanced; log and move on
            logger.exception("saved-search alert %s failed to send", alert.pk)
    return sent
