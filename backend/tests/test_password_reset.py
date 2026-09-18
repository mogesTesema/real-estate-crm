"""Forgot-password and reset-password.

The reset endpoint is public and handles credentials, so its failure modes matter more than
its happy path: it must not reveal who has an account, and a token must not outlive its use.
"""
import re

import pytest
from django.core import mail
from django.utils import timezone

FORGOT_URL = "/api/v1/auth/forgot-password/"
RESET_URL = "/api/v1/auth/reset-password/"
TOKEN_URL = "/api/v1/auth/token/"
PASSWORD = "test-pass-12345"
NEW_PASSWORD = "chosen-by-me-8812"


def link_parts(message) -> dict:
    match = re.search(r"uid=([^&\s]+)&token=([^\s]+)", message.body)
    assert match, f"no reset link in:\n{message.body}"
    return {"uid": match.group(1), "token": match.group(2)}


@pytest.fixture
def reset_request(api_client, agent):
    mail.outbox.clear()
    response = api_client.post(FORGOT_URL, {"email": agent.email})
    assert response.status_code == 200
    assert len(mail.outbox) == 1
    return link_parts(mail.outbox[0])


class TestNoEnumeration:
    """An endpoint that says "no such user" tells an attacker who banks here."""

    def test_an_unknown_address_still_returns_200(self, db, api_client):
        response = api_client.post(FORGOT_URL, {"email": "nobody@nowhere.test"})
        assert response.status_code == 200
        assert len(mail.outbox) == 0

    def test_the_response_is_identical_either_way(self, api_client, agent):
        unknown = api_client.post(FORGOT_URL, {"email": "nobody@nowhere.test"})
        known = api_client.post(FORGOT_URL, {"email": agent.email})
        assert unknown.data == known.data

    def test_a_soft_deleted_user_gets_no_mail(self, api_client, agent):
        """The same trap that once let soft-deleted users log in."""
        agent.deleted_at = timezone.now()
        agent.save(update_fields=["deleted_at"])
        mail.outbox.clear()

        response = api_client.post(FORGOT_URL, {"email": agent.email})
        assert response.status_code == 200
        assert len(mail.outbox) == 0

    def test_a_deactivated_user_gets_no_mail(self, api_client, agent):
        agent.is_active = False
        agent.save(update_fields=["is_active"])
        mail.outbox.clear()

        api_client.post(FORGOT_URL, {"email": agent.email})
        assert len(mail.outbox) == 0


class TestReset:
    def test_a_valid_token_sets_the_password(self, api_client, agent, reset_request):
        response = api_client.post(
            RESET_URL, {**reset_request, "new_password": NEW_PASSWORD}
        )
        assert response.status_code == 200

        agent.refresh_from_db()
        assert agent.check_password(NEW_PASSWORD)
        assert api_client.post(
            TOKEN_URL, {"email": agent.email, "password": NEW_PASSWORD}
        ).status_code == 200

    def test_it_clears_a_forced_password_change(self, api_client, agent, reset_request):
        """They have now chosen their own secret, which is what the gate was waiting for."""
        agent.must_change_password = True
        agent.save(update_fields=["must_change_password"])

        api_client.post(RESET_URL, {**reset_request, "new_password": NEW_PASSWORD})

        agent.refresh_from_db()
        assert agent.must_change_password is False

    def test_a_token_works_only_once(self, api_client, agent, reset_request):
        """Django's generator is keyed on the password hash, so using it invalidates it."""
        first = api_client.post(
            RESET_URL, {**reset_request, "new_password": NEW_PASSWORD}
        )
        assert first.status_code == 200

        second = api_client.post(
            RESET_URL, {**reset_request, "new_password": "another-pass-7761"}
        )
        assert second.status_code == 400

        agent.refresh_from_db()
        assert agent.check_password(NEW_PASSWORD)

    def test_a_tampered_token_is_refused(self, api_client, agent, reset_request):
        response = api_client.post(
            RESET_URL,
            {**reset_request, "token": "not-a-real-token", "new_password": NEW_PASSWORD},
        )
        assert response.status_code == 400

        agent.refresh_from_db()
        assert agent.check_password(PASSWORD)

    def test_a_tampered_uid_is_refused(self, api_client, agent, reset_request):
        response = api_client.post(
            RESET_URL, {**reset_request, "uid": "%%%", "new_password": NEW_PASSWORD}
        )
        assert response.status_code == 400

    def test_another_users_token_does_not_work(
        self, api_client, agent, make_user, reset_request
    ):
        """The token is bound to the uid it was issued for."""
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        someone_else = make_user("agent")
        response = api_client.post(
            RESET_URL,
            {
                "uid": urlsafe_base64_encode(force_bytes(someone_else.pk)),
                "token": reset_request["token"],
                "new_password": NEW_PASSWORD,
            },
        )
        assert response.status_code == 400

        someone_else.refresh_from_db()
        assert someone_else.check_password(PASSWORD)

    def test_the_new_password_must_pass_the_validators(
        self, api_client, agent, reset_request
    ):
        response = api_client.post(RESET_URL, {**reset_request, "new_password": "123"})
        assert response.status_code == 400

        agent.refresh_from_db()
        assert agent.check_password(PASSWORD)


def test_a_portal_client_can_reset_their_own_password(
    api_client, portal_tenant
):
    """Handing a client a password over the phone is a poor start; self-service recovery is
    what makes it tolerable."""
    mail.outbox.clear()
    client_user = portal_tenant["user"]

    api_client.post(FORGOT_URL, {"email": client_user.email})
    assert len(mail.outbox) == 1

    response = api_client.post(
        RESET_URL, {**link_parts(mail.outbox[0]), "new_password": NEW_PASSWORD}
    )
    assert response.status_code == 200

    client_user.refresh_from_db()
    assert client_user.must_change_password is False
    assert api_client.post(
        TOKEN_URL, {"email": client_user.email, "password": NEW_PASSWORD}
    ).status_code == 200
