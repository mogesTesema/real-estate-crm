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
