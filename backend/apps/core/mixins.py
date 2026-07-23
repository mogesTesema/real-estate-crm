"""
Reusable viewset building blocks (plan §2.2, §2.4).

Every domain viewset extends BaseTenantViewSet, which:
  - binds the request user's tenant to the DB session (RLS),
  - applies role-based row-level scoping,
  - stamps tenant on create.
"""
from rest_framework import viewsets

from .permissions import IsAuthenticatedInTenant, RolePermission, visibility_scope
from .tenancy import set_current_tenant


class ScopedQuerySetMixin:
    """
    Narrows the queryset to the rows the user is allowed to see.

    Viewsets declare how ownership maps onto their model:
      scope_owner_field  — FK path to the owning user (e.g. "assigned_agent").
      scope_branch_field — path to the owning user's branch (e.g. "assigned_agent__branch").
    When scope_owner_field is None the model is treated as shared tenant inventory
    (e.g. properties) and only tenant isolation applies.
    """

    scope_owner_field: str | None = None
    scope_branch_field: str | None = None

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if user.is_superuser:
            return qs
        scope = visibility_scope(user)
        if scope == "all" or self.scope_owner_field is None:
            return qs
        if scope == "branch" and self.scope_branch_field and user.branch_id:
            return qs.filter(**{self.scope_branch_field: user.branch_id})
        # default: own records only
        return qs.filter(**{self.scope_owner_field: user})


class BaseTenantViewSet(ScopedQuerySetMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticatedInTenant, RolePermission]
    admin_write = False

    def initial(self, request, *args, **kwargs):
        # request.user triggers JWT auth (lazy); bind tenant for RLS before the handler.
        super().initial(request, *args, **kwargs)
        tenant_id = getattr(request.user, "tenant_id", None)
        if tenant_id is not None:
            set_current_tenant(tenant_id)

    def perform_create(self, serializer):
        serializer.save(tenant_id=self.request.user.tenant_id)

    def perform_destroy(self, instance):
        # Soft delete (plan §2.6) instead of hard DELETE.
        instance.soft_delete()
