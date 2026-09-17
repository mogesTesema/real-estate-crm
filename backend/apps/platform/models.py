"""`platform` models (architecture.md §15, §16, §19).

Audit trail, external integrations and sync mapping, saved reports and dashboard snapshots.

`platform` is a satellite (§1.2): it reads domain data through selectors and never calls a
domain write service. Report builders in particular must go through `selectors` rather than
importing another app's models directly.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.models import AppendOnlyModel


class AuditEvent(AppendOnlyModel):
    """The global "who did what to which record" trail.

    Append-only, enforced by a database-role REVOKE (platform/0002). Written through
    `platform.services.record_event`, called from domain services — not from model signals,
    which cannot see the acting user or the request context.
    """

    class Action(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        DELETE = "DELETE", "Delete"
        VIEW = "VIEW", "View"
        EXPORT = "EXPORT", "Export"
        LOGIN = "LOGIN", "Login"
        LOGIN_FAILED = "LOGIN_FAILED", "Login failed"
        PAYMENT_POSTED = "PAYMENT_POSTED", "Payment posted"
        COMMISSION_APPROVED = "COMMISSION_APPROVED", "Commission approved"

    # Distinct from AppendOnlyModel.created_by: the actor may be null for system actions and
    # for LOGIN_FAILED, where there is no authenticated user yet.
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    action = models.CharField(max_length=30, choices=Action.choices)
    entity_type = models.CharField(max_length=50)
    entity_id = models.UUIDField(null=True, blank=True)
    old_values = models.JSONField(null=True, blank=True)
    new_values = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "platform_audit_event"
        indexes = [
            # "What happened to this record?" — the query an investigation starts with.
            models.Index(
                fields=["entity_type", "entity_id", "-created_at"],
                name="platform_audit_entity_idx",
            ),
            # "What did this user do?" — the other one.
            models.Index(
                fields=["actor_user", "-created_at"], name="platform_audit_actor_idx"
            ),
        ]


class Connection(models.Model):
    """A configured external integration — portal feed, calendar, messaging, e-sign.

    `credentials_ref` is a **pointer into the secrets manager**, never the secret itself. The
    database holds the name of the credential, not its value.
    """

    class Provider(models.TextChoices):
        PROPERTY_FINDER = "PROPERTY_FINDER", "Property Finder"
        BAYUT = "BAYUT", "Bayut"
        DUBIZZLE = "DUBIZZLE", "Dubizzle"
        MLS = "MLS", "MLS"
        GOOGLE = "GOOGLE", "Google"
        OUTLOOK = "OUTLOOK", "Outlook"
        WHATSAPP = "WHATSAPP", "WhatsApp"
        TWILIO = "TWILIO", "Twilio"
        MAILGUN = "MAILGUN", "Mailgun"
        DOCUSIGN = "DOCUSIGN", "DocuSign"
        WEBHOOK = "WEBHOOK", "Webhook"
        OTHER = "OTHER", "Other"

    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"
        BIDIRECTIONAL = "BIDIRECTIONAL", "Bidirectional"

    class AuthType(models.TextChoices):
        API_KEY = "API_KEY", "API key"
        OAUTH2 = "OAUTH2", "OAuth2"
        BASIC = "BASIC", "Basic"
        NONE = "NONE", "None"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        ERROR = "ERROR", "Error"
        DISABLED = "DISABLED", "Disabled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    provider = models.CharField(max_length=30, choices=Provider.choices)
    direction = models.CharField(max_length=20, choices=Direction.choices)
    auth_type = models.CharField(max_length=20, choices=AuthType.choices)
    credentials_ref = models.CharField(max_length=255, null=True, blank=True)
    config = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "platform_connection"

    def __str__(self):
        return self.name


class ExternalMapping(models.Model):
    """Local UUID <-> external id correspondence for one connection.

    The two unique constraints are what make re-syncs idempotent in **both** directions: a
    local record maps to at most one external id per connection, and vice versa. Without the
    second, a re-import creates duplicates; without the first, a re-export does.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        Connection, on_delete=models.CASCADE, related_name="mappings"
    )
    entity_type = models.CharField(max_length=50)
    local_id = models.UUIDField()
    external_id = models.CharField(max_length=255)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    # Change detection: if the hash matches, the record has not moved since the last sync.
    sync_hash = models.CharField(max_length=128, null=True, blank=True)

    class Meta:
        db_table = "platform_external_mapping"
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "entity_type", "local_id"],
                name="platform_mapping_local_uniq",
            ),
            models.UniqueConstraint(
                fields=["connection", "entity_type", "external_id"],
                name="platform_mapping_external_uniq",
            ),
        ]


