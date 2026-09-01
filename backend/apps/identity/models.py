"""Company, org hierarchy, users, RBAC, portal profiles (architecture.md §4)."""
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from apps.core.choices import ScopedEntityType
from apps.core.models import BaseModel


class Company(models.Model):
    class FxMode(models.TextChoices):
        SINGLE_CURRENCY = "SINGLE_CURRENCY", "Single currency"
        MULTI_CURRENCY = "MULTI_CURRENCY", "Multi currency"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200)
    registration_number = models.CharField(max_length=100, null=True, blank=True)
    tax_number = models.CharField(max_length=100, null=True, blank=True)
    # logo_file_id -> collaboration.File: deferred FK pass (collaboration built later).
    logo_file_id = models.UUIDField(null=True, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=30)
    website = models.URLField(null=True, blank=True)
    country = models.CharField(max_length=100)
    timezone = models.CharField(max_length=64)
    default_currency = models.CharField(max_length=3)
    fx_mode = models.CharField(
        max_length=20, choices=FxMode.choices, default=FxMode.SINGLE_CURRENCY
    )
    address_line_1 = models.CharField(max_length=200, null=True, blank=True)
    address_line_2 = models.CharField(max_length=200, null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True)
    state = models.CharField(max_length=100, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "identity_company"

    def __str__(self):
        return self.name


class Branch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, unique=True)
    manager = models.ForeignKey(
        "identity.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="managed_branches",
    )
    phone = models.CharField(max_length=30, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "identity_branch"

    def __str__(self):
        return self.name


class Team(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, null=True, blank=True)
    manager = models.ForeignKey(
        "identity.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="managed_teams",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "identity_team"

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """architecture.md §4: no created_by/updated_by in the spec's field list
    for identity_user, so this does not subclass BaseModel."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    phone = models.CharField(max_length=30, null=True, blank=True)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    # avatar_file_id -> collaboration.File: deferred FK pass.
    avatar_file_id = models.UUIDField(null=True, blank=True)
    employee_number = models.CharField(max_length=50, null=True, blank=True)
    job_title = models.CharField(max_length=150, null=True, blank=True)
    branch = models.ForeignKey(
        Branch, null=True, blank=True, on_delete=models.SET_NULL, related_name="users"
    )
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="users"
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        db_table = "identity_user"
        constraints = [
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(deleted_at__isnull=True),
                name="identity_user_email_uniq",
            )
        ]

    def __str__(self):
        return self.email

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class Role(models.Model):
    class DataScope(models.TextChoices):
        ALL = "ALL", "All"
        BRANCH = "BRANCH", "Branch"
        TEAM = "TEAM", "Team"
        OWN = "OWN", "Own"
        MANAGED_PROPERTIES = "MANAGED_PROPERTIES", "Managed properties"
        FINANCE_ALL = "FINANCE_ALL", "Finance (all)"
        MARKETING_ALL = "MARKETING_ALL", "Marketing (all)"
        PORTAL_OWN = "PORTAL_OWN", "Portal (own)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50, unique=True)
    description = models.TextField(null=True, blank=True)
    is_system_role = models.BooleanField(default=False)
    data_scope = models.CharField(max_length=30, choices=DataScope.choices)

    class Meta:
        db_table = "identity_role"

    def __str__(self):
        return self.code


class Permission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=150, unique=True)
    module = models.CharField(max_length=100)
    action = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "identity_permission"

    def __str__(self):
        return self.code


class UserRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="user_roles")

    class Meta:
        db_table = "identity_user_role"
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="identity_user_role_uniq")
        ]


class RolePermission(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="role_permissions")
    permission = models.ForeignKey(
        Permission, on_delete=models.CASCADE, related_name="role_permissions"
    )

    class Meta:
        db_table = "identity_role_permission"
        constraints = [
            models.UniqueConstraint(
                fields=["role", "permission"], name="identity_role_permission_uniq"
            )
        ]


class PortalProfile(models.Model):
    class PortalType(models.TextChoices):
        BUYER = "BUYER", "Buyer"
        SELLER = "SELLER", "Seller"
        TENANT = "TENANT", "Tenant"
        LANDLORD = "LANDLORD", "Landlord"

    class EligibilityStatus(models.TextChoices):
        PENDING_CONTRACT = "PENDING_CONTRACT", "Pending contract"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"

    class ContractRefType(models.TextChoices):
        TRANSACTION = "TRANSACTION", "Transaction"
        LEASE = "LEASE", "Lease"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="portal_profile")
    # contact_id -> contacts.Contact: forward reference (identity built before
    # contacts). Deferred FK pass promotes this to a real ForeignKey.
    contact_id = models.UUIDField(unique=True)
    portal_type = models.CharField(max_length=20, choices=PortalType.choices)
    eligibility_status = models.CharField(
        max_length=20,
        choices=EligibilityStatus.choices,
        default=EligibilityStatus.PENDING_CONTRACT,
    )
    completed_contract_ref_type = models.CharField(
        max_length=20, choices=ContractRefType.choices, null=True, blank=True
    )
    completed_contract_ref_id = models.UUIDField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    last_access_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "identity_portal_profile"

    def __str__(self):
        return f"{self.portal_type} portal for {self.user_id}"


class RecordShare(models.Model):
    class AccessLevel(models.TextChoices):
        VIEW = "VIEW", "View"
        EDIT = "EDIT", "Edit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity_type = models.CharField(max_length=20, choices=ScopedEntityType.choices)
    entity_id = models.UUIDField()
    shared_with_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="record_shares_received"
    )
    access_level = models.CharField(max_length=10, choices=AccessLevel.choices)
    shared_by = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="record_shares_given"
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "identity_record_share"
        constraints = [
            models.UniqueConstraint(
                fields=["entity_type", "entity_id", "shared_with_user"],
                name="identity_record_share_uniq",
            )
        ]


class FieldPermission(models.Model):
    class AccessLevel(models.TextChoices):
        HIDDEN = "HIDDEN", "Hidden"
        READ_ONLY = "READ_ONLY", "Read only"
        READ_WRITE = "READ_WRITE", "Read/write"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="field_permissions")
    entity_type = models.CharField(max_length=20, choices=ScopedEntityType.choices)
    field_name = models.CharField(max_length=100)
    access_level = models.CharField(max_length=10, choices=AccessLevel.choices)

    class Meta:
        db_table = "identity_field_permission"
        constraints = [
            models.UniqueConstraint(
                fields=["role", "entity_type", "field_name"],
                name="identity_field_permission_uniq",
            )
        ]
