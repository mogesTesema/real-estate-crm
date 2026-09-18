"""User administration: the scoped directory, role changes, and deactivation."""
import pytest

from apps.identity.models import User

USERS_URL = "/api/v1/users/"
ME_URL = "/api/v1/auth/me/"
ROLES_URL = "/api/v1/roles/"


def ids_in(response):
    return {row["id"] for row in response.data["results"]}


class TestScopedDirectory:
    """apply_scope decides the rows; the serializer decides the columns. They are separate
    calls, so a narrow scope gets fewer people AND fewer fields about each."""

    def test_an_owner_sees_every_branch(
        self, auth_client, owner, make_user, other_branch
    ):
        elsewhere = make_user("agent", branch=other_branch, team=None)

        response = auth_client(owner).get(USERS_URL)

        assert response.status_code == 200
        assert str(elsewhere.id) in ids_in(response)

    def test_a_manager_sees_only_their_branch(
        self, auth_client, manager, make_user, other_branch, branch
    ):
        same = make_user("agent")
        elsewhere = make_user("agent", branch=other_branch, team=None)

        found = ids_in(auth_client(manager).get(USERS_URL))

        assert str(same.id) in found
        assert str(elsewhere.id) not in found

    def test_an_agent_sees_their_branch_but_only_in_summary(
        self, auth_client, agent, make_user, other_branch
    ):
        colleague = make_user("agent")
        elsewhere = make_user("agent", branch=other_branch, team=None)

        response = auth_client(agent).get(USERS_URL)
        found = ids_in(response)

        assert str(colleague.id) in found, "colleagues are needed for assignee pickers"
        assert str(elsewhere.id) not in found

        row = next(r for r in response.data["results"] if r["id"] == str(colleague.id))
        assert set(row) == {"id", "full_name", "job_title", "branch", "team", "is_active"}
        assert "email" not in row and "phone" not in row

    def test_a_manager_sees_full_detail(self, auth_client, manager, agent):
        response = auth_client(manager).get(USERS_URL)
        row = next(r for r in response.data["results"] if r["id"] == str(agent.id))
        assert "email" in row and "roles" in row

    def test_a_branchless_user_does_not_match_every_other_branchless_user(
        self, auth_client, make_user
    ):
        """A naive branch_id=None filter would make every unplaced user visible to
        every other one."""
        lonely = make_user("agent", branch=None, team=None)
        other = make_user("agent", branch=None, team=None)

        found = ids_in(auth_client(lonely).get(USERS_URL))
        assert found == {str(lonely.id)}
        assert str(other.id) not in found

    def test_deactivated_users_stay_listed_so_they_can_be_managed(
        self, auth_client, owner, agent
    ):
        """This is a management endpoint, not an assignee picker. Hiding deactivated users
        would make deactivation irreversible over the API and invisible to an audit; clients
        that want only active people ask for them (see the next test). The active-only
        directory other apps consume is `selectors.visible_users`."""
        agent.is_active = False
        agent.save(update_fields=["is_active"])

        response = auth_client(owner).get(USERS_URL)
        assert str(agent.id) in ids_in(response)
        row = next(r for r in response.data["results"] if r["id"] == str(agent.id))
        assert row["is_active"] is False

    def test_the_directory_can_be_filtered_to_active_people(
        self, auth_client, owner, agent
    ):
        agent.is_active = False
        agent.save(update_fields=["is_active"])

        response = auth_client(owner).get(USERS_URL, {"is_active": "true"})
        assert str(agent.id) not in ids_in(response)

    def test_soft_deleted_users_are_never_listed(self, auth_client, owner, agent):
        from django.utils import timezone

        agent.deleted_at = timezone.now()
        agent.save(update_fields=["deleted_at"])

        assert str(agent.id) not in ids_in(auth_client(owner).get(USERS_URL))


