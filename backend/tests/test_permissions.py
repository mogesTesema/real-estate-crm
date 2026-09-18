"""Function-level access control (architecture.md §2).

§2 opens by separating two controls: function-level RBAC answers *"can this role use this
feature?"*, row scoping answers *"which records may this user see?"*. The first pass built
only the second, and these tests exist because of what that left open — a rental tenant could
create properties, capture leads, and write a priority-0 routing rule sending every inbound
lead to themselves. Row scoping never had an opinion about that, because a routing rule is not
a row anybody owns.
"""
import pytest

from apps.identity.permissions import is_agency_admin, is_portal_client


@pytest.fixture
def client_login(portal_tenant, auth_client):
    """A portal client past the forced password change — their steady state.

    The distinction matters: while `must_change_password` is set, `PasswordIsCurrent` blocks
    everything, so probing a freshly invited client proves nothing about the permissions.
    """
    user = portal_tenant["user"]
    response = auth_client(user).post(
        "/api/v1/auth/change-password/",
        {"current_password": "client-pass-55512", "new_password": "client-own-88221"},
        format="json",
    )
    assert response.status_code == 200, response.data
    user.refresh_from_db()
    assert user.must_change_password is False
    return auth_client(user), user


class TestWhoIsAClient:
    def test_a_portal_only_account_is_a_client(self, portal_tenant):
        assert is_portal_client(portal_tenant["user"]) is True

    def test_staff_are_not(self, make_user):
        for code in ("agent", "manager", "owner", "finance", "property_manager"):
            assert is_portal_client(make_user(code)) is False

    def test_a_user_with_no_roles_is_not_a_client_either(self, make_user):
        """They are staff with nothing granted — scoping gives them no rows. Reading a
        role-less account as a client would be a guess in the wrong direction."""
        assert is_portal_client(make_user()) is False

    def test_an_employee_who_is_also_a_tenant_stays_staff(self, make_user, portal_tenant):
        """A real person in a real brokerage. Reading the combination as "client" would lock
        an employee out of their job."""
        from apps.identity.models import Role, UserRole

        staff = make_user("agent")
        UserRole.objects.create(user=staff, role=Role.objects.get(code="portal"))
        assert is_portal_client(staff) is False

    def test_agency_admin_is_the_all_scope(self, make_user):
        assert is_agency_admin(make_user("owner")) is True
        assert is_agency_admin(make_user("super_admin")) is True
        assert is_agency_admin(make_user("manager")) is False


class TestPortalClientsCannotWrite:
    """The hole this closes: every one of these returned 201 before."""

    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            ("/api/v1/contacts/", {"first_name": "Mine", "last_name": "Own"}),
            (
                "/api/v1/leads/",
                {
                    "lead_type": "BUY",
                    "title": "Mine",
                    "contact_details": {"first_name": "X", "email": "x@example.test"},
                },
            ),
            (
                "/api/v1/routing-rules/",
                {"name": "All to me", "priority": 0, "criteria": {}},
            ),
        ],
    )
    def test_writes_are_refused(self, client_login, path, payload):
        client, _ = client_login
        assert client.post(path, payload, format="json").status_code == 403

    def test_creating_a_property_is_refused(self, client_login, property_type, make_user):
        client, _ = client_login
        response = client.post(
            "/api/v1/properties/",
            {
                "property_type": str(property_type.id),
                "managed_by": str(make_user("property_manager").id),
                "title": "Mine",
                "address_line_1": "1 St",
                "city": "Dubai",
                "country": "AE",
            },
            format="json",
        )
        assert response.status_code == 403

    def test_reads_stay_open_because_scoping_already_answers_them(self, client_login):
        """A client reading their own lease through the ordinary endpoint is the portal
        working. Blocking reads outright would break it."""
        client, _ = client_login
        assert client.get("/api/v1/contacts/").status_code == 200
        assert client.get("/api/v1/contacts/").data["count"] <= 1

    def test_self_service_still_works(self, client_login):
        """Change-password and logout must stay reachable, or the account is unusable."""
        client, _ = client_login
        assert client.get("/api/v1/auth/me/").status_code == 200
        assert (
            client.post(
                "/api/v1/auth/change-password/",
                {"current_password": "client-own-88221", "new_password": "client-own-99331"},
                format="json",
            ).status_code
            == 200
        )


