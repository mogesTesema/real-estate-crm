"""Logout and refresh-token revocation.

Before this existed, `is_active=False` was the only way to end a session and a stolen refresh
token stayed exchangeable for its full seven-day life.
"""
LOGIN_URL = "/api/v1/auth/token/"
REFRESH_URL = "/api/v1/auth/token/refresh/"
LOGOUT_URL = "/api/v1/auth/logout/"
ME_URL = "/api/v1/auth/me/"
PASSWORD = "test-pass-12345"


def login(api_client, user):
    response = api_client.post(LOGIN_URL, {"email": user.email, "password": PASSWORD})
    assert response.status_code == 200
    return response.data


def test_logout_revokes_the_refresh_token(api_client, agent):
    tokens = login(api_client, agent)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    response = api_client.post(LOGOUT_URL, {"refresh": tokens["refresh"]})
    assert response.status_code == 205

    # The refresh token is now worthless, so no new access token can be minted.
    api_client.credentials()
    refreshed = api_client.post(REFRESH_URL, {"refresh": tokens["refresh"]})
    assert refreshed.status_code == 401


def test_a_refresh_token_works_before_logout(api_client, agent):
    """Guard against the previous test passing for the wrong reason."""
    tokens = login(api_client, agent)
    assert api_client.post(REFRESH_URL, {"refresh": tokens["refresh"]}).status_code == 200


def test_logging_out_twice_is_refused_cleanly(api_client, agent):
    tokens = login(api_client, agent)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    assert api_client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}).status_code == 205
    second = api_client.post(LOGOUT_URL, {"refresh": tokens["refresh"]})
    assert second.status_code == 400
    assert "already revoked" in str(second.data)


def test_a_garbage_token_is_refused(api_client, agent):
    tokens = login(api_client, agent)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    assert api_client.post(LOGOUT_URL, {"refresh": "not-a-token"}).status_code == 400


def test_rotation_invalidates_the_old_refresh_token(api_client, agent):
    """ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION: a stolen refresh token stops working
    as soon as the legitimate holder next refreshes."""
    tokens = login(api_client, agent)

    rotated = api_client.post(REFRESH_URL, {"refresh": tokens["refresh"]})
    assert rotated.status_code == 200
    assert rotated.data["refresh"] != tokens["refresh"]

    replayed = api_client.post(REFRESH_URL, {"refresh": tokens["refresh"]})
    assert replayed.status_code == 401


def test_logout_does_not_expire_the_access_token(api_client, agent):
    """Stated, not wished away: a JWT cannot be recalled once issued. Logging out stops the
    session being *renewed*; the current access token lives out its (deliberately short)
    lifetime. This test documents the real behaviour so nobody assumes otherwise."""
    tokens = login(api_client, agent)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    api_client.post(LOGOUT_URL, {"refresh": tokens["refresh"]})

    assert api_client.get(ME_URL).status_code == 200


def test_logout_requires_authentication(api_client, agent):
    tokens = login(api_client, agent)
    api_client.credentials()
    assert api_client.post(LOGOUT_URL, {"refresh": tokens["refresh"]}).status_code == 401
