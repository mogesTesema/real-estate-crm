"""Shared fixtures.

Deliberately much smaller than the pre-v3.2 suite: with row visibility moved out of Postgres
RLS and into `identity.selectors.apply_scope` (architecture.md §2), there is no session GUC to
bind, so the old autouse tenant-binding/reset fixture is gone. Nothing here needs to run
before a test to make isolation correct.
"""
import pytest

from apps.identity.models import Branch, Company, Role, Team, User, UserRole


@pytest.fixture
def company(db):
    return Company.objects.create(
        name="Acme Realty",
        legal_name="Acme Realty LLC",
        email="hq@acme.test",
        phone="+15550100",
        country="AE",
        timezone="Asia/Dubai",
        default_currency="AED",
    )


@pytest.fixture
def branch(company):
    return Branch.objects.create(company=company, name="Downtown", code="DT")


@pytest.fixture
def team(branch):
    return Team.objects.create(branch=branch, name="Resale A", code="RA")


@pytest.fixture
def roles(db):
    """The canonical system roles, keyed by code.

    Mirrors the seed in identity's data migration; fixtures read it rather than
    hardcoding scopes so a change to the doctrine shows up here as a failure.
    """
    return {role.code: role for role in Role.objects.all()}


@pytest.fixture
def make_user(branch, team):
    """Build a user, optionally attaching one of the canonical roles."""
    counter = iter(range(1, 1000))

    def _make(role_code=None, *, email=None, **kwargs):
        n = next(counter)
        kwargs.setdefault("branch", branch)
        kwargs.setdefault("team", team)
        user = User.objects.create_user(
            email=email or f"user{n}@acme.test",
            password="test-pass-12345",
            first_name="Test",
            last_name=f"User{n}",
            **kwargs,
        )
        if role_code:
            UserRole.objects.create(user=user, role=Role.objects.get(code=role_code))
        return user

    return _make


@pytest.fixture
def agent(make_user):
    return make_user("agent")


@pytest.fixture
def owner(make_user):
    return make_user("owner")


@pytest.fixture
def property_type(db):
    from apps.inventory.models import PropertyType

    return PropertyType.objects.create(
        name="Apartment", code="APARTMENT", category=PropertyType.Category.RESIDENTIAL
    )


@pytest.fixture
def make_property(property_type, make_user):
    """A property needs a Property Manager — managed_by is required (SRS 3.3.10)."""
    from apps.inventory.models import Property

    manager = make_user("property_manager")
    counter = iter(range(1, 1000))

    def _make(**kwargs):
        n = next(counter)
        kwargs.setdefault("property_type", property_type)
        kwargs.setdefault("managed_by", manager)
        kwargs.setdefault("title", f"Property {n}")
        kwargs.setdefault("address_line_1", f"{n} Marina Walk")
        kwargs.setdefault("city", "Dubai")
        kwargs.setdefault("country", "AE")
        kwargs.setdefault("status", Property.Status.AVAILABLE)
        return Property.objects.create(**kwargs)

    return _make


@pytest.fixture
def pipeline(db):
    from apps.crm.models import Pipeline, PipelineStage

    pipe = Pipeline.objects.create(
        name="Residential Sales",
        pipeline_type=Pipeline.PipelineType.RESIDENTIAL_SALES,
        is_default=True,
    )
    for order, (name, code, prob, won, lost) in enumerate(
        [
            ("New", "NEW", 10, False, False),
            ("Viewing", "VIEWING", 45, False, False),
            ("Won", "WON", 100, True, False),
            ("Lost", "LOST", 0, False, True),
        ]
    ):
        PipelineStage.objects.create(
            pipeline=pipe, name=name, code=code, sort_order=order,
            probability=prob, is_won=won, is_lost=lost,
        )
    return pipe


@pytest.fixture
def make_deal(pipeline, make_user):
    from apps.contacts.models import Contact
    from apps.crm.models import Deal

    owner = make_user("agent")
    counter = iter(range(1, 1000))

    def _make(**kwargs):
        n = next(counter)
        kwargs.setdefault("reference_code", f"DL-{n:04d}")
        kwargs.setdefault(
            "primary_contact",
            Contact.objects.create(
                contact_type=Contact.ContactType.PERSON, first_name="Deal", last_name=f"C{n}"
            ),
        )
        kwargs.setdefault("pipeline", pipeline)
        kwargs.setdefault("stage", pipeline.stages.first())
        kwargs.setdefault("owner", owner)
        kwargs.setdefault("title", f"Deal {n}")
        kwargs.setdefault("deal_type", "SALE")
        kwargs.setdefault("estimated_value", 1000000)
        kwargs.setdefault("currency", "AED")
        kwargs.setdefault("probability", 10)
        return Deal.objects.create(**kwargs)

    return _make


