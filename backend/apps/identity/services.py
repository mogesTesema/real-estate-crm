"""Public write API for `identity` (architecture.md §1.2).

Company, branches, teams, users, RBAC, portal profiles.

This is the ONLY module another app may import to mutate `identity`-owned rows. Calling
`Model.objects.create/update/delete` on an `identity` model from another app is a forbidden
pattern, as is reacting to `post_save` signals to do it. The API layer in `api/` calls these
functions and performs no ORM mutation of its own.

Every function takes an `actor` — the user performing the action — and raises
`PermissionDenied` rather than silently narrowing what it does. Authorisation lives here, not
in the serializers, so it holds for a management command or a shell session too.
"""
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import models, transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from . import signals
from .models import PortalProfile, Role, Team, User, UserRole

# --- Registration authority ---------------------------------------------------------------
#
# Taken verbatim from the source documents, not inferred:
#
#   SRS 3.15.2 — "The System shall support hierarchical registration: Super Admin →
#   Branch/Team Manager → Broker/Agency Owner → Sales/Leasing Agent. Branch/Team Managers
#   register Broker/Agency Owners; Broker/Agency Owners register Sales/Leasing Agents."
#
#   SRS 3.15.1 — Super Admin is the highest authority for user governance, and
#   Role Definitions §1 — "Create/deactivate users in the company, assign roles; override
#   branch/owner registrations when needed."
#
# A manager registering an owner reads oddly, since `owner` holds the broader data_scope. But
# registration authority and data scope are different axes: Role Definitions §2 makes the
# manager the branch's onboarding administrator ("Owner Registration — Register/approve
# Broker/Agency Owners under this branch/team") while the owner is the person with
# agency-wide visibility.
#
# Neither document grants anyone but Super Admin the right to create property_manager,
# marketing or finance. That silence is read as "Super Admin only", since 3.15.1 gives user
# governance to Super Admin and nothing delegates these three onward.

#: The internal roles that may be created through the registration endpoint.
STAFF_ROLE_CODES = frozenset(
    {
        Role.SUPER_ADMIN,
        Role.OWNER,
        Role.MANAGER,
        Role.AGENT,
        Role.PROPERTY_MANAGER,
        Role.MARKETING,
        Role.FINANCE,
    }
)

#: actor role code -> role codes that actor may grant.
REGISTRATION_AUTHORITY: dict[str, frozenset[str]] = {
    Role.SUPER_ADMIN: STAFF_ROLE_CODES,
    Role.MANAGER: frozenset({Role.OWNER}),
    Role.OWNER: frozenset({Role.AGENT}),
}

#: Roles whose holders may only place new users inside their own branch.
BRANCH_BOUND_REGISTRARS = frozenset({Role.MANAGER})

PORTAL_REFUSAL = (
    "The 'portal' role cannot be granted through staff registration. A portal user is an "
    "external client whose access is gated on a completed contract — use "
    "POST /api/v1/portal-users/, which verifies that contract before creating the login."
)

# --- Portal invitation authority -----------------------------------------------------------
#
# Role Definitions §4 (Sales/Leasing Agent) — "Client Portal | Invite portal access only for
# clients with completed contracts." §5 (Property Manager) — "Tenant Management | Onboard
# tenants" and "Landlord Reporting". So each role invites the clients it actually works with;
# marketing and finance work with neither and invite nobody.

#: actor role code -> portal types that actor may invite. None means "any type".
PORTAL_INVITE_AUTHORITY: dict[str, frozenset[str] | None] = {
    Role.SUPER_ADMIN: None,
    Role.OWNER: None,
    Role.MANAGER: None,
    Role.AGENT: frozenset({"BUYER", "SELLER"}),
    Role.PROPERTY_MANAGER: frozenset({"TENANT", "LANDLORD"}),
}


def role_codes_of(user) -> set[str]:
    """The role codes a user holds."""
    return set(
        Role.objects.filter(user_roles__user=user).values_list("code", flat=True)
    )


