"""
RBAC + row-level visibility scope (plan §2.2).

Two questions, kept separate:
  - "What actions may this role take?"  -> RolePermission (DRF permission class)
  - "Whose records may they see?"       -> visibility_scope() + ScopedQuerySetMixin
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role

# Roles that may see everything within their tenant.
_TENANT_WIDE = {Role.SUPER_ADMIN, Role.OWNER, Role.FINANCE, Role.MARKETING}
# Roles that see their branch.
_BRANCH_WIDE = {Role.MANAGER}
# Everyone else (agents, property managers) sees only their own records.

# Roles allowed to perform destructive/admin writes on configuration objects.
_ADMIN_ROLES = {Role.SUPER_ADMIN, Role.OWNER}


def visibility_scope(user) -> str:
    """Return one of: 'all' | 'branch' | 'team' | 'own'."""
    role = getattr(user, "role", None)
    if role in _TENANT_WIDE:
        return "all"
    if role in _BRANCH_WIDE:
        return "branch"
    return "own"


class IsAuthenticatedInTenant(BasePermission):
    """Authenticated and attached to a tenant (superusers exempt from tenant req)."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return bool(user.tenant_id) or user.is_superuser


class RolePermission(BasePermission):
    """
    Object/config-level write gate. Read is open to any authenticated tenant user;
    writes to admin-only viewsets require an admin role. Viewsets opt in by setting
    ``admin_write = True``.
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if getattr(view, "admin_write", False):
            return getattr(request.user, "role", None) in _ADMIN_ROLES
        return True