class Webhook(models.Model):
    """Outbound webhook targets and inbound webhook registrations (SRS 3.19.2).

    `secret` is used for HMAC signature verification — an unsigned inbound webhook endpoint
    is an open write path into the CRM.
    """

    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        Connection, null=True, blank=True, on_delete=models.CASCADE, related_name="webhooks"
    )
    direction = models.CharField(max_length=20, choices=Direction.choices)
    event_type = models.CharField(max_length=100)
    target_url = models.URLField(max_length=500, null=True, blank=True)
    secret = models.CharField(max_length=255, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "platform_webhook"
        constraints = [
            # An outbound webhook with nowhere to post is inert.
            models.CheckConstraint(
                condition=~models.Q(direction="OUTBOUND") | models.Q(target_url__isnull=False),
                name="platform_webhook_outbound_has_target",
            )
        ]


class SyncLog(models.Model):
    """Per-run sync history and error alerting (SRS 3.10.3/3.10.4)."""

    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"

    class Status(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(
        Connection, on_delete=models.CASCADE, related_name="sync_logs"
    )
    entity_type = models.CharField(max_length=50, null=True, blank=True)
    direction = models.CharField(max_length=20, choices=Direction.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    records_processed = models.IntegerField(default=0)
    records_failed = models.IntegerField(default=0)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    error_detail = models.TextField(null=True, blank=True)
    payload_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "platform_sync_log"


class SavedReport(models.Model):
    """A user-defined report: filters, columns, grouping (SRS 3.13.3).

    `visibility` does **not** override row scoping. A report shared at ORG level still renders
    only the rows the viewer's `data_scope` permits — visibility controls who can open the
    definition, `identity.selectors.apply_scope` controls what they see in it.
    """

    class ReportType(models.TextChoices):
        SALES = "SALES", "Sales"
        LEADS = "LEADS", "Leads"
        LISTINGS = "LISTINGS", "Listings"
        FINANCE = "FINANCE", "Finance"
        LEASING = "LEASING", "Leasing"
        MAINTENANCE = "MAINTENANCE", "Maintenance"
        MARKETING = "MARKETING", "Marketing"
        CUSTOM = "CUSTOM", "Custom"

    class Visibility(models.TextChoices):
        PRIVATE = "PRIVATE", "Private"
        TEAM = "TEAM", "Team"
        BRANCH = "BRANCH", "Branch"
        ORG = "ORG", "Organization"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    report_type = models.CharField(max_length=20, choices=ReportType.choices)
    definition = models.JSONField(default=dict, blank=True)
    visibility = models.CharField(
        max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_reports"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "platform_saved_report"

    def __str__(self):
        return self.name


class ReportSchedule(models.Model):
    """Scheduled delivery of a saved report. `next_run_at` is the scheduler's claim key."""

    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"

    class ExportFormat(models.TextChoices):
        PDF = "PDF", "PDF"
        XLSX = "XLSX", "XLSX"
        CSV = "CSV", "CSV"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    saved_report = models.ForeignKey(
        SavedReport, on_delete=models.CASCADE, related_name="schedules"
    )
    frequency = models.CharField(max_length=20, choices=Frequency.choices)
    next_run_at = models.DateTimeField()
    recipients = models.JSONField(default=list, blank=True)
    export_format = models.CharField(max_length=10, choices=ExportFormat.choices)
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_report_schedule"


class DashboardSnapshot(models.Model):
    """Pre-computed dashboard metrics (SRS 3.13.1).

    Exists so role dashboards do not recompute KPIs over a million rows on every load. The
    unique constraint is the nightly job's upsert key.
    """

    class SnapshotType(models.TextChoices):
        KPI_DAILY = "KPI_DAILY", "Daily KPIs"
        PIPELINE = "PIPELINE", "Pipeline"
        LEADERBOARD = "LEADERBOARD", "Leaderboard"
        FINANCE_SUMMARY = "FINANCE_SUMMARY", "Finance summary"

    class ScopeType(models.TextChoices):
        ORG = "ORG", "Organization"
        BRANCH = "BRANCH", "Branch"
        TEAM = "TEAM", "Team"
        USER = "USER", "User"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    snapshot_type = models.CharField(max_length=30, choices=SnapshotType.choices)
    scope_type = models.CharField(max_length=20, choices=ScopeType.choices)
    scope_id = models.UUIDField(null=True, blank=True)
    as_of_date = models.DateField()
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "platform_dashboard_snapshot"
        constraints = [
            # ORG scope has a null scope_id, and Postgres treats NULLs as distinct in a plain
            # unique index — which would let the nightly job insert a duplicate ORG snapshot
            # every run. NULLS NOT DISTINCT makes the upsert key actually hold.
            models.UniqueConstraint(
                fields=["snapshot_type", "scope_type", "scope_id", "as_of_date"],
                name="platform_dashboard_snapshot_uniq",
                nulls_distinct=False,
            )
        ]