def grantable_role_codes(actor) -> set[str]:
    """Which roles `actor` may grant — the union across every role they hold.

    A user holding both `manager` and `owner` may grant what either allows; there is no
    precedence to resolve because the matrix is additive.
    """
    if actor is None or not actor.is_authenticated:
        return set()
    grantable: set[str] = set()
    for code in role_codes_of(actor):
        grantable |= REGISTRATION_AUTHORITY.get(code, frozenset())
    # Django's superuser flag is the bootstrap escape hatch: `createsuperuser` sets it, and
    # it must work before any role exists.
    if actor.is_superuser:
        grantable |= set(STAFF_ROLE_CODES)
    return grantable


def _assert_may_grant(actor, role_code: str) -> None:
    if role_code == Role.PORTAL:
        raise PermissionDenied(PORTAL_REFUSAL)
    if role_code not in grantable_role_codes(actor):
        raise PermissionDenied(
            f"Your role does not permit granting '{role_code}'."
        )


def _resolve_branch(actor, branch):
    """Apply the branch restriction that binds some registrars to their own branch.

    Without this a manager could pass any branch id and staff a branch they do not run.
    """
    if not (role_codes_of(actor) & BRANCH_BOUND_REGISTRARS) or actor.is_superuser:
        return branch

    if actor.branch_id is None:
        raise PermissionDenied(
            "You must belong to a branch before you can register users into one."
        )
    if branch is not None and branch.pk != actor.branch_id:
        raise PermissionDenied("You may only register users into your own branch.")
    return branch if branch is not None else actor.branch


#: Scopes that are derived from a user's org position, so a holder without one sees nothing.
_PLACEMENT_REQUIRED_SCOPES = frozenset(
    {Role.DataScope.BRANCH, Role.DataScope.TEAM}
)


def _validate_placement(branch, team, role: Role) -> None:
    if team is not None:
        if branch is None:
            raise ValidationError({"branch": "A team requires the matching branch."})
        if team.branch_id != branch.pk:
            raise ValidationError(
                {"team": "That team belongs to a different branch."}
            )
    if branch is None and role.data_scope in _PLACEMENT_REQUIRED_SCOPES:
        # A branch-scoped role with no branch resolves to "sees only themselves" — a user
        # who looks like a manager and can do nothing. Refuse it at creation rather than
        # shipping a confusing account.
        raise ValidationError(
            {
                "branch": (
                    f"The '{role.code}' role is scoped to a "
                    f"{role.data_scope.lower()}, so a branch is required."
                )
            }
        )


@transaction.atomic
def register_user(
    *,
    actor,
    email: str,
    first_name: str,
    last_name: str,
    password: str,
    role_code: str,
    branch=None,
    team=None,
    **profile,
) -> User:
    """Register a staff member and grant them their role.

    Order matters: authority first (so an unauthorised caller learns nothing about whether
    the email is taken), then placement, then the password.
    """
    _assert_may_grant(actor, role_code)
    role = Role.objects.filter(code=role_code).first()
    if role is None:
        raise ValidationError({"role_code": f"Unknown role '{role_code}'."})

    branch = _resolve_branch(actor, branch)
    if team is not None and branch is None:
        branch = team.branch
    _validate_placement(branch, team, role)

    email = User.objects.normalize_email(email).lower()
    if User.objects.filter(email__iexact=email, deleted_at__isnull=True).exists():
        raise ValidationError({"email": "A user with that email already exists."})

    user = User(
        email=email,
        first_name=first_name,
        last_name=last_name,
        branch=branch,
        team=team,
        must_change_password=True,
        **profile,
    )
    # Validate against AUTH_PASSWORD_VALIDATORS with the user attached, so the
    # similarity check can compare the password against their name and email.
    validate_password(password, user)
    user.set_password(password)
    user.save()

    UserRole.objects.create(user=user, role=role)

    signals.user_registered.send(sender=None, user=user, actor=actor, role_code=role_code)
    return user


def _assert_not_self(actor, user: User, action: str) -> None:
    """Nobody changes their own roles.

    Without this the matrix is trivially escapable by its own rules: a manager may grant
    `owner`, so a manager grants *themselves* `owner` and jumps from BRANCH to ALL scope in
    one call. Revoking is barred too, so nobody can lock themselves out of their own account.
    Use a second administrator, or the shell.
    """
    if actor is not None and user.pk == actor.pk:
        raise PermissionDenied(f"You cannot {action} your own account.")


