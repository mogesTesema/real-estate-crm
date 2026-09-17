"""Registration: who may create whom, and with what placement.

architecture.md states the chain super_admin -> manager -> owner -> agent in three places and
is silent on property_manager/marketing/finance, which are assigned to owner and super_admin.
The matrix is exercised exhaustively below — every (actor, target) pair, allowed and denied —
because an authority table that is only spot-checked is an authority table with holes.
"""
import pytest

from apps.identity.models import Role, User

USERS_URL = "/api/v1/users/"
TOKEN_URL = "/api/v1/auth/token/"
NEW_PASSWORD = "brand-new-pass-4471"

STAFF_ROLES = [
    "super_admin",
    "owner",
    "manager",
    "agent",
    "property_manager",
    "marketing",
    "finance",
]

#: actor role -> the roles they may create. Everything else must be refused.
ALLOWED = {
    "super_admin": set(STAFF_ROLES),
    "manager": {"owner"},
    "owner": {"agent", "property_manager", "marketing", "finance"},
    "agent": set(),
    "property_manager": set(),
    "marketing": set(),
    "finance": set(),
}


def payload(**overrides):
    body = {
        "email": "new.person@acme.test",
        "first_name": "New",
        "last_name": "Person",
        "password": NEW_PASSWORD,
        "role_code": "agent",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize("actor_role", sorted(ALLOWED))
@pytest.mark.parametrize("target_role", STAFF_ROLES)
def test_the_authority_matrix_is_enforced(
    auth_client, make_user, branch, actor_role, target_role
):
    actor = make_user(actor_role)
    response = auth_client(actor).post(
        USERS_URL,
        payload(
            role_code=target_role,
            email=f"{actor_role}.{target_role}@acme.test",
            branch=str(branch.id),
        ),
    )

    if target_role in ALLOWED[actor_role]:
        assert response.status_code == 201, response.data
        assert [r["code"] for r in response.data["roles"]] == [target_role]
    else:
        assert response.status_code == 403, response.data


def test_the_portal_role_is_refused_with_an_explanation(auth_client, super_admin):
    """Not merely "forbidden" — a portal profile needs a contact with a completed contract,
    and identity cannot see contracts. The caller deserves to know where to go instead."""
    response = auth_client(super_admin).post(USERS_URL, payload(role_code="portal"))

    assert response.status_code == 400
    assert "completed contract" in str(response.data)


def test_an_unknown_role_is_refused(auth_client, super_admin):
    response = auth_client(super_admin).post(
        USERS_URL, payload(role_code="chief_vibes_officer")
    )
    assert response.status_code == 400


class TestBranchPlacement:
    """A manager is a branch's onboarding administrator, so they may staff only that branch.
    Without the check, passing any branch id would let them staff a branch they do not run."""

    def test_a_manager_cannot_place_a_user_in_another_branch(
        self, auth_client, manager, other_branch
    ):
        response = auth_client(manager).post(
            USERS_URL, payload(role_code="owner", branch=str(other_branch.id))
        )
        assert response.status_code == 403
        assert "your own branch" in str(response.data).lower()

    def test_a_manager_omitting_the_branch_gets_their_own(
        self, auth_client, manager, branch
    ):
        response = auth_client(manager).post(USERS_URL, payload(role_code="owner"))

        assert response.status_code == 201
        assert response.data["branch"]["id"] == str(branch.id)

    def test_a_super_admin_may_place_a_user_anywhere(
        self, auth_client, super_admin, other_branch
    ):
        response = auth_client(super_admin).post(
            USERS_URL, payload(role_code="manager", branch=str(other_branch.id))
        )
        assert response.status_code == 201
        assert response.data["branch"]["id"] == str(other_branch.id)

    def test_a_team_from_another_branch_is_refused(
        self, auth_client, super_admin, other_branch, team
    ):
        response = auth_client(super_admin).post(
            USERS_URL,
            payload(
                role_code="agent", branch=str(other_branch.id), team=str(team.id)
            ),
        )
        assert response.status_code == 400
        assert "different branch" in str(response.data)


class TestValidation:
    def test_a_weak_password_is_refused(self, auth_client, super_admin):
        response = auth_client(super_admin).post(
            USERS_URL, payload(password="123")
        )
        assert response.status_code == 400
        assert not User.objects.filter(email="new.person@acme.test").exists()

    def test_a_duplicate_email_is_refused(self, auth_client, super_admin, agent):
        response = auth_client(super_admin).post(
            USERS_URL, payload(email=agent.email)
        )
        assert response.status_code == 400

    def test_an_email_freed_by_soft_deletion_may_be_reused(
        self, auth_client, super_admin, agent
    ):
        from django.utils import timezone

        agent.deleted_at = timezone.now()
        agent.save(update_fields=["deleted_at"])

        response = auth_client(super_admin).post(
            USERS_URL, payload(email=agent.email)
        )
        assert response.status_code == 201

    def test_a_failed_registration_leaves_no_partial_user(
        self, auth_client, super_admin, other_branch, team
    ):
        """register_user is atomic: the user and their role land together or not at all."""
        before = User.objects.count()
        auth_client(super_admin).post(
            USERS_URL,
            payload(branch=str(other_branch.id), team=str(team.id)),
        )
        assert User.objects.count() == before


def test_a_registered_user_can_log_in_with_the_password_the_registrar_set(
    auth_client, api_client, super_admin
):
    """The end-to-end point of the whole pass."""
    created = auth_client(super_admin).post(USERS_URL, payload(role_code="agent"))
    assert created.status_code == 201

    response = api_client.post(
        TOKEN_URL, {"email": "new.person@acme.test", "password": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert "access" in response.data


def test_a_registered_user_must_change_their_password(auth_client, super_admin):
    response = auth_client(super_admin).post(USERS_URL, payload())

    assert response.data["must_change_password"] is True
    created = User.objects.get(email="new.person@acme.test")
    assert created.must_change_password is True


def test_optional_profile_fields_are_stored(auth_client, super_admin):
    response = auth_client(super_admin).post(
        USERS_URL,
        payload(phone="+971500000000", employee_number="E-1001", job_title="Agent II"),
    )

    assert response.status_code == 201
    created = User.objects.get(email="new.person@acme.test")
    assert created.phone == "+971500000000"
    assert created.employee_number == "E-1001"


def test_a_superuser_with_no_roles_can_still_bootstrap(auth_client, make_user, branch):
    """createsuperuser attaches super_admin, but the is_superuser flag must work on its own —
    it is the escape hatch that exists before any role does."""
    root = make_user(None)
    root.is_superuser = True
    root.save(update_fields=["is_superuser"])

    response = auth_client(root).post(
        USERS_URL, payload(role_code="manager", branch=str(branch.id))
    )
    assert response.status_code == 201


def test_createsuperuser_attaches_the_super_admin_role(db):
    root = User.objects.create_superuser(
        email="root@acme.test", password="root-pass-12345",
        first_name="Root", last_name="Admin",
    )
    assert [link.role.code for link in root.user_roles.all()] == [Role.SUPER_ADMIN]


class TestPlacementIsRequiredForScopedRoles:
    """A BRANCH- or TEAM-scoped role with no branch resolves to "sees only themselves" — an
    account that looks like a manager and can do nothing. Refuse it at creation."""

    def test_a_manager_without_a_branch_is_refused(self, auth_client, super_admin):
        response = auth_client(super_admin).post(USERS_URL, payload(role_code="manager"))

        assert response.status_code == 400
        assert "branch is required" in str(response.data)

    def test_an_agent_without_a_branch_is_allowed(self, auth_client, super_admin):
        """OWN scope needs no org placement to be coherent."""
        response = auth_client(super_admin).post(USERS_URL, payload(role_code="agent"))
        assert response.status_code == 201

    def test_an_owner_without_a_branch_is_allowed(self, auth_client, super_admin):
        """ALL scope is agency-wide, so a branch is optional."""
        response = auth_client(super_admin).post(USERS_URL, payload(role_code="owner"))
        assert response.status_code == 201


class TestEmailCasing:
    """Django's normalize_email lowercases only the DOMAIN, so without extra care
    "Alice@acme.test" and "alice@acme.test" become two live rows that both satisfy the
    partial unique index — and only one of those people can ever log in."""

    def test_the_address_is_stored_lowercased(self, auth_client, super_admin):
        response = auth_client(super_admin).post(
            USERS_URL, payload(email="Mixed.Case@Acme.TEST")
        )

        assert response.status_code == 201
        assert response.data["email"] == "mixed.case@acme.test"

    def test_a_differently_cased_duplicate_is_refused(
        self, auth_client, super_admin, agent
    ):
        response = auth_client(super_admin).post(
            USERS_URL, payload(email=agent.email.upper())
        )
        assert response.status_code == 400

    def test_login_is_case_insensitive(self, auth_client, api_client, super_admin):
        auth_client(super_admin).post(USERS_URL, payload(email="Casey.Jones@acme.test"))

        response = api_client.post(
            TOKEN_URL, {"email": "CASEY.JONES@ACME.TEST", "password": NEW_PASSWORD}
        )
        assert response.status_code == 200