@pytest.fixture
def make_lease(make_property, make_user):
    """Leases default to ACTIVE so they participate in the overlap exclusion."""
    from apps.contacts.models import Contact
    from apps.property_ops.models import Lease

    pm = make_user("property_manager")
    counter = iter(range(1, 1000))

    def _make(**kwargs):
        n = next(counter)
        kwargs.setdefault("reference_code", f"LSE-{n:04d}")
        if "property" not in kwargs and "unit" not in kwargs:
            kwargs["property"] = make_property()
        kwargs.setdefault(
            "tenant",
            Contact.objects.create(
                contact_type=Contact.ContactType.PERSON, first_name="Ten", last_name=f"A{n}"
            ),
        )
        kwargs.setdefault(
            "landlord",
            Contact.objects.create(
                contact_type=Contact.ContactType.PERSON, first_name="Land", last_name=f"L{n}"
            ),
        )
        kwargs.setdefault("property_manager", pm)
        kwargs.setdefault("lease_type", Lease.LeaseType.RESIDENTIAL)
        kwargs.setdefault("start_date", "2026-01-01")
        kwargs.setdefault("end_date", "2026-12-31")
        kwargs.setdefault("rent_amount", 100000)
        kwargs.setdefault("billing_frequency", Lease.BillingFrequency.MONTHLY)
        kwargs.setdefault("security_deposit", 10000)
        kwargs.setdefault("status", Lease.Status.ACTIVE)
        kwargs.setdefault("created_by", pm)
        return Lease.objects.create(**kwargs)

    return _make


# --- API fixtures -----------------------------------------------------------
#
# The suite was ORM-level until the identity API landed; these are the first HTTP fixtures.


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def auth_client():
    """Build an APIClient authenticated as a given user.

    Uses force_authenticate rather than a real token so tests exercise view logic without
    paying for a login round-trip; test_auth.py covers the real token flow end to end.
    """
    from rest_framework.test import APIClient

    def _as(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    return _as


@pytest.fixture
def other_branch(company):
    """A second branch, for proving branch-bound registrars cannot cross over."""
    from apps.identity.models import Branch

    return Branch.objects.create(company=company, name="Marina", code="MRN")


@pytest.fixture
def super_admin(make_user):
    return make_user("super_admin")


@pytest.fixture
def manager(make_user):
    return make_user("manager")


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    """Throttle counters live in the cache and outlive a test's database rollback.

    Without this, a test that exhausts a rate limit leaks the counter into whatever runs
    next — the flake is real but looks like it belongs to the innocent test.
    """
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


# --- Contract fixtures, for portal eligibility ------------------------------


@pytest.fixture
def make_contact(db):
    """A client of the firm. Portal access is granted to a contact, never invented."""
    from apps.contacts.models import Contact

    counter = iter(range(1, 1000))

    def _make(**kwargs):
        n = next(counter)
        kwargs.setdefault("contact_type", Contact.ContactType.PERSON)
        kwargs.setdefault("first_name", "Client")
        kwargs.setdefault("last_name", f"Number{n}")
        kwargs.setdefault("email", f"client{n}@example.test")
        return Contact.objects.create(**kwargs)

    return _make


@pytest.fixture
def make_transaction(make_property, make_deal):
    """A sale. `Transaction.deal` is nullable, and a transaction without one can confirm
    no contact at all — which some tests rely on."""
    from apps.crm.models import Transaction

    counter = iter(range(1, 1000))

    def _make(*, contact=None, status=None, property=None, **kwargs):
        n = next(counter)
        deal = make_deal(primary_contact=contact) if contact is not None else None
        return Transaction.objects.create(
            deal=deal,
            property=property or make_property(),
            transaction_type=Transaction.TransactionType.SALE,
            reference_code=f"TXN-{n:04d}",
            gross_amount=1000000,
            currency="AED",
            transaction_date="2026-01-01",
            status=status or Transaction.Status.COMPLETED,
            **kwargs,
        )

    return _make


@pytest.fixture
def make_lease_for(make_lease):
    """A lease with explicit tenant and landlord contacts."""

    def _make(*, tenant=None, landlord=None, status=None, **kwargs):
        from apps.property_ops.models import Lease

        if tenant is not None:
            kwargs["tenant"] = tenant
        if landlord is not None:
            kwargs["landlord"] = landlord
        kwargs.setdefault("status", status or Lease.Status.ACTIVE)
        return make_lease(**kwargs)

    return _make


@pytest.fixture
def portal_tenant(auth_client, make_user, make_contact, make_lease_for):
    """A rental tenant with an active lease and a working portal login."""
    from apps.identity.models import User

    pm = make_user("property_manager")
    contact = make_contact()
    lease = make_lease_for(tenant=contact)

    response = auth_client(pm).post(
        "/api/v1/portal-users/",
        {
            "contact_id": str(contact.id),
            "portal_type": "TENANT",
            "contract_ref_type": "LEASE",
            "contract_ref_id": str(lease.id),
            "password": "client-pass-55512",
        },
    )
    assert response.status_code == 201, response.data
    user = User.objects.get(email=contact.email)
    return {"user": user, "contact": contact, "lease": lease, "inviter": pm}
