"""Authentication: login, session identity, and the forced password change."""
import pytest
from django.utils import timezone

TOKEN_URL = "/api/v1/auth/token/"
ME_URL = "/api/v1/auth/me/"
CHANGE_PASSWORD_URL = "/api/v1/auth/change-password/"
USERS_URL = "/api/v1/users/"

PASSWORD = "test-pass-12345"  # what tests/conftest.py's make_user sets


def login(api_client, email, password=PASSWORD):
    return api_client.post(TOKEN_URL, {"email": email, "password": password})


def test_login_returns_a_token_pair(api_client, agent):
    response = login(api_client, agent.email)
    assert response.status_code == 200
    assert "access" in response.data
    assert "refresh" in response.data


def test_login_rejects_a_wrong_password(api_client, agent):
    assert login(api_client, agent.email, "not-the-password").status_code == 401


def test_a_soft_deleted_user_cannot_log_in(api_client, agent):
    """The partial unique index lets a soft-deleted row keep its email, so authentication
    has to exclude it explicitly — see UserManager.get_by_natural_key."""
    agent.deleted_at = timezone.now()
    agent.save(update_fields=["deleted_at"])

    assert login(api_client, agent.email).status_code == 401


def test_an_email_reused_after_soft_deletion_authenticates_the_live_user(
    api_client, make_user, agent
):
    """Without the manager override this raises MultipleObjectsReturned rather than
    logging anyone in."""
    email = agent.email
    agent.deleted_at = timezone.now()
    agent.save(update_fields=["deleted_at"])

    replacement = make_user("agent", email=email)

    response = login(api_client, email)
    assert response.status_code == 200

    access = response.data["access"]
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert api_client.get(ME_URL).data["id"] == str(replacement.id)


def test_an_inactive_user_cannot_log_in(api_client, agent):
    agent.is_active = False
    agent.save(update_fields=["is_active"])
    assert login(api_client, agent.email).status_code == 401


def test_me_reports_identity_roles_and_scopes(auth_client, agent):
    response = auth_client(agent).get(ME_URL)

    assert response.status_code == 200
    body = response.data
    assert body["email"] == agent.email
    assert [role["code"] for role in body["roles"]] == ["agent"]
    assert body["data_scopes"] == ["OWN"]
    assert body["company"]["name"] == "Acme Realty"
    # An agent may register nobody, and the frontend needs to know that to hide the button.
    assert body["grantable_role_codes"] == []


def test_me_reports_what_an_owner_may_grant(auth_client, owner):
    body = auth_client(owner).get(ME_URL).data
    assert body["grantable_role_codes"] == [
        "agent",
        "finance",
        "marketing",
        "property_manager",
    ]


def test_me_allows_editing_your_own_profile(auth_client, agent):
    response = auth_client(agent).patch(ME_URL, {"job_title": "Senior Agent"})

    assert response.status_code == 200
    assert response.data["job_title"] == "Senior Agent"
    agent.refresh_from_db()
    assert agent.job_title == "Senior Agent"


def test_a_user_without_a_branch_still_gets_a_me_response(auth_client, make_user):
    """Company is reached through the nullable branch, so an unplaced user must not 500."""
    unplaced = make_user("super_admin", branch=None, team=None)
    response = auth_client(unplaced).get(ME_URL)

    assert response.status_code == 200
    assert response.data["company"] is None


class TestForcedPasswordChange:
    """A registrar chooses the initial password, so it is known to someone else until the
    new user replaces it. Until then they may reach only /me/ and change-password."""

    @pytest.fixture
    def stale(self, make_user):
        user = make_user("owner")
        user.must_change_password = True
        user.save(update_fields=["must_change_password"])
        return user

    def test_they_are_blocked_from_the_rest_of_the_api(self, auth_client, stale):
        response = auth_client(stale).get(USERS_URL)
        assert response.status_code == 403
        assert "change your password" in str(response.data).lower()

    def test_but_may_still_read_their_own_session(self, auth_client, stale):
        response = auth_client(stale).get(ME_URL)
        assert response.status_code == 200
        assert response.data["must_change_password"] is True

    def test_changing_the_password_lifts_the_block(self, auth_client, stale):
        client = auth_client(stale)
        response = client.post(
            CHANGE_PASSWORD_URL,
            {"current_password": PASSWORD, "new_password": "fresh-pass-98765"},
        )
        assert response.status_code == 200

        stale.refresh_from_db()
        assert stale.must_change_password is False
        assert stale.check_password("fresh-pass-98765")
        assert client.get(USERS_URL).status_code == 200

    def test_the_current_password_must_be_right(self, auth_client, stale):
        response = auth_client(stale).post(
            CHANGE_PASSWORD_URL,
            {"current_password": "wrong", "new_password": "fresh-pass-98765"},
        )
        assert response.status_code == 400
        stale.refresh_from_db()
        assert stale.must_change_password is True

    def test_the_new_password_must_pass_the_validators(self, auth_client, stale):
        response = auth_client(stale).post(
            CHANGE_PASSWORD_URL,
            {"current_password": PASSWORD, "new_password": "123"},
        )
        assert response.status_code == 400
        stale.refresh_from_db()
        assert stale.check_password(PASSWORD)


def test_the_api_refuses_anonymous_requests(api_client):
    assert api_client.get(USERS_URL).status_code == 401
    assert api_client.get(ME_URL).status_code == 401
