"""`collaboration` document models (architecture.md §12).

Files, documents, polymorphic links, access grants, e-signature envelopes and their audit
trail.

`File` is the target of every deferred `*_file_id` FK in the schema (user avatars, company
logos, inventory media, call recordings) — which is why `collaboration` is built late and
those columns arrive in follow-up migrations.
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.models import AppendOnlyModel, SoftDeleteModel


class File(models.Model):
    """Metadata for a stored blob. The bytes live in S3-compatible object storage.

    `checksum` is what makes de-duplication and integrity checks possible; `is_encrypted`
    records whether the object was written with client-side encryption.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    original_name = models.CharField(max_length=255)
    storage_key = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=127)
    size_bytes = models.BigIntegerField()
    checksum = models.CharField(max_length=128)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_files"
    )
    is_encrypted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "collaboration_file"

    def __str__(self):
        return self.original_name


class Document(SoftDeleteModel):
    """A business document wrapping a stored file.

    `is_confidential` is a coarse flag; real per-role/per-user control is `AccessGrant`.
    """

    class DocumentType(models.TextChoices):
        ID_DOCUMENT = "ID_DOCUMENT", "ID document"
        PROPERTY_DEED = "PROPERTY_DEED", "Property deed"
        LISTING_AGREEMENT = "LISTING_AGREEMENT", "Listing agreement"
        OFFER = "OFFER", "Offer"
        SALE_CONTRACT = "SALE_CONTRACT", "Sale contract"
        LEASE = "LEASE", "Lease"
        INVOICE = "INVOICE", "Invoice"
        RECEIPT = "RECEIPT", "Receipt"
        INSPECTION_REPORT = "INSPECTION_REPORT", "Inspection report"
        OTHER = "OTHER", "Other"

    file = models.ForeignKey(File, on_delete=models.PROTECT, related_name="documents")
    title = models.CharField(max_length=255)
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    version = models.IntegerField(default=1)
    status = models.CharField(max_length=50)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_documents"
    )
    is_confidential = models.BooleanField(default=False)

    class Meta:
        db_table = "collaboration_document"

    def __str__(self):
        return self.title


class DocumentLink(models.Model):
    """Attaches a document to whatever it is about.

    Polymorphic by nullable FK rather than a generic relation, so the database still enforces
    referential integrity to each target.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="links")
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="document_links",
    )
    property = models.ForeignKey(
        "inventory.Property",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="document_links",
    )
    deal = models.ForeignKey(
        "crm.Deal", null=True, blank=True, on_delete=models.CASCADE, related_name="document_links"
    )
    lease = models.ForeignKey(
        "property_ops.Lease",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="document_links",
    )
    transaction = models.ForeignKey(
        "crm.Transaction",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="document_links",
    )
    thread = models.ForeignKey(
        "collaboration.Thread",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="document_links",
    )
    message = models.ForeignKey(
        "collaboration.Message",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="document_links",
    )
    relationship_type = models.CharField(max_length=50)

    class Meta:
        db_table = "collaboration_document_link"
        constraints = [
            # "At least one of the polymorphic link targets should be set" — a link to nothing
            # makes the document unreachable from any record.
            models.CheckConstraint(
                condition=(
                    models.Q(contact__isnull=False)
                    | models.Q(property__isnull=False)
                    | models.Q(deal__isnull=False)
                    | models.Q(lease__isnull=False)
                    | models.Q(transaction__isnull=False)
                    | models.Q(thread__isnull=False)
                    | models.Q(message__isnull=False)
                ),
                name="collaboration_document_link_has_target",
            )
        ]


class AccessGrant(models.Model):
    """Role- or user-level document ACL (SRS 3.7.4).

    Refines `Document.is_confidential`: e.g. confidential financial documents visible only to
    Finance and Management. Signed-URL downloads are gated on this.
    """

    class AccessLevel(models.TextChoices):
        VIEW = "VIEW", "View"
        DOWNLOAD = "DOWNLOAD", "Download"
        EDIT = "EDIT", "Edit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="access_grants"
    )
    role = models.ForeignKey(
        "identity.Role",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="document_grants",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="document_grants",
    )
    access_level = models.CharField(max_length=20, choices=AccessLevel.choices)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "collaboration_access_grant"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__isnull=False) | models.Q(user__isnull=False),
                name="collaboration_access_grant_has_grantee",
            )
        ]


class EsignEnvelope(models.Model):
    """An e-signature envelope, internal or provider-backed."""

    class Provider(models.TextChoices):
        INTERNAL = "INTERNAL", "Internal"
        DOCUSIGN = "DOCUSIGN", "DocuSign"
        ADOBE_SIGN = "ADOBE_SIGN", "Adobe Sign"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SENT = "SENT", "Sent"
        PARTIALLY_SIGNED = "PARTIALLY_SIGNED", "Partially signed"
        COMPLETED = "COMPLETED", "Completed"
        DECLINED = "DECLINED", "Declined"
        VOIDED = "VOIDED", "Voided"
        EXPIRED = "EXPIRED", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="esign_envelopes"
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    external_envelope_id = models.CharField(max_length=200, null=True, blank=True)
    subject = models.CharField(max_length=255)
    message = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "collaboration_esign_envelope"


class EsignSigner(models.Model):
    """A party on an envelope. `signing_order` drives sequential signing."""

    class Role(models.TextChoices):
        SIGNER = "SIGNER", "Signer"
        APPROVER = "APPROVER", "Approver"
        WITNESS = "WITNESS", "Witness"
        CC = "CC", "CC"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SENT = "SENT", "Sent"
        VIEWED = "VIEWED", "Viewed"
        SIGNED = "SIGNED", "Signed"
        DECLINED = "DECLINED", "Declined"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    envelope = models.ForeignKey(
        EsignEnvelope, on_delete=models.CASCADE, related_name="signers"
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="esign_signatures",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="esign_signatures",
    )
    name = models.CharField(max_length=200)
    email = models.EmailField()
    signing_order = models.IntegerField(default=1)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.SIGNER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    signed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "collaboration_esign_signer"
        ordering = ["envelope", "signing_order"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(contact__isnull=False) | models.Q(user__isnull=False),
                name="collaboration_esign_signer_has_identity",
            )
        ]


class SignatureEvent(AppendOnlyModel):
    """The legal audit trail of a signature flow.

    Append-only, enforced by a database-role REVOKE (collaboration/0002). This is the evidence
    that a particular person, at a particular IP, saw and signed a particular document — if it
    can be edited afterwards it proves nothing.
    """

    class EventType(models.TextChoices):
        CREATED = "CREATED", "Created"
        SENT = "SENT", "Sent"
        VIEWED = "VIEWED", "Viewed"
        SIGNED = "SIGNED", "Signed"
        DECLINED = "DECLINED", "Declined"
        COMPLETED = "COMPLETED", "Completed"
        VOIDED = "VOIDED", "Voided"
        REMINDER_SENT = "REMINDER_SENT", "Reminder sent"

    envelope = models.ForeignKey(
        EsignEnvelope, on_delete=models.PROTECT, related_name="signature_events"
    )
    signer = models.ForeignKey(
        EsignSigner,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="signature_events",
    )
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "collaboration_signature_event"
