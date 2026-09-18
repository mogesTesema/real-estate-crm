"""Portal clients — the eighth role, and the one with a hard isolation rule.

SRS 3.11.2 grants a portal login **only** to a client with a completed contract, "not to
open/unconverted leads". SRS 3.11.6 and 3.17.6 then make client isolation a hard security
rule. Both are load-bearing, so both are tested from the outside.
"""
import pytest

from apps.crm.models import Transaction
from apps.identity.models import PortalProfile, User
from apps.property_ops.models import Lease, LeaseParty

PORTAL_URL = "/api/v1/portal-users/"
TOKEN_URL = "/api/v1/auth/token/"
USERS_URL = "/api/v1/users/"
ME_URL = "/api/v1/auth/me/"
CLIENT_PASSWORD = "client-pass-55512"


def invite(client, *, contact, ref_type, ref_id, portal_type, password=CLIENT_PASSWORD):
    return client.post(
        PORTAL_URL,
        {
            "contact_id": str(contact.id),
            "portal_type": portal_type,
            "contract_ref_type": ref_type,
            "contract_ref_id": str(ref_id),
            "password": password,
        },
    )


class TestWhoMayInvite:
    """Role Definitions §4 gives portal invitation to the Sales/Leasing Agent ("Invite portal
    access only for clients with completed contracts"); §5 gives the Property Manager tenant
    onboarding and landlord reporting. Each invites the clients they actually work with."""

    @pytest.mark.parametrize(
        ("role", "portal_type", "allowed"),
        [
            ("agent", "BUYER", True),
            ("agent", "SELLER", True),
            ("agent", "TENANT", False),
            ("agent", "LANDLORD", False),
            ("property_manager", "TENANT", True),
            ("property_manager", "LANDLORD", True),
            ("property_manager", "BUYER", False),
            ("property_manager", "SELLER", False),
            ("manager", "TENANT", True),
            ("owner", "BUYER", True),
            ("super_admin", "LANDLORD", True),
            ("marketing", "BUYER", False),
            ("finance", "TENANT", False),
        ],
    )
    def test_invite_authority(
        self,
        auth_client,
        make_user,
        make_contact,
        make_lease_for,
        make_transaction,
        role,
        portal_type,
        allowed,
    ):
        actor = make_user(role)
        contact = make_contact()

        if portal_type in ("TENANT", "LANDLORD"):
            kwargs = {"tenant": contact} if portal_type == "TENANT" else {"landlord": contact}
            contract = make_lease_for(**kwargs)
            ref_type = "LEASE"
        else:
            contract = make_transaction(contact=contact)
            ref_type = "TRANSACTION"

        response = invite(
            auth_client(actor),
            contact=contact,
            ref_type=ref_type,
            ref_id=contract.id,
            portal_type=portal_type,
        )

        if allowed:
            assert response.status_code == 201, response.data
        else:
            assert response.status_code == 403, response.data

    def test_a_portal_client_cannot_invite_anyone(
        self, auth_client, portal_tenant, make_contact, make_lease_for
    ):
        contact = make_contact()
        lease = make_lease_for(tenant=contact)

        response = invite(
            auth_client(portal_tenant["user"]),
            contact=contact,
            ref_type="LEASE",
            ref_id=lease.id,
            portal_type="TENANT",
        )
        assert response.status_code == 403