class TestStaffOnlyReads:
    """Some *reads* are staff tools rather than records."""

    def test_the_duplicate_probe_is_closed_to_clients(self, client_login):
        """It reports the existence of contacts outside the caller's scope by design — safe
        between colleagues, a customer enumerating the company's book otherwise."""
        client, _ = client_login
        assert (
            client.get("/api/v1/contacts/duplicates/?phone=0501234567").status_code == 403
        )

    def test_bulk_export_is_closed_to_clients(self, client_login):
        client, _ = client_login
        assert client.get("/api/v1/contacts/export/").status_code == 403

    def test_the_dashboard_is_closed_to_clients(self, client_login):
        client, _ = client_login
        assert client.get("/api/v1/dashboard/").status_code == 403
        assert client.get("/api/v1/reports/").status_code == 403

    def test_staff_still_reach_all_three(self, auth_client, make_user):
        agent = make_user("agent")
        assert auth_client(agent).get("/api/v1/contacts/duplicates/?phone=0501234567").status_code == 200
        assert auth_client(agent).get("/api/v1/contacts/export/").status_code == 200
        assert auth_client(agent).get("/api/v1/dashboard/").status_code == 200


class TestRoutingRulesAreConfiguration:
    """A routing rule decides what happens to *everybody's* records, so it is not a row with
    an owner and row scoping has nothing to say about it."""

    def test_an_agent_cannot_route_every_lead_to_themselves(self, auth_client, make_user):
        agent = make_user("agent")
        response = auth_client(agent).post(
            "/api/v1/routing-rules/",
            {"name": "All to me", "priority": 0, "criteria": {},
             "assign_to_user": str(agent.id)},
            format="json",
        )
        assert response.status_code == 403

    def test_a_branch_manager_cannot_either(self, auth_client, make_user):
        manager = make_user("manager")
        response = auth_client(manager).post(
            "/api/v1/routing-rules/",
            {"name": "Mine", "priority": 0, "criteria": {},
             "assign_to_user": str(manager.id)},
            format="json",
        )
        assert response.status_code == 403

    def test_an_owner_can(self, auth_client, make_user, owner):
        response = auth_client(owner).post(
            "/api/v1/routing-rules/",
            {"name": "Buyers", "priority": 1, "criteria": {"lead_type": "BUY"},
             "assign_to_user": str(make_user("agent").id)},
            format="json",
        )
        assert response.status_code == 201, response.data

    def test_staff_may_still_read_them(self, auth_client, make_user):
        """So an agent can see why their leads are landing where they are."""
        assert auth_client(make_user("agent")).get("/api/v1/routing-rules/").status_code == 200

    def test_an_agent_cannot_edit_or_delete_one(self, auth_client, make_user, owner):
        from apps.crm.models import LeadRoutingRule

        rule = LeadRoutingRule.objects.create(
            name="Existing", priority=1, criteria={}, assign_to_user=make_user("agent")
        )
        agent = make_user("agent")
        assert auth_client(agent).patch(
            f"/api/v1/routing-rules/{rule.id}/", {"priority": 0}, format="json"
        ).status_code == 403
        assert auth_client(agent).delete(
            f"/api/v1/routing-rules/{rule.id}/"
        ).status_code == 403


def test_an_unauthenticated_request_is_still_refused(api_client):
    assert api_client.get("/api/v1/contacts/").status_code == 401
    assert api_client.post("/api/v1/contacts/", {}, format="json").status_code == 401
