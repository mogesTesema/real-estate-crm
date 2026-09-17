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
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction

from .models import Role, Team, User, UserRole

# --- Registration authority ---------------------------------------------------------------
#
# architecture.md states the chain super_admin -> manager -> owner -> agent in three places
# (the v3.2 header, the Architecture Principles, and §4: "owner→ALL (registered under a
# manager); agent→OWN (registered under an owner)"). A manager registering an owner reads
# oddly, since `owner` holds the broader data_scope — but registration authority and data
# scope are different axes: the manager is a branch's onboarding administrator, the owner is
# the person with agency-wide visibility.
#
# The spec is silent on property_manager, marketing and finance. Those are given to `owner`
# (and super_admin), the roles that already hold agency-wide scope.

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
    Role.OWNER: frozenset(
        {Role.AGENT, Role.PROPERTY_MANAGER, Role.MARKETING, Role.FINANCE}
    ),
}

#: Roles whose holders may only place new users inside their own branch.
BRANCH_BOUND_REGISTRARS = frozenset({Role.MANAGER})

PORTAL_REFUSAL = (
    "The 'portal' role cannot be granted here. A portal profile must be tied to a contact "
    "with a completed contract (a closed transaction or an active lease), which this "
    "endpoint cannot verify. Grant portal access from the module that owns the contract."
)


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

    user_role, _ = UserRole.objects.get_or_create(user=user, role=role)
    return user_role


@transaction.atomic
def revoke_role(*, actor, user: User, role_code: str) -> None:
    """Remove a role. Revoking the last super_admin is refused."""
    _assert_not_self(actor, user, "change the roles on")
    _assert_may_grant(actor, role_code)
    if role_code == Role.SUPER_ADMIN:
        _assert_not_last_super_admin(user, "revoke the last super_admin role")
    UserRole.objects.filter(user=user, role__code=role_code).delete()


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


def team_for(team_id):
    """Resolve a team id, or None."""
    return Team.objects.filter(pk=team_id).first() if team_id else None
