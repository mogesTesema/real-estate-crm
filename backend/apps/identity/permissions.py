"""Function-level access control — "may this role use this feature at all?"

The companion to `identity.scoping`, and a **different question**. architecture.md §2 opens by
separating them: function-level RBAC answers *"can this role use this feature?"*, row scoping
answers *"which records may this user see?"*. Building only the second leaves every endpoint
open to every authenticated account, which is how a rental tenant ends up able to rewrite the
lead routing rules — scoping never had an opinion about that, because routing rules are not
rows anybody owns.

Lives outside `identity/api/` on purpose: §1.2 makes every app's `api` package private, so a
permission class other apps' views must use cannot live in one. Same reasoning as
`ScopedQuerysetMixin`.

**What this is not.** `identity_permission` / `identity_role_permission` (§4) are the full
per-action matrix the SRS role definitions describe, and they are still unseeded. These
classes key off `data_scope`, which every role already carries and which scoping already
trusts, so there is no second source of truth to drift. They are the floor, not the ceiling:
the finer matrix refines them later without contradicting them.
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role
from .scoping import scopes_for


def is_portal_client(user) -> bool:
    """True when this account exists only to serve one client their own contracts.

    Defined by what the account *can* be rather than by its role code, so a custom role with
    `data_scope = PORTAL_OWN` is treated as a client too — §4 wants custom roles to work
    without hardcoded role-name switches.

    A user holding PORTAL_OWN *and* a staff scope is staff: that combination has no meaning
    today, but reading it as "client" would silently lock out an employee who is also a
    tenant of the company, which is a real person in a real brokerage.
    """
    scopes = scopes_for(user)
    return bool(scopes) and scopes == {Role.DataScope.PORTAL_OWN}


def is_agency_admin(user) -> bool:
    """Agency-wide authority: `super_admin` and `owner` by seed, or any role scoped ALL."""
    return Role.DataScope.ALL in scopes_for(user)


class StaffWrite(BasePermission):
    """Reads are left to row scoping; **writes require staff**.

    A `DEFAULT_PERMISSION_CLASS`, not an opt-in. The failure this prevents is not a wrong rule
    on one endpoint — it is a new endpoint added in a later pass that nobody remembers to
    protect, which is exactly how `POST /routing-rules/` came to accept a rental tenant.
    Fail closed, and let a view that genuinely serves client writes say so.

    Reads stay open because `apply_scope` already decides what a client may see, and a portal
    client reading their own lease through the ordinary lease endpoint is the portal working.
    Endpoints whose *reads* are staff tools — the duplicate probe, exports, dashboards —
    add `IsStaff` on top.

    A view opts out with ``portal_writable = True`` (self-service: change-password, logout).
    """

    message = "Portal clients have read-only access to their own records."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        if getattr(view, "portal_writable", False):
            return True
        return not is_portal_client(user)


class IsStaff(BasePermission):
    """Closed to portal clients entirely, reads included.

    For surfaces that are staff tools rather than records: the duplicate probe (which reports
    the *existence* of contacts outside the caller's scope by design — safe between
    colleagues, a customer enumerating the company's book otherwise), bulk export, and the
    analytics dashboards.
    """

    message = "This endpoint is not available to portal clients."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return not is_portal_client(user)


class IsAgencyAdmin(BasePermission):
    """Agency configuration — rules that decide what happens to everybody's records.

    A lead routing rule is not a row with an owner, so row scoping has nothing to say about
    it; without this, any authenticated account could write a priority-0 catch-all sending
    every inbound lead to itself. Reads stay open to staff so an agent can see why their leads
    are landing where they are.
    """

    message = "Only agency administrators may change this configuration."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if is_portal_client(user):
            return False
        if request.method in SAFE_METHODS:
            return True
        return bool(user.is_superuser or is_agency_admin(user))
