"""Binds the authenticated user's tenant to the request + DB session (plan §2.1)."""
from .tenancy import clear_current_tenant, set_current_tenant


class TenantMiddleware:
    """
    Runs after AuthenticationMiddleware. For DRF (JWT) the user is resolved inside
    the view, so we also re-bind lazily; here we bind from the session user when
    present and always clear on the way out to avoid leaking context across
    pooled connections.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = None
        user = getattr(request, "user", None)
        tenant_id = getattr(user, "tenant_id", None) if user else None
        if tenant_id is not None:
            token = set_current_tenant(tenant_id)
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant(token)
