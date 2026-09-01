"""`property_ops` maintenance models (architecture.md §14).

Vendors, maintenance requests, work orders.

Cost flows one way: a completed work order's `final_amount` becomes a `finance.Expense`
through `finance.services.record_expense`, and a billable expense lands on the landlord's
owner statement. `property_ops` never writes a finance row itself.
"""
import uuid

from django.conf import settings
from django.db import models


class Vendor(models.Model):
    """A service provider, layered on the contact record that identifies them.

    `contact` is unique: one vendor profile per contact, so vendor ratings and licences
    cannot fork across duplicate rows.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contact = models.OneToOneField(
        "contacts.Contact", on_delete=models.PROTECT, related_name="vendor_profile"
    )
    service_category = models.CharField(max_length=100)
    license_number = models.CharField(max_length=100, null=True, blank=True)
    rating = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "property_ops_vendor"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__isnull=True)
                | models.Q(rating__gte=0, rating__lte=5),
                name="property_ops_vendor_rating_range",
            )
        ]

    def __str__(self):
        return f"Vendor {self.contact_id}"


class MaintenanceRequest(models.Model):
    """A reported maintenance issue.

    `unit` points at `inventory.Unit`, per §8's routing rule — a single-unit property may
    have no units and be maintained at property level, which is why it stays nullable.
    """

    class Priority(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        EMERGENCY = "EMERGENCY", "Emergency"

    class Status(models.TextChoices):
        NEW = "NEW", "New"
        ASSIGNED = "ASSIGNED", "Assigned"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        WAITING_FOR_PARTS = "WAITING_FOR_PARTS", "Waiting for parts"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    property = models.ForeignKey(
        "inventory.Property", on_delete=models.PROTECT, related_name="maintenance_requests"
    )
    unit = models.ForeignKey(
        "inventory.Unit",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="maintenance_requests",
    )
    lease = models.ForeignKey(
        "property_ops.Lease",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="maintenance_requests",
    )
    reported_by_contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reported_maintenance",
    )
    reported_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reported_maintenance",
    )
    title = models.CharField(max_length=200)
    description = models.TextField()
    priority = models.CharField(
        max_length=20, choices=Priority.choices, default=Priority.MEDIUM
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    assigned_vendor = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="maintenance_requests",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_maintenance",
    )
    estimated_cost = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    actual_cost = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "property_ops_maintenance_request"
        constraints = [
            # Both reporter FKs are nullable in the spec, but an issue reported by nobody has
            # no one to notify of progress.
            models.CheckConstraint(
                condition=models.Q(reported_by_contact__isnull=False)
                | models.Q(reported_by_user__isnull=False),
                name="property_ops_maintenance_has_reporter",
            )
        ]

    def __str__(self):
        return self.title


class WorkOrder(models.Model):
    """A vendor job dispatched for a maintenance request.

    The spec leaves `status` as an unspecified VARCHAR; an explicit enum is defined here so
    the quote -> approve -> complete progression is legible rather than free-form. Flagged as
    a deliberate narrowing of the spec.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        QUOTED = "QUOTED", "Quoted"
        APPROVED = "APPROVED", "Approved"
        SCHEDULED = "SCHEDULED", "Scheduled"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Column name follows the spec's `property_ops_maintenance_request_id` literally.
    maintenance_request = models.ForeignKey(
        MaintenanceRequest,
        on_delete=models.PROTECT,
        related_name="work_orders",
        db_column="property_ops_maintenance_request_id",
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="work_orders")
    scheduled_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    quoted_amount = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    approved_amount = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    final_amount = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "property_ops_work_order"