class TestEligibility:
    """The contract must be real, in the right state, and the client must be party to it."""

    @pytest.mark.parametrize(
        ("status", "eligible"),
        [
            (Lease.Status.ACTIVE, True),
            (Lease.Status.EXPIRING, True),
            (Lease.Status.RENEWED, True),
            # "signed/active lease" (SRS 3.11.2) — awaiting signature is not signed.
            (Lease.Status.PENDING_SIGNATURE, False),
            (Lease.Status.DRAFT, False),
            (Lease.Status.TERMINATED, False),
            (Lease.Status.EXPIRED, False),
        ],
    )
    def test_lease_status_gates_access(
        self, auth_client, make_user, make_contact, make_lease_for, status, eligible
    ):
        pm = make_user("property_manager")
        contact = make_contact()
        lease = make_lease_for(tenant=contact, status=status)

        response = invite(
            auth_client(pm),
            contact=contact,
            ref_type="LEASE",
            ref_id=lease.id,
            portal_type="TENANT",
        )

        assert (response.status_code == 201) is eligible, response.data

    @pytest.mark.parametrize(
        ("status", "eligible"),
        [
            (Transaction.Status.CONTRACTED, True),
            (Transaction.Status.PARTIALLY_PAID, True),
            (Transaction.Status.COMPLETED, True),
            # The open case SRS 3.11.2 explicitly forbids.
            (Transaction.Status.PENDING, False),
            (Transaction.Status.CANCELLED, False),
        ],
    )
    def test_transaction_status_gates_access(
        self, auth_client, make_user, make_contact, make_transaction, status, eligible
    ):
        agent = make_user("agent")
        contact = make_contact()
        txn = make_transaction(contact=contact, status=status)

        response = invite(
            auth_client(agent),
            contact=contact,
            ref_type="TRANSACTION",
            ref_id=txn.id,
            portal_type="BUYER",
        )

        assert (response.status_code == 201) is eligible, response.data

    def test_a_contact_unconnected_to_the_contract_is_refused(
        self, auth_client, make_user, make_contact, make_lease_for
    ):
        """Holding *a* contract is not enough — it must be *this* client's."""
        pm = make_user("property_manager")
        tenant = make_contact()
        stranger = make_contact()
        lease = make_lease_for(tenant=tenant)

        response = invite(
            auth_client(pm),
            contact=stranger,
            ref_type="LEASE",
            ref_id=lease.id,
            portal_type="TENANT",
        )
        assert response.status_code == 400
        assert "completed contract" in str(response.data)

    def test_a_co_tenant_qualifies(
        self, auth_client, make_user, make_contact, make_lease_for
    ):
        """architecture.md §2 says "leases where contact is tenant/**party**", so someone
        living under the lease gets a portal even though they are not the primary tenant."""
        pm = make_user("property_manager")
        co_tenant = make_contact()
        lease = make_lease_for()
        LeaseParty.objects.create(
            lease=lease, contact=co_tenant, party_type=LeaseParty.PartyType.CO_TENANT
        )

        response = invite(
            auth_client(pm),
            contact=co_tenant,
            ref_type="LEASE",
            ref_id=lease.id,
            portal_type="TENANT",
        )
        assert response.status_code == 201

    def test_a_guarantor_does_not_qualify(
        self, auth_client, make_user, make_contact, make_lease_for
    ):
        """They are liable for the lease, not resident under it — and the tenant portal shows
        a home's maintenance and rent history."""
        pm = make_user("property_manager")
        guarantor = make_contact()
        lease = make_lease_for()
        LeaseParty.objects.create(
            lease=lease, contact=guarantor, party_type=LeaseParty.PartyType.GUARANTOR
        )

        response = invite(
            auth_client(pm),
            contact=guarantor,
            ref_type="LEASE",
            ref_id=lease.id,
            portal_type="TENANT",
        )
        assert response.status_code == 400

    def test_a_transaction_with_no_deal_confirms_nobody(
        self, auth_client, make_user, make_contact, make_transaction
    ):
        """`Transaction.deal` is nullable and is the only path to a buyer, so a transaction
        recorded without one cannot vouch for anyone. Fail closed."""
        agent = make_user("agent")
        contact = make_contact()
        txn = make_transaction(contact=None)  # no deal

        response = invite(
            auth_client(agent),
            contact=contact,
            ref_type="TRANSACTION",
            ref_id=txn.id,
            portal_type="BUYER",
        )
        assert response.status_code == 400

    def test_a_nonexistent_contract_is_refused(
        self, auth_client, make_user, make_contact
    ):
        import uuid

        response = invite(
            auth_client(make_user("property_manager")),
            contact=make_contact(),
            ref_type="LEASE",
            ref_id=uuid.uuid4(),
            portal_type="TENANT",
        )
        assert response.status_code == 400

    def test_the_contract_type_must_match_the_client_type(
        self, auth_client, make_user, make_contact, make_lease_for
    ):
        """A buyer is not established by a lease."""
        contact = make_contact()
        lease = make_lease_for(tenant=contact)

        response = invite(
            auth_client(make_user("super_admin")),
            contact=contact,
            ref_type="LEASE",
            ref_id=lease.id,
            portal_type="BUYER",
        )
        assert response.status_code == 400
        assert "established by a transaction" in str(response.data)

    def test_one_portal_login_per_client(
        self, auth_client, portal_tenant
    ):
        """`PortalProfile.contact` is OneToOne, so a second invite must be refused with a
        clear message rather than an IntegrityError."""
        response = invite(
            auth_client(portal_tenant["inviter"]),
            contact=portal_tenant["contact"],
            ref_type="LEASE",
            ref_id=portal_tenant["lease"].id,
            portal_type="TENANT",
        )
        assert response.status_code == 400
        assert "already has portal access" in str(response.data)

    def test_a_contact_with_no_email_cannot_be_invited(
        self, auth_client, make_user, make_contact, make_lease_for
    ):
        """There would be nothing to log in with."""
        pm = make_user("property_manager")
        contact = make_contact(email=None)
        lease = make_lease_for(tenant=contact)

        response = invite(
            auth_client(pm),
            contact=contact,
            ref_type="LEASE",
            ref_id=lease.id,
            portal_type="TENANT",
        )
        assert response.status_code == 400
        assert "no email" in str(response.data)


