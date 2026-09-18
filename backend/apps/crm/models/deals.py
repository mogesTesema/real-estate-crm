"""`crm` deal models (architecture.md §9).

Deals, viewings, agent GPS field tracking, offers, closing checklists, and the closed
transaction that anchors the money side.

Service-layer invariants recorded here because the schema cannot express them:

* **Viewings own a calendar activity.** Creating, updating, or cancelling a `Viewing` MUST go
  through `collaboration.services.upsert_activity_for_source(source_type="VIEWING", ...)`.
  The UI must never create a standalone VIEWING activity.
* **Won letting deals never insert leases directly.** `crm.services.mark_deal_won` calls
  `property_ops.services.create_lease_from_deal`; `crm` writes no `property_ops_*` row.
* **Field sessions gate field activity.** An agent must start a session with
  `gps_enabled=True` before marking sale/viewing field activity in progress (SRS 3.16.5).
"""
import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models

from apps.core.models import AppendOnlyModel, BaseModel, SoftDeleteModel

from .leads import Lead
from .pipeline import Pipeline, PipelineStage


class Deal(SoftDeleteModel):
    """The opportunity driving a sale or letting."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        WON = "WON", "Won"
        LOST = "LOST", "Lost"
        CANCELLED = "CANCELLED", "Cancelled"

    reference_code = models.CharField(max_length=50)
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="deals"
    )
    primary_contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="deals"
    )
    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT, related_name="deals")
    stage = models.ForeignKey(PipelineStage, on_delete=models.PROTECT, related_name="deals")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_deals"
    )
    title = models.CharField(max_length=200)
    deal_type = models.CharField(max_length=50)
    estimated_value = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3)
    probability = models.DecimalField(max_digits=5, decimal_places=2)
    expected_close_date = models.DateField(null=True, blank=True)
    actual_close_date = models.DateField(null=True, blank=True)
    lost_reason = models.TextField(null=True, blank=True)
    next_action = models.CharField(max_length=200, null=True, blank=True)
    next_action_due_at = models.DateTimeField(null=True, blank=True)
    custom_data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    # Denormalised from the stage history so "days in stage" is a subtraction rather than a
    # scan of every history row for every card on the board.
    stage_entered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "crm_deal"
        constraints = [
            models.UniqueConstraint(
                fields=["reference_code"],
                condition=models.Q(deleted_at__isnull=True),
                name="crm_deal_reference_uniq",
            ),
            # Mandatory loss reason (SRS 3.4.5). The spec allows this at the service layer or
            # as a CHECK; a CHECK is strictly better — it cannot be bypassed by an import, a
            # data migration, or a future endpoint that forgets.
            models.CheckConstraint(
                condition=~models.Q(status="LOST") | models.Q(lost_reason__isnull=False),
                name="crm_deal_lost_requires_reason",
            ),
        ]

    def __str__(self):
        return self.reference_code


class DealStageHistory(models.Model):
    """Every stage movement, with the reason that justified it (SRS 3.4.4).

    > "The System shall log a mandatory reason and next action when a deal moves stage or is
    >  marked Lost."

    `reason` is NOT NULL and the service refuses an empty one: a pipeline whose history says
    only *that* a deal moved, never *why*, cannot answer the question a manager actually
    asks. `crm_lead_status_history` and `inventory_property_status_history` already exist for
    their entities; this is the deal's equivalent.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="stage_history")
    from_stage = models.ForeignKey(
        PipelineStage,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    to_stage = models.ForeignKey(
        PipelineStage, on_delete=models.PROTECT, related_name="+"
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField()
    next_action = models.CharField(max_length=200, null=True, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_deal_stage_history"
        ordering = ["deal", "-changed_at"]
        constraints = [
            # The mandate is "a mandatory reason", so an empty string is not a reason.
            models.CheckConstraint(
                condition=~models.Q(reason=""), name="crm_deal_stage_reason_not_blank"
            ),
        ]


class DealProperty(models.Model):
    """The properties an opportunity is about (SRS 3.4.7).

    > "The System shall support linking one or more properties to an opportunity, and one
    >  opportunity to a specific matched property once identified."

    Hence many rows per deal, with `is_primary` marking the one finally matched.
    architecture.md §3's entity diagram lists "Deal ├── Property/properties" but §9's table
    definition omits any such column; this closes that gap (see the v3.3 changelog).

    `unit` is optional: a deal may be about a whole property or one addressable unit of it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="deal_properties")
    property = models.ForeignKey(
        "inventory.Property", on_delete=models.PROTECT, related_name="deal_links"
    )
    unit = models.ForeignKey(
        "inventory.Unit",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deal_links",
    )
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "crm_deal_property"
        constraints = [
            # NULLS NOT DISTINCT: `unit` is null for a whole-property link, and Postgres
            # would otherwise treat two such rows as distinct, letting the same property be
            # attached to the same deal twice.
            models.UniqueConstraint(
                fields=["deal", "property", "unit"],
                name="crm_deal_property_uniq",
                nulls_distinct=False,
            ),
            # "one opportunity to a specific matched property once identified" — one primary,
            # not several.
            models.UniqueConstraint(
                fields=["deal"],
                condition=models.Q(is_primary=True),
                name="crm_deal_one_primary_property",
            ),
        ]


class Viewing(BaseModel):
    """A property viewing appointment, with GPS check-in (SRS 3.16.4)."""

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        CONFIRMED = "CONFIRMED", "Confirmed"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"
        NO_SHOW = "NO_SHOW", "No show"

    deal = models.ForeignKey(
        Deal, null=True, blank=True, on_delete=models.CASCADE, related_name="viewings"
    )
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.CASCADE, related_name="viewings"
    )
    property = models.ForeignKey(
        "inventory.Property", on_delete=models.PROTECT, related_name="viewings"
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="viewings"
    )
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="viewings"
    )
    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    location = models.CharField(max_length=200, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    check_in_at = models.DateTimeField(null=True, blank=True)
    check_out_at = models.DateTimeField(null=True, blank=True)
    check_in_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    check_in_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    feedback = models.TextField(null=True, blank=True)
    rating = models.IntegerField(null=True, blank=True)
    # `activity` (FK collaboration.Activity, nullable) is an optional denormalized link added
    # after collaboration exists. The authoritative link is source_type/source_id on the
    # activity itself.

    class Meta:
        db_table = "crm_viewing"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(scheduled_end__gt=models.F("scheduled_start")),
                name="crm_viewing_end_after_start",
            )
        ]


class AgentFieldSession(BaseModel):
    """A GPS-tracked field trip (SRS 3.16.5–3.16.7).

    Manager/owner reads of these sessions are policy-gated and audited — this is location data
    about an employee, and §9 treats it accordingly.
    """

    class SessionType(models.TextChoices):
        SALE_TRIP = "SALE_TRIP", "Sale trip"
        VIEWING = "VIEWING", "Viewing"
        FOLLOW_UP = "FOLLOW_UP", "Follow up"
        OTHER_FIELD = "OTHER_FIELD", "Other field work"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="field_sessions"
    )
    session_type = models.CharField(max_length=20, choices=SessionType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="field_sessions"
    )
    deal = models.ForeignKey(
        Deal, null=True, blank=True, on_delete=models.SET_NULL, related_name="field_sessions"
    )
    property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="field_sessions",
    )
    viewing = models.ForeignKey(
        Viewing, null=True, blank=True, on_delete=models.SET_NULL, related_name="field_sessions"
    )
    gps_required = models.BooleanField(default=True)
    gps_enabled = models.BooleanField(default=False)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "crm_agent_field_session"


class AgentLocationPoint(AppendOnlyModel):
    """One GPS breadcrumb in a field session.

    Append-only: UPDATE/DELETE are revoked at the database-role level, so a recorded trail
    cannot be quietly rewritten. Retention (bulk deletion of old trails) is a Super Admin
    policy applied out of band, not something the app role can do row by row.
    """

    session = models.ForeignKey(
        AgentFieldSession, on_delete=models.CASCADE, related_name="location_points"
    )
    recorded_at = models.DateTimeField()
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    accuracy_m = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    geo_point = gis_models.PointField(geography=True, srid=4326, null=True, blank=True)

    class Meta:
        db_table = "crm_agent_location_point"


class Offer(models.Model):
    """An offer or counter-offer.

    `parent_offer` chains counters into a negotiation thread (SRS 3.6.1): each counter is a
    new row pointing at the offer it responds to, and `direction` records who countered whom.
    Offers are never edited into a new amount — that would erase the negotiation history.
    """

    class Direction(models.TextChoices):
        BUYER_TO_SELLER = "BUYER_TO_SELLER", "Buyer to seller"
        SELLER_TO_BUYER = "SELLER_TO_BUYER", "Seller to buyer"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        COUNTERED = "COUNTERED", "Countered"
        ACCEPTED = "ACCEPTED", "Accepted"
        REJECTED = "REJECTED", "Rejected"
        EXPIRED = "EXPIRED", "Expired"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deal = models.ForeignKey(Deal, on_delete=models.PROTECT, related_name="offers")
    property = models.ForeignKey(
        "inventory.Property", on_delete=models.PROTECT, related_name="offers"
    )
    parent_offer = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="counters"
    )
    offered_by_contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="offers"
    )
    direction = models.CharField(max_length=20, choices=Direction.choices)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    deposit_amount = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    conditions = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "crm_offer"


class Transaction(BaseModel):
    """A closed sale or rental — the anchor the money side hangs off.

    Referenced by finance commissions, installment plans, and invoices, and by
    `property_ops.Lease.transaction` as lineage. `reference_code` is fully unique (not
    partial): transactions are historical records and are never soft-deleted.
    """

    class TransactionType(models.TextChoices):
        SALE = "SALE", "Sale"
        RENTAL = "RENTAL", "Rental"
        LEASE_RENEWAL = "LEASE_RENEWAL", "Lease renewal"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONTRACTED = "CONTRACTED", "Contracted"
        PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    deal = models.ForeignKey(
        Deal, null=True, blank=True, on_delete=models.PROTECT, related_name="transactions"
    )
    property = models.ForeignKey(
        "inventory.Property", on_delete=models.PROTECT, related_name="transactions"
    )
    transaction_type = models.CharField(max_length=20, choices=TransactionType.choices)
    reference_code = models.CharField(max_length=50, unique=True)
    gross_amount = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3)
    transaction_date = models.DateField()
    closing_date = models.DateField(null=True, blank=True)
    contract_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "crm_transaction"

    def __str__(self):
        return self.reference_code


class ClosingChecklist(BaseModel):
    """Transaction-type-specific closing checklist (SRS 3.6.6)."""

    class ChecklistType(models.TextChoices):
        SALE = "SALE", "Sale"
        RENTAL = "RENTAL", "Rental"
        OFF_PLAN = "OFF_PLAN", "Off-plan"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    deal = models.ForeignKey(
        Deal, null=True, blank=True, on_delete=models.CASCADE, related_name="closing_checklists"
    )
    transaction = models.ForeignKey(
        Transaction,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="closing_checklists",
    )
    checklist_type = models.CharField(max_length=20, choices=ChecklistType.choices)
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    class Meta:
        db_table = "crm_closing_checklist"
        constraints = [
            # Both FKs are nullable in the spec, but a checklist attached to neither a deal
            # nor a transaction is orphaned and unreachable.
            models.CheckConstraint(
                condition=models.Q(deal__isnull=False) | models.Q(transaction__isnull=False),
                name="crm_closing_checklist_has_parent",
            )
        ]


class ClosingChecklistItem(models.Model):
    """One line of a closing checklist, optionally requiring a document."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    checklist = models.ForeignKey(
        ClosingChecklist, on_delete=models.CASCADE, related_name="items"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    is_required = models.BooleanField(default=True)
    is_completed = models.BooleanField(default=False)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    # Added by crm/0003 once collaboration exists — the signed contract or receipt that
    # satisfies this checklist item.
    document = models.ForeignKey(
        "collaboration.Document",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="checklist_items",
    )
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "crm_closing_checklist_item"
        ordering = ["checklist", "sort_order"]
