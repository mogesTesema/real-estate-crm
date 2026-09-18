"""Rate limiting on the two public, credential-handling endpoints.

Throttling is disabled for the rest of the suite (see `config/settings/test.py`) because a
shared counter makes unrelated tests fail — whichever happens to run after the limit is
exhausted. It is turned back on here, the only place it is the subject.
"""
from contextlib import contextmanager

import pytest
from rest_framework.throttling import SimpleRateThrottle

TOKEN_URL = "/api/v1/auth/token/"
FORGOT_URL = "/api/v1/auth/forgot-password/"
PASSWORD = "test-pass-12345"


@contextmanager
def throttle_rates(**rates):
    """Temporarily set throttle rates.

    `override_settings(REST_FRAMEWORK=...)` does NOT work here: DRF binds
    `SimpleRateThrottle.THROTTLE_RATES` as a class attribute at import time, so reloading
    `api_settings` leaves the class still pointing at the original dict. Patching the class
    attribute is what actually takes effect.
    """
    original = SimpleRateThrottle.THROTTLE_RATES
    SimpleRateThrottle.THROTTLE_RATES = {**original, **rates}
    try:
        yield
    finally:
        SimpleRateThrottle.THROTTLE_RATES = original


@pytest.mark.django_db
def test_login_is_throttled(api_client, agent):
    """An endpoint that checks passwords and is reachable unauthenticated is a brute-force
    target; unthrottled, nothing slows an attacker down."""
    with throttle_rates(login="3/min"):
        for _ in range(3):
            api_client.post(TOKEN_URL, {"email": agent.email, "password": "wrong"})

        response = api_client.post(
            TOKEN_URL, {"email": agent.email, "password": "wrong"}
        )

    assert response.status_code == 429


@pytest.mark.django_db
def test_throttling_counts_failed_attempts_not_just_successes(api_client, agent):
    """The limit has to bite on wrong passwords — those are the attack."""
    with throttle_rates(login="2/min"):
        api_client.post(TOKEN_URL, {"email": agent.email, "password": "wrong"})
        api_client.post(TOKEN_URL, {"email": agent.email, "password": "wrong"})

        # A correct password now, but the budget is already spent.
        response = api_client.post(
            TOKEN_URL, {"email": agent.email, "password": PASSWORD}
        )

    assert response.status_code == 429


@pytest.mark.django_db
def test_password_reset_is_throttled(api_client):
    """Public, and it sends mail — both a brute-force and a mail-flooding vector."""
    with throttle_rates(password_reset="2/min"):
        for _ in range(2):
            api_client.post(FORGOT_URL, {"email": "someone@acme.test"})

        response = api_client.post(FORGOT_URL, {"email": "someone@acme.test"})

    assert response.status_code == 429


@pytest.mark.django_db
def test_a_legitimate_login_is_not_blocked(api_client, agent):
    with throttle_rates(login="10/min"):
        response = api_client.post(
            TOKEN_URL, {"email": agent.email, "password": PASSWORD}
        )
    assert response.status_code == 200
