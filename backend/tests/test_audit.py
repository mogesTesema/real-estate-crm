"""The audit trail SRS 5.3 requires.

> "The System shall maintain a full audit trail for sensitive actions (login, data export,
>  permission changes, document access, GPS track access)."

Registration, role changes, deactivation and login all happen in `identity`, which the import
DAG forbids from writing to `platform`. So `identity` emits signals and `platform` records
them — and these tests are what prove the inversion actually carries the events rather than
dropping them silently.
"""
import pytest

from apps.platform.models import AuditEvent

USERS_URL = "/api/v1/users/"
TOKEN_URL = "/api/v1/auth/token/"
PASSWORD = "test-pass-12345"


def events(action=None, entity_type=None):
    qs = AuditEvent.objects.all()
    if action:
        qs = qs.filter(action=action)
    if entity_type:
        qs = qs.filter(entity_type=entity_type)
    return qs


@pytest.fixture(autouse=True)
def _audit_db(db):
    """Just database access — deliberately NOT a cleanup fixture.

    `AuditEvent.objects.all().delete()` raises "permission denied": DELETE is revoked on
    `platform_audit_event` at the database-role level, which is precisely the guarantee being
    relied on. Test isolation comes from pytest-django's per-test transaction rollback
    instead, which needs no privilege at all.
    """


class TestPermissionChanges:
    """"Permission changes" is named explicitly in SRS 5.3."""

    def test_registration_is_recorded(self, auth_client, super_admin, branch):
        response = auth_client(super_admin).post(
            USERS_URL,
            {
                "email": "newhire@acme.test",
                "first_name": "New",
                "last_name": "Hire",
                "password": "what-a-pass-9911",
                "role_code": "manager",
                "branch": str(branch.id),
            },
        )
        assert response.status_code == 201

        event = events(AuditEvent.Action.CREATE, "USER").get()
        assert event.actor_user_id == super_admin.id
        assert str(event.entity_id) == response.data["id"]
        assert event.new_values["role_code"] == "manager"

    def test_granting_a_role_is_recorded(self, auth_client, super_admin, make_user):
        target = make_user("agent")
        auth_client(super_admin).post(
            f"{USERS_URL}{target.id}/roles/", {"role_code": "finance"}
        )

        event = events(AuditEvent.Action.UPDATE, "USER_ROLE").get()
        assert event.actor_user_id == super_admin.id
        assert event.new_values == {"granted": "finance"}

    def test_revoking_a_role_is_recorded(self, auth_client, super_admin, make_user):
        target = make_user("agent")
        auth_client(super_admin).delete(
            f"{USERS_URL}{target.id}/roles/", {"role_code": "agent"}, format="json"
        )

        event = events(AuditEvent.Action.UPDATE, "USER_ROLE").get()
        assert event.old_values == {"revoked": "agent"}

    def test_a_refused_grant_records_nothing(self, auth_client, owner, make_user):
        """An attempt that changed nothing is not a permission change."""
        target = make_user("agent")
        auth_client(owner).post(
            f"{USERS_URL}{target.id}/roles/", {"role_code": "super_admin"}
        )
        assert events(entity_type="USER_ROLE").count() == 0

    def test_a_duplicate_grant_records_nothing(
        self, auth_client, super_admin, make_user
    ):
        """Re-granting a role the user already holds is a no-op, not an event."""
        target = make_user("agent")
        url = f"{USERS_URL}{target.id}/roles/"
        auth_client(super_admin).post(url, {"role_code": "finance"})
        auth_client(super_admin).post(url, {"role_code": "finance"})

        assert events(entity_type="USER_ROLE").count() == 1

    def test_deactivation_and_reactivation_are_recorded(
        self, auth_client, super_admin, make_user
    ):
        target = make_user("agent")
        auth_client(super_admin).delete(f"{USERS_URL}{target.id}/")
        auth_client(super_admin).post(f"{USERS_URL}{target.id}/reactivate/")

        recorded = list(
            events(AuditEvent.Action.UPDATE, "USER").order_by("created_at")
        )
        assert [e.new_values["is_active"] for e in recorded] == [False, True]


class TestLogin:
    """Named first in SRS 5.3's list."""

    def test_a_successful_login_is_recorded(self, api_client, agent):
        api_client.post(TOKEN_URL, {"email": agent.email, "password": PASSWORD})

        event = events(AuditEvent.Action.LOGIN).get()
        assert event.actor_user_id == agent.id
        assert str(event.entity_id) == str(agent.id)

    def test_a_failed_login_is_recorded_with_no_actor(self, api_client, agent):
        """The point of a failed login is that no identity was established — so the attempted
        address is what gets recorded, which is what makes credential-stuffing visible."""
        api_client.post(TOKEN_URL, {"email": agent.email, "password": "wrong"})

        event = events(AuditEvent.Action.LOGIN_FAILED).get()
        assert event.actor_user is None
        assert event.new_values["attempted_email"] == agent.email

    def test_a_login_for_an_unknown_address_is_still_recorded(self, api_client, db):
        api_client.post(TOKEN_URL, {"email": "ghost@acme.test", "password": "whatever"})

        event = events(AuditEvent.Action.LOGIN_FAILED).get()
        assert event.new_values["attempted_email"] == "ghost@acme.test"

    def test_the_client_address_is_captured(self, api_client, agent):
        api_client.post(
            TOKEN_URL,
            {"email": agent.email, "password": PASSWORD},
            HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1",
            HTTP_USER_AGENT="pytest-client/1.0",
        )

        event = events(AuditEvent.Action.LOGIN).get()
        assert event.ip_address == "203.0.113.7"  # the client, not the proxy
        assert event.user_agent == "pytest-client/1.0"


class TestPortalAudit:
    def test_granting_portal_access_is_recorded(self, portal_tenant):
        event = events(AuditEvent.Action.CREATE, "PORTAL_PROFILE").get()
        assert event.actor_user_id == portal_tenant["inviter"].id
        assert event.new_values["portal_type"] == "TENANT"
        assert event.new_values["contract_ref_type"] == "LEASE"

    def test_revoking_portal_access_is_recorded(self, auth_client, portal_tenant):
        profile = portal_tenant["user"].portal_profile
        auth_client(portal_tenant["inviter"]).delete(
            f"/api/v1/portal-users/{profile.id}/"
        )

        event = events(AuditEvent.Action.UPDATE, "PORTAL_PROFILE").get()
        assert event.new_values["eligibility_status"] == "SUSPENDED"


def test_audit_rows_cannot_be_rewritten(db, agent, api_client):
    """The whole point. `platform_audit_event` has UPDATE/DELETE revoked at the database-role
    level — covered in depth by tests/test_append_only.py; asserted here so the audit story
    is complete in one place."""
    from django.db import connection

    api_client.post(TOKEN_URL, {"email": agent.email, "password": PASSWORD})
    assert events(AuditEvent.Action.LOGIN).exists()

    with connection.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege(current_user, 'platform_audit_event', 'UPDATE'), "
            "has_table_privilege(current_user, 'platform_audit_event', 'DELETE')"
        )
        can_update, can_delete = cur.fetchone()
    assert not can_update and not can_delete
