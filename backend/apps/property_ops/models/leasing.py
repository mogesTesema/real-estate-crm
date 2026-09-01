"""`property_ops` leasing models (architecture.md §10).

Leases, parties, rent schedules, deposits, inspections, screening applications, renewals.

**"Tenant" here means a rental tenant.** `Lease.tenant` is a FK to `contacts.Contact` — a
lease party. Nothing in this schema carries a SaaS customer partition.

Service-layer rules the schema cannot express:

* `Lease.transaction` is **lineage only**. It is set exclusively inside `property_ops.services`
  after reading the deal/transaction through `crm.selectors`. `crm` must never write a lease row.
* Scheduling, completing, or cancelling an `Inspection` MUST upsert a `collaboration.Activity`
  with `source_type="INSPECTION"` — no orphan INSPECTION calendar rows.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.models import BaseModel


class Lease(models.Model):
    """A tenancy contract over a property or one of its units.

    The overlap guarantee is a database exclusion constraint, not application logic — see
    property_ops/0002. Double-letting a unit is the kind of error that must be impossible
    rather than merely unlikely.
    """

    class LeaseType(models.TextChoices):
        RESIDENTIAL = "RESIDENTIAL", "Residential"
        COMMERCIAL = "COMMERCIAL", "Commercial"
        SHORT_TERM = "SHORT_TERM", "Short term"

    class BillingFrequency(models.TextChoices):
        MONTHLY = "MONTHLY", "Monthly"
        QUARTERLY = "QUARTERLY", "Quarterly"
        YEARLY = "YEARLY", "Yearly"
        CUSTOM = "CUSTOM", "Custom"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_SIGNATURE = "PENDING_SIGNATURE", "Pending signature"
        ACTIVE = "ACTIVE", "Active"
        EXPIRING = "EXPIRING", "Expiring"
        RENEWED = "RENEWED", "Renewed"
        TERMINATED = "TERMINATED", "Terminated"
        EXPIRED = "EXPIRED", "Expired"

    #: Statuses that occupy the rentable target, and so participate in the overlap exclusion.
    OCCUPYING_STATUSES = ("PENDING_SIGNATURE", "ACTIVE", "EXPIRING", "RENEWED")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference_code = models.CharField(max_length=50, unique=True)
    property = models.ForeignKey(
        "inventory.Property", on_delete=models.PROTECT, related_name="leases"
    )
    unit = models.ForeignKey(
        "inventory.Unit", null=True, blank=True, on_delete=models.PROTECT, related_name="leases"
    )
    # The rental tenant and the landlord, both contacts.
    tenant = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="tenancies"
    )
    landlord = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="landlord_leases"
    )
    property_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="managed_leases"
    )
    transaction = models.ForeignKey(
        "crm.Transaction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="leases",
    )
    lease_type = models.CharField(max_length=20, choices=LeaseType.choices)
    start_date = models.DateField()
    end_date = models.DateField()
    rent_amount = models.DecimalField(max_digits=15, decimal_places=2)
    billing_frequency = models.CharField(max_length=20, choices=BillingFrequency.choices)
    security_deposit = models.DecimalField(max_digits=15, decimal_places=2)
    management_fee = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    notice_period_days = models.IntegerField(null=True, blank=True)
    terms = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "property_ops_lease"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gt=models.F("start_date")),
                name="property_ops_lease_end_after_start",
            )
        ]

    def __str__(self):
        return self.reference_code


class LeaseParty(models.Model):
    """Additional parties on a lease beyond the primary tenant and landlord."""

    class PartyType(models.TextChoices):
        TENANT = "TENANT", "Tenant"
        CO_TENANT = "CO_TENANT", "Co-tenant"
        GUARANTOR = "GUARANTOR", "Guarantor"
        LANDLORD = "LANDLORD", "Landlord"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name="parties")
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="lease_parties"
    )
    party_type = models.CharField(max_length=20, choices=PartyType.choices)
    share_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )

    class Meta:
        db_table = "property_ops_lease_party"


class RentSchedule(models.Model):
    """One rent period and its invoicing/payment state.

    `invoice` is written only by `finance.services` when the period is invoiced — rent
    schedules describe *what is owed when*; the invoice is the money artefact.
    """

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        INVOICED = "INVOICED", "Invoiced"
        PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
        PAID = "PAID", "Paid"
        OVERDUE = "OVERDUE", "Overdue"
        WAIVED = "WAIVED", "Waived"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name="rent_schedules")
    period_start = models.DateField()
    period_end = models.DateField()
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    late_fee_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    # Added by property_ops/0003 rather than 0001: finance is built after property_ops, so
    # the target did not exist when this table was created.
    invoice = models.ForeignKey(
        "finance.Invoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="rent_schedules",
    )

    class Meta:
        db_table = "property_ops_rent_schedule"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period_end__gt=models.F("period_start")),
                name="property_ops_rent_period_valid",
            )
        ]


class Deposit(models.Model):
    """Security deposit held against a lease."""

    class Status(models.TextChoices):
        HELD = "HELD", "Held"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
        FULLY_REFUNDED = "FULLY_REFUNDED", "Fully refunded"
        FORFEITED = "FORFEITED", "Forfeited"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(Lease, on_delete=models.PROTECT, related_name="deposits")
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    held_amount = models.DecimalField(max_digits=15, decimal_places=2)
    refunded_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    deducted_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.HELD)
    received_date = models.DateField(null=True, blank=True)
    refunded_date = models.DateField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "property_ops_deposit"
        constraints = [
            # The spec states no CHECK here, but money that has been refunded plus money
            # deducted cannot exceed money taken — an unstated invariant worth holding.
            models.CheckConstraint(
                condition=models.Q(
                    refunded_amount__gte=0, deducted_amount__gte=0, amount__gte=0
                )
                & models.Q(
                    amount__gte=models.F("refunded_amount") + models.F("deducted_amount")
                ),
                name="property_ops_deposit_amounts_balance",
            )
        ]


class Inspection(models.Model):
    """Move-in / move-out / routine / maintenance inspection.

    Must be created, completed, and cancelled through `property_ops.services`, which upserts
    the linked calendar activity.
    """

    class InspectionType(models.TextChoices):
        MOVE_IN = "MOVE_IN", "Move in"
        MOVE_OUT = "MOVE_OUT", "Move out"
        ROUTINE = "ROUTINE", "Routine"
        MAINTENANCE = "MAINTENANCE", "Maintenance"

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(Lease, on_delete=models.CASCADE, related_name="inspections")
    property = models.ForeignKey(
        "inventory.Property", on_delete=models.PROTECT, related_name="inspections"
    )
    inspection_type = models.CharField(max_length=20, choices=InspectionType.choices)
    scheduled_date = models.DateField()
    completed_date = models.DateField(null=True, blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inspections"
    )
    condition_summary = models.TextField(null=True, blank=True)
    meter_readings = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    # `activity` (FK collaboration.Activity, nullable) is an optional denormalized link added
    # after collaboration exists.

    class Meta:
        db_table = "property_ops_inspection"


class Application(BaseModel):
    """Rental application and tenant screening (SRS 3.5.2).

    On approval the status becomes CONVERTED and a lease is created from it.
    """

    class CheckStatus(models.TextChoices):
        NOT_STARTED = "NOT_STARTED", "Not started"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"
        WAIVED = "WAIVED", "Waived"

    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Submitted"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"
        CONVERTED = "CONVERTED", "Converted"

    property = models.ForeignKey(
        "inventory.Property", on_delete=models.PROTECT, related_name="applications"
    )
    unit = models.ForeignKey(
        "inventory.Unit",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="applications",
    )
    applicant_contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.PROTECT, related_name="rental_applications"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_applications",
    )
    desired_move_in = models.DateField(null=True, blank=True)
    proposed_rent = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    employment_status = models.CharField(max_length=50, null=True, blank=True)
    stated_income = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    background_check_status = models.CharField(
        max_length=20, choices=CheckStatus.choices, default=CheckStatus.NOT_STARTED
    )
    credit_check_status = models.CharField(
        max_length=20, choices=CheckStatus.choices, default=CheckStatus.NOT_STARTED
    )
    screening_result = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "property_ops_application"


class Renewal(BaseModel):
    """Lease renewal negotiation with rent escalation (SRS 3.5.1/3.5.5).

    `new_lease` is populated once accepted and a successor lease is generated; the original
    lease then moves to status RENEWED.
    """

    class EscalationType(models.TextChoices):
        NONE = "NONE", "None"
        FIXED_AMOUNT = "FIXED_AMOUNT", "Fixed amount"
        PERCENTAGE = "PERCENTAGE", "Percentage"

    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        NEGOTIATING = "NEGOTIATING", "Negotiating"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        EXPIRED = "EXPIRED", "Expired"

    original_lease = models.ForeignKey(
        Lease, on_delete=models.PROTECT, related_name="renewals"
    )
    new_lease = models.OneToOneField(
        Lease,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="renewed_from",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PROPOSED)
    proposed_start_date = models.DateField()
    proposed_end_date = models.DateField()
    current_rent = models.DecimalField(max_digits=15, decimal_places=2)
    proposed_rent = models.DecimalField(max_digits=15, decimal_places=2)
    escalation_type = models.CharField(
        max_length=20, choices=EscalationType.choices, default=EscalationType.NONE
    )
    escalation_value = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    notice_sent_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "property_ops_renewal"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(proposed_end_date__gt=models.F("proposed_start_date")),
                name="property_ops_renewal_dates_valid",
            ),
            # A renewal cannot succeed itself.
            models.CheckConstraint(
                condition=~models.Q(new_lease=models.F("original_lease")),
                name="property_ops_renewal_distinct_leases",
            ),
        ]