@transaction.atomic
def assign_role(*, actor, user: User, role_code: str) -> UserRole:
    """Grant an additional role.

    Runs the *same* authority check as registration. Without that, this endpoint would be a
    trivial way around the matrix: register nobody, just grant yourself `owner`.
    """
    _assert_not_self(actor, user, "change the roles on")
    _assert_may_grant(actor, role_code)
    role = Role.objects.filter(code=role_code).first()
    if role is None:
        raise ValidationError({"role_code": f"Unknown role '{role_code}'."})

    user_role, created = UserRole.objects.get_or_create(user=user, role=role)
    if created:
        signals.role_assigned.send(
            sender=None, user=user, actor=actor, role_code=role_code
        )
    return user_role


@transaction.atomic
def revoke_role(*, actor, user: User, role_code: str) -> None:
    """Remove a role. Revoking the last super_admin is refused."""
    _assert_not_self(actor, user, "change the roles on")
    _assert_may_grant(actor, role_code)
    if role_code == Role.SUPER_ADMIN:
        _assert_not_last_super_admin(user, "revoke the last super_admin role")
    removed, _ = UserRole.objects.filter(user=user, role__code=role_code).delete()
    if removed:
        signals.role_revoked.send(sender=None, user=user, actor=actor, role_code=role_code)


def _is_administrator(user: User) -> bool:
    """Counts both routes to highest authority.

    `is_superuser` is a Django flag and the `super_admin` role is this system's concept; they
    are set independently. An account created by `createsuperuser` before the role seed, or
    flagged from Django admin, holds authority without the role — and is exactly the account
    most likely to be the last one standing.
    """
    return (
        user.is_superuser
        or UserRole.objects.filter(user=user, role__code=Role.SUPER_ADMIN).exists()
    )


def _assert_not_last_super_admin(user: User, action: str) -> None:
    """Refuse an action that would leave nobody able to administer users."""
    if not _is_administrator(user):
        return
    remaining = (
        User.objects.filter(is_active=True, deleted_at__isnull=True)
        .exclude(pk=user.pk)
        .filter(
            models.Q(is_superuser=True)
            | models.Q(user_roles__role__code=Role.SUPER_ADMIN)
        )
        .exists()
    )
    if not remaining:
        raise PermissionDenied(
            f"Refusing to {action}: no other active administrator would remain, and nobody "
            "could administer users."
        )


@transaction.atomic
def deactivate_user(*, actor, user: User) -> None:
    """Revoke access without destroying the record.

    Sets `is_active=False` and nothing else. It never hard-deletes — roughly thirty PROTECT
    foreign keys point at User, so `.delete()` would raise — and it deliberately does not set
    `deleted_at`, which would release the address for reuse under the partial unique index
    and make old audit rows read ambiguously. Freeing the address is a separate, more
    deliberate act.
    """
    if not grantable_role_codes(actor):
        raise PermissionDenied("Your role does not permit deactivating users.")
    if user.pk == actor.pk:
        raise PermissionDenied("You cannot deactivate your own account.")
    _assert_not_last_super_admin(user, "deactivate the last super_admin")

    if user.is_active:
        user.is_active = False
        user.save(update_fields=["is_active", "updated_at"])
        signals.user_deactivated.send(sender=None, user=user, actor=actor)


@transaction.atomic
def reactivate_user(*, actor, user: User) -> None:
    """Undo a deactivation.

    Without this, a mis-click is unrecoverable through the API: the row still holds the email
    address, so re-registering the same person collides with the partial unique index.
    """
    if not grantable_role_codes(actor):
        raise PermissionDenied("Your role does not permit reactivating users.")
    if user.deleted_at is not None:
        raise ValidationError(
            {"detail": "That account was deleted, not deactivated, and cannot be restored here."}
        )

    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active", "updated_at"])
        signals.user_reactivated.send(sender=None, user=user, actor=actor)


# --- Portal access -------------------------------------------------------------------------