class TestFailClosed:
    """Silence means refusal. If nobody can vouch for the contract — the owning app is not
    installed, its receiver is disconnected — access must not be granted by default."""

    def test_no_verifier_means_no_access(
        self, auth_client, make_user, make_contact, make_lease_for, monkeypatch
    ):
        from apps.identity import signals
        from apps.property_ops import receivers

        signals.verify_portal_eligibility.disconnect(
            receivers.verify_lease_eligibility,
            dispatch_uid="property_ops.verify_lease_eligibility",
        )
        try:
            pm = make_user("property_manager")
            contact = make_contact()
            lease = make_lease_for(tenant=contact)

            response = invite(
                auth_client(pm),
                contact=contact,
                ref_type="LEASE",
                ref_id=lease.id,
                portal_type="TENANT",
            )
            assert response.status_code == 400
            assert not User.objects.filter(email=contact.email).exists()
        finally:
            receivers.connect()


class TestPortalLogin:
    def test_the_client_can_log_in(self, api_client, portal_tenant):
        response = api_client.post(
            TOKEN_URL,
            {"email": portal_tenant["user"].email, "password": CLIENT_PASSWORD},
        )
        assert response.status_code == 200
        assert "access" in response.data

    def test_they_must_change_the_password_the_inviter_set(
        self, auth_client, portal_tenant
    ):
        assert portal_tenant["user"].must_change_password is True
        response = auth_client(portal_tenant["user"]).get(PORTAL_URL)
        assert response.status_code == 403

    def test_suspending_access_stops_login(
        self, api_client, auth_client, portal_tenant
    ):
        """A client's right to be here expires with their contract, so the check runs at
        every login rather than only at invitation (SRS 3.11.2)."""
        profile = portal_tenant["user"].portal_profile
        response = auth_client(portal_tenant["inviter"]).delete(
            f"{PORTAL_URL}{profile.id}/"
        )
        assert response.status_code == 204

        profile.refresh_from_db()
        assert profile.eligibility_status == PortalProfile.EligibilityStatus.SUSPENDED
        # Suspended, not deleted: the contract reference is history worth keeping.
        assert PortalProfile.objects.filter(pk=profile.pk).exists()

        login = api_client.post(
            TOKEN_URL,
            {"email": portal_tenant["user"].email, "password": CLIENT_PASSWORD},
        )
        assert login.status_code == 401
        assert "not active" in str(login.data)


