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
