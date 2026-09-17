"""DRF permission classes for the identity API.

These answer "may this request proceed at all?". They deliberately do NOT decide which rows
come back — that is `selectors.apply_scope` — nor whether a specific role may be granted,
which is `services.grantable_role_codes`, so the rule holds for a shell session too.
"""
from rest_framework.permissions import BasePermission

from ..services import grantable_role_codes


class PasswordIsCurrent(BasePermission):
    """Confine a user with a forced password change to the endpoints that let them fix it.

    Registration has the registrar choose the initial password, so until it is replaced the
    account's secret is known to someone else. Authenticating is allowed — otherwise they
    could never change it — but nothing else is, so a leaked initial password cannot be used
    to read the CRM.

    Views opt out by setting ``allow_stale_password = True`` (i.e. /me/ and change-password).
    """

    message = (
        "You must change your password before using the API. "
        "POST your current and new password to /api/v1/auth/change-password/."
    )

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if getattr(view, "allow_stale_password", False):
            return True
        return not user.must_change_password


class CanRegisterUsers(BasePermission):
    """May the requester grant any role at all?

    A coarse gate that keeps the endpoint closed to agents and portal users. Whether they may
    grant the *specific* role in the request body is decided by `services.register_user`,
    which raises PermissionDenied — so this class does not need the request body.
    """

    message = "Your role does not permit registering users."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return bool(grantable_role_codes(user))