def invitable_portal_types(actor) -> set[str]:
    """Which portal types `actor` may invite — the union across the roles they hold."""
    if actor is None or not actor.is_authenticated:
        return set()
    all_types = {choice for choice, _ in PortalProfile.PortalType.choices}
    if actor.is_superuser:
        return all_types

    allowed: set[str] = set()
    for code in role_codes_of(actor):
        if code not in PORTAL_INVITE_AUTHORITY:
            continue
        permitted = PORTAL_INVITE_AUTHORITY[code]
        if permitted is None:
            return all_types
        allowed |= set(permitted)
    return allowed


def _confirm_eligibility(*, contact_id, portal_type, ref_type, ref_id) -> dict:
    """Ask the app that owns the contract whether this client may have a portal login.

    `identity` cannot read `crm` or `property_ops` (architecture.md §1.2), so it asks and they
    answer — see `apps/identity/signals.py`.

    **Fails closed.** Silence is a refusal: an uninstalled app, a contract that does not
    exist, a contract in the wrong state, and a contact unconnected to it are indistinguishable
    from here, and all of them mean "no".
    """
    responses = signals.verify_portal_eligibility.send(
        sender=None,
        contact_id=contact_id,
        portal_type=portal_type,
        contract_ref_type=ref_type,
        contract_ref_id=ref_id,
    )
    for _receiver, response in responses:
        if response and response.get("eligible"):
            return response

    raise ValidationError(
        {
            "contract_ref_id": (
                "No completed contract confirms this client. Portal access requires a closed "
                "transaction or a signed, active lease that this contact is party to "
                "(SRS 3.11.2); an open lead does not qualify."
            )
        }
    )


@transaction.atomic
def grant_portal_access(
    *,
    actor,
    contact_id,
    portal_type: str,
    contract_ref_type: str,
    contract_ref_id,
    password: str,
) -> User:
    """Create a portal login for a client who holds a completed contract (SRS 3.11.2).

    Order matters: authority, then eligibility, then the account. An actor who may not invite
    this client type learns nothing about whether the contract exists.
    """
    if portal_type not in invitable_portal_types(actor):
        raise PermissionDenied(
            f"Your role does not permit inviting '{portal_type}' portal clients."
        )

    confirmation = _confirm_eligibility(
        contact_id=contact_id,
        portal_type=portal_type,
        ref_type=contract_ref_type,
        ref_id=contract_ref_id,
    )

    if PortalProfile.objects.filter(contact_id=contact_id).exists():
        raise ValidationError(
            {"contact_id": "That client already has portal access."}
        )

    email = (confirmation.get("email") or "").strip().lower()
    if not email:
        raise ValidationError(
            {
                "contact_id": (
                    "That contact has no email address, so there is nothing to log in with. "
                    "Add one to the contact first."
                )
            }
        )
    if User.objects.filter(email__iexact=email, deleted_at__isnull=True).exists():
        raise ValidationError({"contact_id": f"A user already exists for {email}."})

    user = User(
        email=email,
        first_name=confirmation.get("first_name") or "",
        last_name=confirmation.get("last_name") or "",
        must_change_password=True,
    )
    validate_password(password, user)
    user.set_password(password)
    user.save()

    role = Role.objects.filter(code=Role.PORTAL).first()
    if role is None:  # pragma: no cover - seeded by identity/0002
        raise ValidationError({"detail": "The 'portal' role is missing from this database."})
    UserRole.objects.create(user=user, role=role)

    profile = PortalProfile.objects.create(
        user=user,
        contact_id=contact_id,
        portal_type=portal_type,
        eligibility_status=PortalProfile.EligibilityStatus.ACTIVE,
        completed_contract_ref_type=contract_ref_type,
        completed_contract_ref_id=contract_ref_id,
        is_verified=True,
    )

    signals.portal_access_granted.send(
        sender=None, user=user, actor=actor, portal_profile=profile
    )
    return user