class TestRoleChanges:
    def test_granting_a_role_obeys_the_registration_matrix(
        self, auth_client, super_admin, make_user
    ):
        target = make_user("agent")

        response = auth_client(super_admin).post(
            f"{USERS_URL}{target.id}/roles/", {"role_code": "finance"}
        )

        assert response.status_code == 200
        assert {r["code"] for r in response.data["roles"]} == {"agent", "finance"}

    def test_an_owner_cannot_grant_a_role_the_documents_never_gave_them(
        self, auth_client, owner, make_user
    ):
        """SRS 3.15.2 gives the owner Sales/Leasing Agents and nothing else."""
        target = make_user("agent")
        for code in ("property_manager", "marketing", "finance"):
            response = auth_client(owner).post(
                f"{USERS_URL}{target.id}/roles/", {"role_code": code}
            )
            assert response.status_code == 403, code

    def test_an_owner_cannot_grant_a_role_above_their_authority(
        self, auth_client, owner, make_user
    ):
        target = make_user("agent")
        response = auth_client(owner).post(
            f"{USERS_URL}{target.id}/roles/", {"role_code": "super_admin"}
        )
        assert response.status_code == 403

    def test_an_agent_cannot_escalate_themselves(self, auth_client, agent):
        """The obvious hole if assign_role skipped the authority check that registration
        runs: don't create anyone, just promote yourself."""
        response = auth_client(agent).post(
            f"{USERS_URL}{agent.id}/roles/", {"role_code": "owner"}
        )

        assert response.status_code == 403
        assert set(agent.user_roles.values_list("role__code", flat=True)) == {"agent"}

    def test_revoking_a_role(self, auth_client, owner, make_user):
        target = make_user("agent")
        response = auth_client(owner).delete(
            f"{USERS_URL}{target.id}/roles/", {"role_code": "agent"}, format="json"
        )

        assert response.status_code == 204
        assert not target.user_roles.exists()

    def test_granting_the_same_role_twice_is_idempotent(
        self, auth_client, super_admin, make_user
    ):
        target = make_user("agent")
        url = f"{USERS_URL}{target.id}/roles/"

        auth_client(super_admin).post(url, {"role_code": "finance"})
        response = auth_client(super_admin).post(url, {"role_code": "finance"})

        assert response.status_code == 200
        assert target.user_roles.filter(role__code="finance").count() == 1


class TestDeactivation:
    def test_it_revokes_access_without_destroying_the_record(
        self, auth_client, api_client, owner, make_user
    ):
        target = make_user("agent")
        email = target.email

        response = auth_client(owner).delete(f"{USERS_URL}{target.id}/")
        assert response.status_code == 204

        target.refresh_from_db()
        assert target.is_active is False
        # Deliberately NOT soft-deleted: the row, its FKs and its email all survive, so
        # audit trails stay readable and a new hire cannot inherit the address.
        assert target.deleted_at is None
        assert User.objects.filter(email=email).exists()

        assert api_client.post(
            "/api/v1/auth/token/", {"email": email, "password": "test-pass-12345"}
        ).status_code == 401

    def test_an_agent_cannot_deactivate_anyone(self, auth_client, agent, make_user):
        target = make_user("agent")
        assert auth_client(agent).delete(f"{USERS_URL}{target.id}/").status_code == 403

    def test_you_cannot_deactivate_yourself(self, auth_client, super_admin):
        response = auth_client(super_admin).delete(f"{USERS_URL}{super_admin.id}/")
        assert response.status_code == 403
        assert "your own account" in str(response.data).lower()

    def test_the_last_super_admin_cannot_be_deactivated(
        self, auth_client, super_admin, make_user
    ):
        """Otherwise one call locks everybody out of user administration for good."""
        other_admin = make_user("super_admin")

        # While two exist, removing one is fine.
        assert (
            auth_client(super_admin).delete(f"{USERS_URL}{other_admin.id}/").status_code
            == 204
        )

        # Now super_admin is the last one; another admin cannot remove them either.
        remover = make_user("super_admin")
        assert (
            auth_client(remover).delete(f"{USERS_URL}{super_admin.id}/").status_code
            == 204
        )
        # `remover` is now the only active super_admin, and cannot remove itself.
        assert (
            auth_client(remover).delete(f"{USERS_URL}{remover.id}/").status_code == 403
        )

    def test_the_last_super_admin_role_cannot_be_revoked(
        self, auth_client, super_admin
    ):
        response = auth_client(super_admin).delete(
            f"{USERS_URL}{super_admin.id}/roles/",
            {"role_code": "super_admin"},
            format="json",
        )
        assert response.status_code == 403
        assert super_admin.user_roles.filter(role__code="super_admin").exists()