class TestIsolation:
    """SRS 3.11.6 / 3.17.6 — "no portal user may view another client's private data". A hard
    rule, so it gets direct tests rather than being inferred from the scoping layer."""

    @pytest.fixture
    def second_client(self, auth_client, make_user, make_contact, make_lease_for):
        pm = make_user("property_manager")
        contact = make_contact()
        lease = make_lease_for(tenant=contact)
        invite(
            auth_client(pm),
            contact=contact,
            ref_type="LEASE",
            ref_id=lease.id,
            portal_type="TENANT",
        )
        return User.objects.get(email=contact.email)

    def _unblocked(self, user):
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        return user

    def test_a_client_sees_only_their_own_profile(
        self, auth_client, portal_tenant, second_client
    ):
        me = self._unblocked(portal_tenant["user"])
        response = auth_client(me).get(PORTAL_URL)

        assert response.status_code == 200
        ids = {row["id"] for row in response.data["results"]}
        assert ids == {str(me.portal_profile.id)}
        assert str(second_client.portal_profile.id) not in ids

    def test_a_client_cannot_fetch_another_client_directly(
        self, auth_client, portal_tenant, second_client
    ):
        me = self._unblocked(portal_tenant["user"])
        response = auth_client(me).get(f"{PORTAL_URL}{second_client.portal_profile.id}/")
        assert response.status_code == 404

    def test_a_client_sees_only_themselves_in_the_user_list(
        self, auth_client, portal_tenant, agent
    ):
        me = self._unblocked(portal_tenant["user"])
        response = auth_client(me).get(USERS_URL)

        assert response.status_code == 200
        assert {row["id"] for row in response.data["results"]} <= {str(me.id)}

    def test_a_client_cannot_register_staff(self, auth_client, portal_tenant, branch):
        me = self._unblocked(portal_tenant["user"])
        response = auth_client(me).post(
            USERS_URL,
            {
                "email": "sneaky@acme.test",
                "first_name": "S",
                "last_name": "N",
                "password": "what-a-pass-9911",
                "role_code": "agent",
                "branch": str(branch.id),
            },
        )
        assert response.status_code == 403

    def test_clients_do_not_appear_in_the_staff_directory(
        self, auth_client, owner, portal_tenant
    ):
        """A client is not a colleague. Listing them to staff would leak the client base."""
        response = auth_client(owner).get(USERS_URL)
        ids = {row["id"] for row in response.data["results"]}
        assert str(portal_tenant["user"].id) not in ids

    def test_the_client_still_has_a_working_session(self, auth_client, portal_tenant):
        me = self._unblocked(portal_tenant["user"])
        response = auth_client(me).get(ME_URL)

        assert response.status_code == 200
        assert [r["code"] for r in response.data["roles"]] == ["portal"]
        assert response.data["data_scopes"] == ["PORTAL_OWN"]
        assert response.data["grantable_role_codes"] == []


def test_staff_registration_still_refuses_the_portal_role(auth_client, super_admin, branch):
    """Two doors, one rule: a client login is never a staff role grant, and the refusal
    points at the door that does work."""
    response = auth_client(super_admin).post(
        USERS_URL,
        {
            "email": "client@example.test",
            "first_name": "C",
            "last_name": "L",
            "password": "what-a-pass-9911",
            "role_code": "portal",
            "branch": str(branch.id),
        },
    )
    assert response.status_code == 400
    assert "portal-users" in str(response.data)


def test_a_seller_qualifies_through_owning_the_property(
    auth_client, make_user, make_contact, make_property, make_transaction
):
    """A buy-side deal names the buyer as primary contact, so the seller is reachable only
    through `inventory.PropertyOwner` on the property being sold."""
    from apps.inventory.models import PropertyOwner

    agent = make_user("agent")
    buyer = make_contact()
    seller = make_contact()
    prop = make_property()
    PropertyOwner.objects.create(
        property=prop,
        contact=seller,
        ownership_percentage=100,
        is_primary_owner=True,
        start_date="2020-01-01",
    )
    txn = make_transaction(contact=buyer, property=prop)

    response = invite(
        auth_client(agent),
        contact=seller,
        ref_type="TRANSACTION",
        ref_id=txn.id,
        portal_type="SELLER",
    )
    assert response.status_code == 201, response.data


def test_owning_a_property_does_not_make_you_its_buyer(
    auth_client, make_user, make_contact, make_property, make_transaction
):
    """The owner path is evidence of selling, not of buying."""
    from apps.inventory.models import PropertyOwner

    agent = make_user("agent")
    buyer = make_contact()
    seller = make_contact()
    prop = make_property()
    PropertyOwner.objects.create(
        property=prop,
        contact=seller,
        ownership_percentage=100,
        is_primary_owner=True,
        start_date="2020-01-01",
    )
    txn = make_transaction(contact=buyer, property=prop)

    response = invite(
        auth_client(agent),
        contact=seller,
        ref_type="TRANSACTION",
        ref_id=txn.id,
        portal_type="BUYER",
    )
    assert response.status_code == 400