@transaction.atomic
def revoke_portal_access(*, actor, portal_profile, reason: str = "") -> None:
    """Suspend a client's portal access — when a lease ends, say.

    Suspends rather than deletes: `last_access_at` and the contract reference are history, and
    SRS 5.3 wants that history to survive. Reinstating is then a status change, not a rebuild.
    """
    if not invitable_portal_types(actor):
        raise PermissionDenied("Your role does not permit revoking portal access.")

    portal_profile.eligibility_status = PortalProfile.EligibilityStatus.SUSPENDED
    portal_profile.save(update_fields=["eligibility_status"])

    signals.portal_access_revoked.send(
        sender=None,
        user=portal_profile.user,
        actor=actor,
        portal_profile=portal_profile,
        reason=reason,
    )


def portal_login_blocked_reason(user: User) -> str | None:
    """Why this user may not log in, or None.

    A portal client's right to be here expires with their contract, so it is checked at every
    login rather than only at invitation (SRS 3.11.2).
    """
    profile = PortalProfile.objects.filter(user=user).first()
    if profile is None:
        return None
    if profile.eligibility_status != PortalProfile.EligibilityStatus.ACTIVE:
        return (
            "Portal access for this account is not active. It is granted while a contract "
            "is in force; contact your agent or property manager."
        )
    return None


@transaction.atomic
def change_password(*, user: User, current_password: str, new_password: str) -> None:
    """Change one's own password, clearing the forced-change flag."""
    if not user.check_password(current_password):
        raise ValidationError({"current_password": "That is not your current password."})
    if current_password == new_password:
        # Otherwise a forced change is satisfied by re-entering the password the registrar
        # chose — which is the one secret the whole mechanism exists to retire.
        raise ValidationError(
            {"new_password": "The new password must differ from the current one."}
        )
    validate_password(new_password, user)
    user.set_password(new_password)
    user.must_change_password = False
    user.save(update_fields=["password", "must_change_password", "updated_at"])
    signals.password_changed.send(sender=None, user=user, actor=user)


def team_for(team_id):
    """Resolve a team id, or None."""
    return Team.objects.filter(pk=team_id).first() if team_id else None


# --- Password reset ---------------------------------------------------------------------


password_reset_token_generator = PasswordResetTokenGenerator()


def send_password_reset(*, email: str) -> None:
    """Email a reset link, if that address belongs to a live account.

    Returns None either way and never signals which case occurred — the caller must respond
    identically, because an endpoint that distinguishes them is an account-enumeration
    oracle.

    The token is Django's stateless `PasswordResetTokenGenerator`: it is keyed on the
    password hash and `last_login`, so it invalidates itself the moment it is used or the
    user signs in. Nothing is stored.
    """
    user = User.objects.filter(
        email__iexact=email.strip().lower(), deleted_at__isnull=True, is_active=True
    ).first()
    if user is None:
        return

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = password_reset_token_generator.make_token(user)
    reset_url = f"{settings.FRONTEND_ORIGIN}/reset-password?uid={uid}&token={token}"

    send_mail(
        subject="Reset your password",
        message=(
            f"Hello {user.first_name or ''},\n\n"
            f"Use this link to set a new password:\n{reset_url}\n\n"
            "If you did not request this, you can ignore this message — "
            "your password has not changed.\n"
        ),
        from_email=None,  # falls back to DEFAULT_FROM_EMAIL
        recipient_list=[user.email],
        fail_silently=False,
    )


@transaction.atomic
def reset_password(*, uid: str, token: str, new_password: str) -> User:
    """Complete a reset. Raises ValidationError on a bad, used, or expired token."""
    invalid = ValidationError(
        {"token": "That reset link is invalid or has expired. Request a new one."}
    )
    try:
        user_id = force_str(urlsafe_base64_decode(uid))
    except (TypeError, ValueError, OverflowError) as exc:
        raise invalid from exc

    user = User.objects.filter(
        pk=user_id, deleted_at__isnull=True, is_active=True
    ).first()
    if user is None or not password_reset_token_generator.check_token(user, token):
        raise invalid

    validate_password(new_password, user)
    user.set_password(new_password)
    # They have now chosen their own secret, so the forced-change gate is satisfied.
    user.must_change_password = False
    user.save(update_fields=["password", "must_change_password", "updated_at"])

    signals.password_changed.send(sender=None, user=user, actor=None)
    return user
