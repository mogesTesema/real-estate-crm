"""
Opportunity stage movement (SRS §3.4.4, §3.4.5, plan §2.6).

Moving stage requires a reason and next action, writes a history row, syncs
probability from the target stage, and flips status to won/lost on terminal stages.
A stage-change Activity is logged to the unified timeline.
"""
from django.db import transaction

from apps.activities.services import log_activity

from .models import Opportunity, OpportunityStageHistory, Stage


class MissingReason(Exception):
    pass


@transaction.atomic
def move_stage(
    *,
    opportunity: Opportunity,
    to_stage: Stage,
    user=None,
    reason: str = "",
    next_action: str = "",
) -> Opportunity:
    if not reason:
        raise MissingReason("A reason is required to move an opportunity's stage.")
    if to_stage.pipeline_id != opportunity.pipeline_id:
        raise ValueError("Target stage belongs to a different pipeline.")

    from_stage = opportunity.stage
    OpportunityStageHistory.objects.create(
        tenant_id=opportunity.tenant_id,
        opportunity=opportunity,
        from_stage=from_stage,
        to_stage=to_stage,
        changed_by=user,
        reason=reason,
        next_action=next_action,
    )

    opportunity.stage = to_stage
    opportunity.probability = to_stage.probability
    if to_stage.is_won:
        opportunity.status = Opportunity.Status.WON
        opportunity.probability = 100
    elif to_stage.is_lost:
        opportunity.status = Opportunity.Status.LOST
        opportunity.lost_reason = reason
    else:
        opportunity.status = Opportunity.Status.OPEN
    opportunity.save(
        update_fields=["stage", "probability", "status", "lost_reason", "updated_at"]
    )

    log_activity(
        tenant_id=opportunity.tenant_id,
        actor=user,
        activity_type="stage_change",
        body=f"{from_stage.name if from_stage else '—'} → {to_stage.name}: {reason}",
        related=opportunity,
    )
    return opportunity
