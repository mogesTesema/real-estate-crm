"""Models for `crm` (architecture.md §6, §7, §9).

architecture.md §1.2 makes this package layout normative, not optional:

    models/marketing.py
    models/leads.py
    models/pipeline.py
    models/deals.py

Every model is re-exported here so `from apps.crm.models import X` works regardless of which
submodule defines it.
"""
from .deals import (
    AgentFieldSession,
    AgentLocationPoint,
    ClosingChecklist,
    ClosingChecklistItem,
    Deal,
    DealProperty,
    DealStageHistory,
    Offer,
    Transaction,
    Viewing,
)
from .leads import (
    Lead,
    LeadAssignment,
    LeadLocationPreference,
    LeadRoutingRule,
    LeadStatusHistory,
)
from .marketing import (
    Campaign,
    CampaignMetric,
    CampaignStep,
    LandingPage,
    LeadSource,
    SavedSearchAlert,
)
from .pipeline import Pipeline, PipelineStage

__all__ = [
    "AgentFieldSession",
    "AgentLocationPoint",
    "Campaign",
    "CampaignMetric",
    "CampaignStep",
    "ClosingChecklist",
    "ClosingChecklistItem",
    "Deal",
    "DealProperty",
    "DealStageHistory",
    "LandingPage",
    "Lead",
    "LeadAssignment",
    "LeadLocationPreference",
    "LeadRoutingRule",
    "LeadSource",
    "LeadStatusHistory",
    "Offer",
    "Pipeline",
    "PipelineStage",
    "SavedSearchAlert",
    "Transaction",
    "Viewing",
]
