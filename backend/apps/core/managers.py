"""Model managers for tenant-aware models (plan §2.1)."""
from django.db import models

from .tenancy import get_current_tenant


class TenantQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)


class TenantManager(models.Manager):
    """
    Default manager for TenantAwareModel.

    - Filters to the current tenant when one is bound (convenience + defense in depth).
    - Hides soft-deleted rows by default.
    RLS remains the authoritative guard at the database layer.
    """

    def get_queryset(self):
        qs = TenantQuerySet(self.model, using=self._db).filter(is_deleted=False)
        tenant_id = get_current_tenant()
        if tenant_id is not None:
            qs = qs.filter(tenant_id=tenant_id)
        return qs

    def with_deleted(self):
        qs = TenantQuerySet(self.model, using=self._db)
        tenant_id = get_current_tenant()
        if tenant_id is not None:
            qs = qs.filter(tenant_id=tenant_id)
        return qs


class UnscopedManager(models.Manager):
    """Escape hatch — bypasses the tenant/soft-delete filter (admin, migrations)."""

    def get_queryset(self):
        return models.QuerySet(self.model, using=self._db)
