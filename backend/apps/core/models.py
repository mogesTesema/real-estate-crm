"""
Core models: tenancy root, org hierarchy, users, custom-field metadata, audit.

See implimentation-plan.md §2 (architecture) and §3 (data model).
"""
import uuid

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils import timezone

from .managers import TenantManager, UnscopedManager
from .tenancy import get_current_tenant


# --------------------------------------------------------------------------- #
# Abstract bases
# --------------------------------------------------------------------------- #
class TimeStampedModel(models.Model):
    """UUID pk + timestamps + soft delete (plan §2.6)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at", "updated_at"])


class CustomFieldsMixin(models.Model):
    """Tenant-defined extension fields without migrations (plan §2.3)."""

    custom_fields = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True


class TenantAwareModel(TimeStampedModel):
    """Every business record hangs off a tenant and is isolated by RLS."""

    tenant = models.ForeignKey(
        "core.Tenant", on_delete=models.CASCADE, related_name="+", db_index=True
    )

    objects = TenantManager()
    all_objects = UnscopedManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.tenant_id is None:
            current = get_current_tenant()
            if current is not None:
                self.tenant_id = current
        super().save(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Tenancy root + org hierarchy
# --------------------------------------------------------------------------- #
class Tenant(TimeStampedModel):
    """A SaaS customer (brokerage). Root of isolation; not itself tenant-scoped."""

    name = models.CharField(max_length=255)
    subdomain = models.SlugField(max_length=63, unique=True)
    plan = models.CharField(max_length=50, default="mvp")
    enabled_modules = models.JSONField(default=list, blank=True)
    default_currency = models.CharField(max_length=3, default="USD")
    default_locale = models.CharField(max_length=10, default="en-us")
    timezone = models.CharField(max_length=64, default="UTC")

    objects = models.Manager()

    def __str__(self):
        return self.name


class Company(TenantAwareModel):
    """A brand/legal entity within a tenant (franchises run several)."""

    name = models.CharField(max_length=255)

    class Meta:
        verbose_name_plural = "companies"

    def __str__(self):
        return self.name


class Branch(TenantAwareModel):
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="branches"
    )
    name = models.CharField(max_length=255)

    class Meta:
        verbose_name_plural = "branches"

    def __str__(self):
        return self.name


class Team(TenantAwareModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


# --------------------------------------------------------------------------- #
# Users + roles (RBAC — plan §2.2, SRS §2.3)
# --------------------------------------------------------------------------- #
class Role(models.TextChoices):
    SUPER_ADMIN = "super_admin", "Super Admin"
    OWNER = "owner", "Broker / Agency Owner"
    MANAGER = "manager", "Branch / Team Manager"
    AGENT = "agent", "Sales / Leasing Agent"
    PROPERTY_MANAGER = "property_manager", "Property Manager"
    MARKETING = "marketing", "Marketing Staff"
    FINANCE = "finance", "Finance / Accounts"
    PORTAL = "portal", "Portal User"


class UserManager(BaseUserManager):
    """Not tenant-filtered — authentication must resolve users across tenants."""

    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Users must have an email address")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", Role.SUPER_ADMIN)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user. Email is the identifier and is globally unique (MVP constraint,
    plan §2.1). Carries tenant + org scope used by the permission layer.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, blank=True)

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="users", null=True, blank=True
    )
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.AGENT)
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )
    team = models.ForeignKey(
        Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="members"
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    mfa_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.email


# --------------------------------------------------------------------------- #
# Custom-field metadata (plan §2.3)
# --------------------------------------------------------------------------- #
class FieldDefinition(TenantAwareModel):
    class FieldType(models.TextChoices):
        TEXT = "text", "Text"
        NUMBER = "number", "Number"
        BOOLEAN = "boolean", "Boolean"
        DATE = "date", "Date"
        SELECT = "select", "Select (single)"
        MULTISELECT = "multiselect", "Select (multiple)"

    target_model = models.CharField(
        max_length=64, help_text="e.g. 'contacts.Contact'"
    )
    key = models.SlugField(max_length=64)
    label = models.CharField(max_length=255)
    field_type = models.CharField(max_length=16, choices=FieldType.choices)
    options = models.JSONField(default=list, blank=True)
    required = models.BooleanField(default=False)

    class Meta:
        unique_together = ("tenant", "target_model", "key")

    def __str__(self):
        return f"{self.target_model}.{self.key}"


# --------------------------------------------------------------------------- #
# Audit log (SRS §3.17.3). Not under RLS so system actions are always writable.
# --------------------------------------------------------------------------- #
class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    model_label = models.CharField(max_length=128)
    object_id = models.CharField(max_length=64)
    action = models.CharField(max_length=16, choices=Action.choices)
    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    changes = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["model_label", "object_id"]),
            models.Index(fields=["tenant", "at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.model_label}:{self.object_id}"
