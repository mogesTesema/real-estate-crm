"""`identity.selectors.apply_scope` — the mandatory row-visibility layer (architecture.md §2).

Tested directly rather than only through HTTP, because every list endpoint in every future
module runs through this function; a bug here is a data leak in modules that do not exist yet.
"""
import pytest

from apps.identity.models import Role, User, UserRole
from apps.identity.selectors import apply_scope, scopes_for, visible_users


def scoped(user):
    return set(apply_scope(User.objects.all(), user, "user").values_list("id", flat=True))


@pytest.fixture
def cast(make_user, other_branch):
    """One user per scope in the main branch, plus an outsider in another branch."""
    from apps.identity.models import Team

    other_team = Team.objects.create(branch=other_branch, name="Marina A", code="MA")
    return {
        "owner": make_user("owner"),
        "manager": make_user("manager"),
        "agent": make_user("agent"),
        "colleague": make_user("agent"),
        "finance": make_user("finance"),
        "pm": make_user("property_manager"),
        "outsider": make_user("agent", branch=other_branch, team=other_team),
    }


class TestPerScope:
    def test_all_sees_everyone(self, cast):
        assert cast["outsider"].id in scoped(cast["owner"])

    def test_branch_sees_the_branch_only(self, cast):
        visible = scoped(cast["manager"])
        assert cast["agent"].id in visible
        assert cast["outsider"].id not in visible

    def test_own_sees_branch_colleagues(self, cast):
        """A deliberate policy choice: a directory exists so colleagues can be picked as
        assignees. Row breadth is widened; column breadth is narrowed instead — the API
        serves these users a summary serializer."""
        visible = scoped(cast["agent"])
        assert cast["colleague"].id in visible
        assert cast["outsider"].id not in visible

    def test_managed_properties_behaves_like_own(self, cast):
        visible = scoped(cast["pm"])
        assert cast["agent"].id in visible
        assert cast["outsider"].id not in visible

    def test_module_wide_scopes_see_everyone(self, cast):
        """FINANCE_ALL and MARKETING_ALL are module- not org-scoped, but their holders are
        agency-level staff, so for a *people* list they read as agency-wide."""
        assert cast["outsider"].id in scoped(cast["finance"])

    def test_team_scope_sees_the_team(self, make_user, roles, team):
        """No seeded role uses TEAM, but the enum value exists and the branch says a
        team-scoped manager should use it — so the predicate must work."""
        from apps.identity.models import Team

        scoped_role = Role.objects.create(
            name="Team Manager", code="team_manager", data_scope=Role.DataScope.TEAM
        )
        lead = make_user(None)
        UserRole.objects.create(user=lead, role=scoped_role)

        teammate = make_user("agent")
        other_team = Team.objects.create(branch=team.branch, name="Resale B", code="RB")
        stranger = make_user("agent", team=other_team)

        visible = scoped(lead)
        assert teammate.id in visible
        assert stranger.id not in visible

    def test_portal_own_sees_only_itself(self, make_user):
        portal_user = make_user("portal")
        assert scoped(portal_user) == {portal_user.id}


class TestEdgeCases:
    def test_a_user_with_no_roles_sees_nothing(self, make_user, cast):
        """Not 'everything'. A scoping layer that fails open is worse than none."""
        roleless = make_user(None)
        assert scoped(roleless) == set()

    def test_an_anonymous_user_sees_nothing(self, db):
        from django.contrib.auth.models import AnonymousUser

        assert apply_scope(User.objects.all(), AnonymousUser(), "user").count() == 0

    def test_a_django_superuser_is_unfiltered(self, make_user, cast):
        root = make_user(None)
        root.is_superuser = True
        root.save(update_fields=["is_superuser"])
        assert cast["outsider"].id in scoped(root)

    def test_branchless_users_do_not_all_see_each_other(self, make_user):
        """branch_id=None matching branch_id=None would make every unplaced user visible to
        every other — the classic null-comparison scoping leak."""
        a = make_user("agent", branch=None, team=None)
        make_user("agent", branch=None, team=None)  # a second unplaced user
        assert scoped(a) == {a.id}

    def test_an_unregistered_resource_raises(self, agent):
        """A silent fallback to the unfiltered queryset is how scoping layers stop scoping."""
        with pytest.raises(KeyError):
            apply_scope(User.objects.all(), agent, "deal")


class TestMultipleRoles:
    """UserRole has no is_primary flag, so a user may hold several roles with no defined
    precedence. Predicates are OR-ed rather than ranked."""

    def test_the_union_is_granted(self, make_user, cast, other_branch):
        both = make_user("agent")
        UserRole.objects.create(user=both, role=Role.objects.get(code="owner"))

        assert scopes_for(both) == {"OWN", "ALL"}
        # ALL widens what OWN alone would allow.
        assert cast["outsider"].id in scoped(both)

    def test_a_narrow_role_never_narrows_a_broad_one(self, make_user, cast):
        """OR, not AND: adding a restrictive role must not take access away."""
        owner_then_agent = cast["owner"]
        UserRole.objects.create(
            user=owner_then_agent, role=Role.objects.get(code="agent")
        )
        assert cast["outsider"].id in scoped(owner_then_agent)


def test_visible_users_excludes_inactive_and_deleted(make_user, owner):
    from django.utils import timezone

    active = make_user("agent")
    inactive = make_user("agent")
    inactive.is_active = False
    inactive.save(update_fields=["is_active"])
    removed = make_user("agent")
    removed.deleted_at = timezone.now()
    removed.save(update_fields=["deleted_at"])

    found = set(visible_users(owner).values_list("id", flat=True))

    assert active.id in found
    assert inactive.id not in found
    assert removed.id not in found