class TestProfileEditing:
    def test_a_manager_may_edit_someone_in_their_branch(
        self, auth_client, manager, agent
    ):
        response = auth_client(manager).patch(
            f"{USERS_URL}{agent.id}/", {"job_title": "Lead Agent"}
        )
        assert response.status_code == 200
        agent.refresh_from_db()
        assert agent.job_title == "Lead Agent"

    def test_someone_outside_the_scope_is_not_found(
        self, auth_client, manager, make_user, other_branch
    ):
        """Scoping hides the row rather than returning 403, so a manager cannot probe for
        the existence of users in other branches."""
        elsewhere = make_user("agent", branch=other_branch, team=None)
        response = auth_client(manager).get(f"{USERS_URL}{elsewhere.id}/")
        assert response.status_code == 404


def test_the_role_catalogue_is_readable(auth_client, agent):
    response = auth_client(agent).get(ROLES_URL)

    assert response.status_code == 200
    codes = {row["code"] for row in response.data["results"]}
    assert codes == {
        "super_admin", "owner", "manager", "agent",
        "property_manager", "marketing", "finance", "portal",
    }


@pytest.mark.parametrize("url", [USERS_URL, ROLES_URL, ME_URL])
def test_every_endpoint_requires_authentication(api_client, url):
    assert api_client.get(url).status_code == 401


class TestPrivilegeEscalation:
    """The matrix is only as good as its self-referential cases. A manager may grant `owner`,
    so without a self-check a manager grants *themselves* `owner` and jumps from BRANCH to
    ALL scope in one call — escaping the matrix by obeying it."""

    def test_a_manager_cannot_grant_themselves_owner(self, auth_client, manager):
        response = auth_client(manager).post(
            f"{USERS_URL}{manager.id}/roles/", {"role_code": "owner"}
        )

        assert response.status_code == 403
        assert set(manager.user_roles.values_list("role__code", flat=True)) == {"manager"}

    def test_a_super_admin_cannot_change_their_own_roles_either(
        self, auth_client, super_admin
    ):
        """No exception for the top role: use a second account or the shell."""
        response = auth_client(super_admin).post(
            f"{USERS_URL}{super_admin.id}/roles/", {"role_code": "finance"}
        )
        assert response.status_code == 403

    def test_nobody_can_revoke_their_own_roles(self, auth_client, manager):
        response = auth_client(manager).delete(
            f"{USERS_URL}{manager.id}/roles/", {"role_code": "manager"}, format="json"
        )
        assert response.status_code == 403
        assert manager.user_roles.exists()

    def test_a_manager_may_still_grant_owner_to_someone_else(
        self, auth_client, manager, make_user
    ):
        """The self-check must not break the legitimate path."""
        target = make_user("agent")
        response = auth_client(manager).post(
            f"{USERS_URL}{target.id}/roles/", {"role_code": "owner"}
        )
        assert response.status_code == 200


class TestReactivation:
    def test_a_deactivated_user_can_be_restored(
        self, auth_client, api_client, owner, make_user
    ):
        """Without this a mis-click is unrecoverable: the row keeps the email, so
        re-registering the person collides with the partial unique index."""
        target = make_user("agent")
        auth_client(owner).delete(f"{USERS_URL}{target.id}/")

        response = auth_client(owner).post(f"{USERS_URL}{target.id}/reactivate/")

        assert response.status_code == 200
        target.refresh_from_db()
        assert target.is_active is True
        assert api_client.post(
            "/api/v1/auth/token/",
            {"email": target.email, "password": "test-pass-12345"},
        ).status_code == 200

    def test_an_agent_cannot_reactivate(self, auth_client, agent, make_user, owner):
        target = make_user("agent")
        auth_client(owner).delete(f"{USERS_URL}{target.id}/")

        assert (
            auth_client(agent).post(f"{USERS_URL}{target.id}/reactivate/").status_code
            == 403
        )


def test_the_last_administrator_counts_a_bootstrap_superuser(
    auth_client, make_user, super_admin
):
    """`createsuperuser` attaches the role now, but `is_superuser` can also be set from
    Django admin. A role-only count would miss exactly that account — the one most likely to
    be the last one standing."""
    bootstrap = make_user(None)
    bootstrap.is_superuser = True
    bootstrap.save(update_fields=["is_superuser"])

    # Two administrators exist, so removing the role-based one is allowed...
    assert (
        auth_client(bootstrap).delete(f"{USERS_URL}{super_admin.id}/").status_code == 204
    )
    # ...and now the flag-only account is the last administrator and cannot be removed.
    remover = make_user("super_admin")
    assert (
        auth_client(remover).delete(f"{USERS_URL}{bootstrap.id}/").status_code == 204
    )
