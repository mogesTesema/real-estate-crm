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
            apply_scope(User.objects.all(), agent, "invoice")


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


# ===========================================================================================
# Domain resources
#
# Every resource registered by a domain app, against every `data_scope`. §2 makes apply_scope
# the one place row visibility is decided, so a hole here is a data leak in whichever endpoint
# lands next — these are written before the endpoints exist, on purpose.
# ===========================================================================================

from django.utils import timezone  # noqa: E402

from apps.core.choices import ScopedEntityType  # noqa: E402
from apps.identity.models import RecordShare  # noqa: E402

DataScope = Role.DataScope


@pytest.fixture
def with_scope(make_user):
    """A user holding exactly one role with the given `data_scope`.

    Built from an ad-hoc role rather than the seeded codes because §4 wants custom roles to
    work "without hardcoded role-name switches" — and because no seeded role carries TEAM.
    """
    from apps.identity.models import UserRole

    counter = iter(range(1, 500))

    def _make(data_scope, **kwargs):
        n = next(counter)
        role = Role.objects.create(
            code=f"t_{data_scope.lower()}_{n}",
            name=f"Test {data_scope}",
            data_scope=data_scope,
        )
        user = make_user(**kwargs)
        UserRole.objects.create(user=user, role=role)
        return user

    return _make


def visible(user, resource, model):
    return set(
        apply_scope(model._default_manager.all(), user, resource).values_list("id", flat=True)
    )


# --- contact -------------------------------------------------------------------------


class TestContactScope:
    @pytest.fixture
    def setup(self, with_scope, make_contact, other_branch):
        from apps.identity.models import Team

        other_team = Team.objects.create(branch=other_branch, name="Far", code="FAR")
        mine = with_scope(DataScope.OWN)
        theirs = with_scope(DataScope.OWN, branch=other_branch, team=other_team)
        return {
            "agent": mine,
            "outsider": theirs,
            "ours": make_contact(assigned_agent=mine),
            "colleagues": make_contact(assigned_agent=with_scope(DataScope.OWN)),
            "far": make_contact(assigned_agent=theirs),
            "unassigned": make_contact(),
        }

    def test_own_sees_only_their_own_contacts(self, setup, with_scope):
        from apps.contacts.models import Contact

        assert visible(setup["agent"], "contact", Contact) == {setup["ours"].id}

    def test_branch_sees_the_branch(self, setup, with_scope):
        from apps.contacts.models import Contact

        seen = visible(with_scope(DataScope.BRANCH), "contact", Contact)
        assert {setup["ours"].id, setup["colleagues"].id} <= seen
        assert setup["far"].id not in seen

    def test_all_sees_everything_including_unassigned(self, setup, with_scope):
        from apps.contacts.models import Contact

        assert visible(with_scope(DataScope.ALL), "contact", Contact) == {
            c.id for c in Contact.objects.all()
        }

    def test_an_unassigned_contact_is_invisible_to_own(self, setup):
        """assigned_agent IS NULL must not match `assigned_agent = me`. The null-comparison
        leak, in its second-most common disguise."""
        from apps.contacts.models import Contact

        assert setup["unassigned"].id not in visible(setup["agent"], "contact", Contact)

    def test_finance_and_marketing_see_the_whole_contact_base(self, setup, with_scope):
        """Deliberate, and the one place the two module-wide scopes read as agency-wide:
        neither invoicing nor a campaign can work from a slice of the contact base. Reaching
        the endpoint at all stays a function-level permission question."""
        from apps.contacts.models import Contact

        everything = {c.id for c in Contact.objects.all()}
        assert visible(with_scope(DataScope.FINANCE_ALL), "contact", Contact) == everything
        assert visible(with_scope(DataScope.MARKETING_ALL), "contact", Contact) == everything

    def test_a_property_manager_sees_the_owners_of_what_they_manage(
        self, with_scope, make_contact, make_property
    ):
        from apps.contacts.models import Contact
        from apps.inventory.models import PropertyOwner

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        owner_contact = make_contact()
        stranger = make_contact()
        PropertyOwner.objects.create(
            property=make_property(managed_by=pm),
            contact=owner_contact,
            ownership_percentage=100,
            is_primary_owner=True,
            start_date="2026-01-01",
        )
        seen = visible(pm, "contact", Contact)
        assert owner_contact.id in seen
        assert stranger.id not in seen


# --- lead ----------------------------------------------------------------------------


class TestLeadScope:
    """The lead table is where the two design corrections in this pass are proved: a lead has
    **two** anchors, and the module-wide scopes are not agency-wide."""

    @pytest.fixture
    def make_lead(self, make_contact):
        from apps.crm.models import Lead

        counter = iter(range(1, 500))

        def _make(**kwargs):
            n = next(counter)
            kwargs.setdefault("contact", make_contact())
            kwargs.setdefault("lead_type", Lead.LeadType.BUY)
            kwargs.setdefault("title", f"Lead {n}")
            return Lead.objects.create(**kwargs)

        return _make

    def test_own_sees_leads_assigned_to_them(self, with_scope, make_lead):
        from apps.crm.models import Lead

        me = with_scope(DataScope.OWN)
        mine = make_lead(assigned_agent=me)
        make_lead(assigned_agent=with_scope(DataScope.OWN))
        assert visible(me, "lead", Lead) == {mine.id}

    def test_own_sees_the_unclaimed_team_pool(self, with_scope, make_lead, team):
        """SRS 3.1.5 routing may assign a lead to a team for round-robin, leaving
        assigned_agent_id null. An OWN predicate written only against `assigned_agent` hides
        the entire pool from the agents meant to work it — which is why `lead` carries two
        anchors and §2's doctrine now says so."""
        from apps.crm.models import Lead

        me = with_scope(DataScope.OWN)
        pooled = make_lead(assigned_team=team, assigned_agent=None)
        assert pooled.assigned_agent_id is None
        assert pooled.id in visible(me, "lead", Lead)

    def test_own_does_not_see_another_teams_pool(self, with_scope, make_lead, other_branch):
        from apps.crm.models import Lead
        from apps.identity.models import Team

        other_team = Team.objects.create(branch=other_branch, name="Far", code="FART")
        pooled = make_lead(assigned_team=other_team)
        assert pooled.id not in visible(with_scope(DataScope.OWN), "lead", Lead)

    def test_branch_reaches_team_pooled_leads_too(
        self, with_scope, make_lead, team, other_branch
    ):
        """A team-pooled lead has no assigned_agent to derive a branch from, so BRANCH has to
        read the branch off the team as well."""
        from apps.crm.models import Lead
        from apps.identity.models import Team

        far_team = Team.objects.create(branch=other_branch, name="Far", code="FARB")
        near = make_lead(assigned_team=team)
        far = make_lead(assigned_team=far_team)
        seen = visible(with_scope(DataScope.BRANCH), "lead", Lead)
        assert near.id in seen
        assert far.id not in seen

    def test_team_scope_sees_the_team(self, with_scope, make_lead, team):
        from apps.crm.models import Lead

        colleague = with_scope(DataScope.OWN)  # same team, via make_user's default
        mine = make_lead(assigned_agent=colleague)
        pooled = make_lead(assigned_team=team)
        assert {mine.id, pooled.id} <= visible(with_scope(DataScope.TEAM), "lead", Lead)

    def test_marketing_gets_no_blanket_grant_over_leads(self, with_scope, make_lead):
        """§2: MARKETING_ALL covers "campaign/source/landing objects agency-wide; **leads per
        permission/grant**". An earlier sketch lumped MARKETING_ALL and FINANCE_ALL in with
        ALL behind a shared frozenset, which would have handed marketing every lead in the
        company the moment a generic builder consulted it."""
        from apps.crm.models import Lead

        make_lead(assigned_agent=with_scope(DataScope.OWN))
        assert visible(with_scope(DataScope.MARKETING_ALL), "lead", Lead) == set()
        assert visible(with_scope(DataScope.FINANCE_ALL), "lead", Lead) == set()

    def test_marketing_still_sees_a_lead_shared_with_them(self, with_scope, make_lead):
        """"per permission/grant" — the grant is identity_record_share, which apply_scope
        unions in regardless of what the roles give."""
        from apps.crm.models import Lead

        marketer = with_scope(DataScope.MARKETING_ALL)
        lead = make_lead(assigned_agent=with_scope(DataScope.OWN))
        RecordShare.objects.create(
            entity_type=ScopedEntityType.LEAD,
            entity_id=lead.id,
            shared_with_user=marketer,
            access_level=RecordShare.AccessLevel.VIEW,
            shared_by=with_scope(DataScope.ALL),
        )
        assert visible(marketer, "lead", Lead) == {lead.id}

    def test_a_property_manager_sees_leads_targeting_what_they_manage(
        self, with_scope, make_lead, make_property
    ):
        from apps.crm.models import Lead

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        on_target = make_lead(target_property=make_property(managed_by=pm))
        elsewhere = make_lead(target_property=make_property())
        seen = visible(pm, "lead", Lead)
        assert on_target.id in seen
        assert elsewhere.id not in seen

    def test_a_portal_client_sees_no_leads(self, portal_tenant, make_lead):
        """A portal profile follows a completed contract (SRS 3.11.2); an open inquiry is not
        portal data."""
        from apps.crm.models import Lead

        make_lead(contact=portal_tenant["contact"])
        assert visible(portal_tenant["user"], "lead", Lead) == set()


# --- deal ----------------------------------------------------------------------------


class TestDealScope:
    def test_own_sees_deals_they_own(self, with_scope, make_deal):
        from apps.crm.models import Deal

        me = with_scope(DataScope.OWN)
        mine = make_deal(owner=me)
        make_deal()
        assert visible(me, "deal", Deal) == {mine.id}

    def test_branch_sees_the_branch(self, with_scope, make_deal, other_branch):
        from apps.crm.models import Deal
        from apps.identity.models import Team

        far_team = Team.objects.create(branch=other_branch, name="Far", code="FARD")
        near = make_deal(owner=with_scope(DataScope.OWN))
        far = make_deal(owner=with_scope(DataScope.OWN, branch=other_branch, team=far_team))
        seen = visible(with_scope(DataScope.BRANCH), "deal", Deal)
        assert near.id in seen
        assert far.id not in seen

    def test_finance_sees_every_deal(self, with_scope, make_deal):
        """A deal is where the money originates — commissions and transactions hang off it
        (§11) — so it falls under §2's "finance objects agency-wide"."""
        from apps.crm.models import Deal

        make_deal()
        assert visible(with_scope(DataScope.FINANCE_ALL), "deal", Deal) == {
            d.id for d in Deal.objects.all()
        }

    def test_a_property_manager_sees_deals_on_their_properties(
        self, with_scope, make_deal, make_property
    ):
        from apps.crm.models import Deal, DealProperty

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        theirs = make_deal()
        DealProperty.objects.create(deal=theirs, property=make_property(managed_by=pm))
        elsewhere = make_deal()
        DealProperty.objects.create(deal=elsewhere, property=make_property())
        seen = visible(pm, "deal", Deal)
        assert theirs.id in seen
        assert elsewhere.id not in seen

    def test_a_portal_client_sees_only_their_own_closed_deals(
        self, portal_tenant, make_deal
    ):
        """§2's portal path: "Buyer/Seller: own **closed** deals/offers/documents linked to
        the contact". An open negotiation is not portal data."""
        from apps.crm.models import Deal

        contact = portal_tenant["contact"]
        won = make_deal(primary_contact=contact, status=Deal.Status.WON)
        make_deal(primary_contact=contact, status=Deal.Status.OPEN)
        make_deal(status=Deal.Status.WON)  # someone else's, also closed
        assert visible(portal_tenant["user"], "deal", Deal) == {won.id}


# --- property and listing -------------------------------------------------------------


class TestPropertyScope:
    def test_managed_properties_is_the_anchor(self, with_scope, make_property):
        from apps.inventory.models import Property

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        mine = make_property(managed_by=pm)
        make_property()
        assert visible(pm, "property", Property) == {mine.id}

    def test_an_agent_reaches_the_property_behind_their_listing(
        self, with_scope, make_property
    ):
        """Without this arm an agent could not open the property their own listing is on."""
        from apps.inventory.models import Listing, Property

        agent = with_scope(DataScope.OWN)
        listed = make_property()
        Listing.objects.create(
            property=listed,
            reference_code="LST-SC-1",
            listing_type=Listing.ListingType.SALE,
            title="Mine",
            status=Listing.Status.ACTIVE,
            assigned_agent=agent,
        )
        other = make_property()
        seen = visible(agent, "property", Property)
        assert listed.id in seen
        assert other.id not in seen

    def test_a_landlord_on_the_portal_sees_the_properties_they_own(
        self, portal_tenant, make_property
    ):
        from apps.inventory.models import Property, PropertyOwner

        mine = make_property()
        PropertyOwner.objects.create(
            property=mine,
            contact=portal_tenant["contact"],
            ownership_percentage=100,
            is_primary_owner=True,
            start_date="2026-01-01",
        )
        make_property()  # someone else's
        assert visible(portal_tenant["user"], "property", Property) == {mine.id}


class TestListingScope:
    @pytest.fixture
    def make_listing(self, make_property):
        from apps.inventory.models import Listing

        counter = iter(range(1, 500))

        def _make(**kwargs):
            n = next(counter)
            kwargs.setdefault("property", make_property())
            kwargs.setdefault("reference_code", f"LST-S{n:03d}")
            kwargs.setdefault("listing_type", Listing.ListingType.SALE)
            kwargs.setdefault("title", f"Listing {n}")
            kwargs.setdefault("status", Listing.Status.ACTIVE)
            return Listing.objects.create(**kwargs)

        return _make

    def test_the_listing_agent_sees_it(self, with_scope, make_listing):
        from apps.inventory.models import Listing

        me = with_scope(DataScope.OWN)
        mine = make_listing(assigned_agent=me)
        make_listing(assigned_agent=with_scope(DataScope.OWN))
        assert visible(me, "listing", Listing) == {mine.id}

    def test_the_co_listing_agent_sees_it_too(self, with_scope, make_listing):
        """SRS 3.3.5 multi-agent assignment, added in v3.3. A second agent on the mandate who
        cannot see the mandate is not on it."""
        from apps.inventory.models import Listing

        co = with_scope(DataScope.OWN)
        shared = make_listing(assigned_agent=with_scope(DataScope.OWN), co_listing_agent=co)
        assert visible(co, "listing", Listing) == {shared.id}

    def test_a_property_manager_sees_listings_on_their_properties(
        self, with_scope, make_listing, make_property
    ):
        from apps.inventory.models import Listing

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        mine = make_listing(property=make_property(managed_by=pm))
        make_listing()
        assert visible(pm, "listing", Listing) == {mine.id}


# --- lease: the rental-tenant isolation boundary ---------------------------------------


class TestLeaseScope:
    """The Architecture Principles open by defining isolation as rental-tenant /
    portal-client isolation — "one portal client never sees another's private data". This
    class is that promise, tested."""

    def test_a_rental_tenant_sees_only_their_own_lease(
        self, portal_tenant, make_lease_for, make_contact
    ):
        from apps.property_ops.models import Lease

        make_lease_for(tenant=make_contact())  # a different tenant entirely
        assert visible(portal_tenant["user"], "lease", Lease) == {portal_tenant["lease"].id}

    def test_a_landlord_sees_the_leases_on_their_property(
        self, portal_tenant, make_lease_for
    ):
        from apps.property_ops.models import Lease

        as_landlord = make_lease_for(landlord=portal_tenant["contact"])
        seen = visible(portal_tenant["user"], "lease", Lease)
        assert as_landlord.id in seen

    def test_a_co_tenant_on_the_lease_sees_it(self, portal_tenant, make_lease_for, make_contact):
        from apps.property_ops.models import Lease, LeaseParty

        others = make_lease_for(tenant=make_contact())
        LeaseParty.objects.create(
            lease=others,
            contact=portal_tenant["contact"],
            party_type=LeaseParty.PartyType.CO_TENANT,
        )
        assert others.id in visible(portal_tenant["user"], "lease", Lease)

    def test_a_suspended_portal_profile_sees_nothing(self, portal_tenant):
        """Eligibility is not a one-time gate. Revoking it has to take the rows back."""
        from apps.identity.models import PortalProfile
        from apps.property_ops.models import Lease

        profile = portal_tenant["user"].portal_profile
        profile.eligibility_status = PortalProfile.EligibilityStatus.SUSPENDED
        profile.save(update_fields=["eligibility_status"])
        portal_tenant["user"].refresh_from_db()
        assert visible(portal_tenant["user"], "lease", Lease) == set()

    def test_the_property_manager_of_the_property_sees_the_lease(
        self, with_scope, make_lease, make_property
    ):
        from apps.property_ops.models import Lease

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        theirs = make_lease(property=make_property(managed_by=pm))
        make_lease()
        assert visible(pm, "lease", Lease) == {theirs.id}

    def test_marketing_sees_no_leases(self, with_scope, make_lease):
        from apps.property_ops.models import Lease

        make_lease()
        assert visible(with_scope(DataScope.MARKETING_ALL), "lease", Lease) == set()


# --- media: the one registered child resource ------------------------------------------


class TestMediaScope:
    """Media inherits from whichever parent it hangs off — property, unit, or listing — and
    all three columns are nullable, so it cannot inherit down a single path."""

    @pytest.fixture
    def make_media(self, db):
        from apps.inventory.models import Media

        counter = iter(range(1, 500))

        def _make(**kwargs):
            n = next(counter)
            kwargs.setdefault("media_type", Media.MediaType.PHOTO)
            kwargs.setdefault("storage_key", f"media/{n}.jpg")
            return Media.objects.create(**kwargs)

        return _make

    def test_media_follows_its_property(self, with_scope, make_media, make_property):
        from apps.inventory.models import Media

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        mine = make_media(property=make_property(managed_by=pm))
        make_media(property=make_property())
        assert visible(pm, "media", Media) == {mine.id}

    def test_media_follows_its_unit_through_the_property(
        self, with_scope, make_media, make_property
    ):
        from apps.inventory.models import Media, Unit

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        unit = Unit.objects.create(
            property=make_property(managed_by=pm), unit_number="101", status=Unit.Status.AVAILABLE
        )
        mine = make_media(unit=unit)
        assert visible(pm, "media", Media) == {mine.id}

    def test_media_follows_its_listing(self, with_scope, make_media, make_property):
        from apps.inventory.models import Listing, Media

        agent = with_scope(DataScope.OWN)
        listing = Listing.objects.create(
            property=make_property(),
            reference_code="LST-MD-1",
            listing_type=Listing.ListingType.SALE,
            title="Gallery",
            status=Listing.Status.ACTIVE,
            assigned_agent=agent,
        )
        mine = make_media(listing=listing)
        make_media(property=make_property())
        assert visible(agent, "media", Media) == {mine.id}

    def test_each_parent_arm_contributes_once(self, with_scope, make_media, make_property):
        """Media's rule ORs three nested arms. A row reachable through more than one of them —
        a photo on a listing whose property is also visible — must appear once, not twice.

        This is the invariant that pays for dropping `.distinct()` from apply_scope: `nested`
        subqueries instead of joining, so no arm can multiply the outer rows. A JOIN-based
        implementation would return this row twice.
        """
        from apps.inventory.models import Listing, Media

        viewer = with_scope(DataScope.ALL)
        prop = make_property()
        listing = Listing.objects.create(
            property=prop,
            reference_code="LST-MD-2",
            listing_type=Listing.ListingType.SALE,
            title="Both arms",
            status=Listing.Status.ACTIVE,
        )
        both = make_media(property=prop, listing=listing)
        rows = list(
            apply_scope(Media.objects.all(), viewer, "media").values_list("id", flat=True)
        )
        assert rows.count(both.id) == 1


# --- viewings and GPS field sessions ----------------------------------------------------


class TestViewingScope:
    @pytest.fixture
    def make_viewing(self, make_property, make_contact, make_user):
        from apps.crm.models import Viewing

        counter = iter(range(1, 500))

        def _make(**kwargs):
            next(counter)
            kwargs.setdefault("property", make_property())
            kwargs.setdefault("agent", make_user("agent"))
            kwargs.setdefault("contact", make_contact())
            kwargs.setdefault("scheduled_start", timezone.now())
            kwargs.setdefault("scheduled_end", timezone.now())
            return Viewing.objects.create(**kwargs)

        return _make

    def test_the_agent_running_it_sees_it(self, with_scope, make_viewing):
        from apps.crm.models import Viewing

        me = with_scope(DataScope.OWN)
        mine = make_viewing(agent=me)
        make_viewing()
        assert visible(me, "viewing", Viewing) == {mine.id}

    def test_a_portal_client_sees_appointments_in_their_own_name(
        self, portal_tenant, make_viewing
    ):
        from apps.crm.models import Viewing

        mine = make_viewing(contact=portal_tenant["contact"])
        make_viewing()
        assert visible(portal_tenant["user"], "viewing", Viewing) == {mine.id}


class TestFieldSessionScope:
    """GPS field tracking (SRS 3.16.5–3.16.7). Row visibility is only half the control here;
    3.16.7 also requires every supervisory read to be audited, which is the endpoint's job."""

    @pytest.fixture
    def make_session(self, make_user):
        from apps.crm.models import AgentFieldSession

        def _make(**kwargs):
            kwargs.setdefault("agent", make_user("agent"))
            kwargs.setdefault("session_type", AgentFieldSession.SessionType.VIEWING)
            kwargs.setdefault("started_at", timezone.now())
            return AgentFieldSession.objects.create(**kwargs)

        return _make

    def test_an_agent_sees_only_their_own_trail(self, with_scope, make_session):
        from apps.crm.models import AgentFieldSession

        me = with_scope(DataScope.OWN)
        mine = make_session(agent=me)
        make_session()
        assert visible(me, "field_session", AgentFieldSession) == {mine.id}

    def test_a_manager_sees_their_branch(self, with_scope, make_session, other_branch):
        from apps.crm.models import AgentFieldSession
        from apps.identity.models import Team

        far_team = Team.objects.create(branch=other_branch, name="Far", code="FARF")
        near = make_session(agent=with_scope(DataScope.OWN))
        far = make_session(
            agent=with_scope(DataScope.OWN, branch=other_branch, team=far_team)
        )
        seen = visible(with_scope(DataScope.BRANCH), "field_session", AgentFieldSession)
        assert near.id in seen
        assert far.id not in seen

    def test_scopes_the_resource_does_not_mention_see_nothing(self, with_scope, make_session):
        """Deny by default. A location trail has nothing to do with managing a property,
        running a campaign, or raising an invoice, and the registry says so by omission —
        `scopes.get(scope)` returning None must mean "nothing", never "everything"."""
        from apps.crm.models import AgentFieldSession

        make_session()
        for scope in (
            DataScope.MANAGED_PROPERTIES,
            DataScope.FINANCE_ALL,
            DataScope.MARKETING_ALL,
            DataScope.PORTAL_OWN,
        ):
            assert visible(with_scope(scope), "field_session", AgentFieldSession) == set()


# --- explicit record shares -------------------------------------------------------------


class TestRecordShares:
    """§2: "Explicit cross-user sharing is modeled by identity_record_share and unioned in
    apply_scope." Unioned *outside* the role loop, and unconditionally — a share is a grant in
    its own right, so it must survive a user whose roles grant nothing on the resource."""

    @pytest.fixture
    def share(self, with_scope):
        def _make(user, entity_type, entity_id, **kwargs):
            return RecordShare.objects.create(
                entity_type=entity_type,
                entity_id=entity_id,
                shared_with_user=user,
                access_level=kwargs.pop("access_level", RecordShare.AccessLevel.VIEW),
                shared_by=with_scope(DataScope.ALL),
                **kwargs,
            )

        return _make

    def test_a_share_widens_an_agents_view(self, with_scope, make_deal, share):
        from apps.crm.models import Deal

        me = with_scope(DataScope.OWN)
        mine = make_deal(owner=me)
        theirs = make_deal()
        share(me, ScopedEntityType.DEAL, theirs.id)
        assert visible(me, "deal", Deal) == {mine.id, theirs.id}

    def test_an_expired_share_grants_nothing(self, with_scope, make_deal, share):
        from datetime import timedelta

        from apps.crm.models import Deal

        me = with_scope(DataScope.OWN)
        theirs = make_deal()
        share(
            me,
            ScopedEntityType.DEAL,
            theirs.id,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        assert visible(me, "deal", Deal) == set()

    def test_a_share_with_no_expiry_is_permanent(self, with_scope, make_deal, share):
        from apps.crm.models import Deal

        me = with_scope(DataScope.OWN)
        theirs = make_deal()
        share(me, ScopedEntityType.DEAL, theirs.id, expires_at=None)
        assert visible(me, "deal", Deal) == {theirs.id}

    def test_a_view_share_and_an_edit_share_both_grant_visibility(
        self, with_scope, make_deal, share
    ):
        """access_level separates VIEW from EDIT on the *write* path. Both make the row
        visible; a scoping layer that hid EDIT-shared rows would be incoherent."""
        from apps.crm.models import Deal

        me = with_scope(DataScope.OWN)
        editable = make_deal()
        share(me, ScopedEntityType.DEAL, editable.id, access_level=RecordShare.AccessLevel.EDIT)
        assert visible(me, "deal", Deal) == {editable.id}

    def test_a_share_of_one_entity_type_does_not_leak_into_another(
        self, with_scope, make_deal, make_contact, share
    ):
        """entity_id is a bare UUID with no FK, so nothing but the entity_type filter stops a
        DEAL share from being read as a CONTACT share."""
        from apps.contacts.models import Contact

        me = with_scope(DataScope.OWN)
        deal = make_deal()
        contact = make_contact()
        share(me, ScopedEntityType.DEAL, deal.id)
        # A contact whose id we never shared stays hidden...
        assert contact.id not in visible(me, "contact", Contact)
        # ...and the deal's id, reused against the contact resource, matches nothing.
        share(me, ScopedEntityType.CONTACT, deal.id)
        assert visible(me, "contact", Contact) == set()

    def test_a_share_does_not_reach_a_resource_that_cannot_be_shared(self):
        """`entity_type=None` resources — the people directory, media, viewings — skip the
        union entirely. There is no ScopedEntityType for them, so there is nothing to forge."""
        from apps.identity.scoping import REGISTRY

        assert REGISTRY["user"].entity_type is None
        assert REGISTRY["media"].entity_type is None


# --- the registry itself ------------------------------------------------------------------


class TestRegistry:
    def test_every_registered_resource_covers_every_data_scope_deliberately(self):
        """Not a completeness requirement — omission *is* denial — but an audit hook.

        Printing which scopes each resource declines makes the deny-by-default decisions
        visible in one place, so a reviewer can tell a considered NOTHING from a forgotten
        one. The assertion is only that the declared keys are real scopes.
        """
        from apps.identity.scoping import REGISTRY

        valid = set(Role.DataScope.values)
        for name, spec in REGISTRY.items():
            assert set(spec.scopes) <= valid, name

    def test_registering_a_name_twice_is_refused(self):
        """Two apps silently claiming one resource name is how the wrong anchor ends up
        guarding an endpoint."""
        from apps.identity.models import User as UserModel
        from apps.identity.scoping import EVERYTHING, register

        with pytest.raises(ValueError, match="already registered"):
            register("contact", model=UserModel, scopes={Role.DataScope.ALL: EVERYTHING})

    def test_an_unknown_data_scope_is_refused(self):
        """A typo in a scope key would otherwise be silently unreachable — and a scope that is
        never matched is a scope that sees nothing, which looks like working code."""
        from apps.identity.models import User as UserModel
        from apps.identity.scoping import EVERYTHING, register

        with pytest.raises(ValueError, match="unknown data_scope"):
            register("bogus", model=UserModel, scopes={"BRANCH_ALL": EVERYTHING})

    def test_the_resources_this_pass_promised_are_registered(self):
        """A resource that is not registered raises on first use, which is the safe failure —
        but it fails at runtime, in whichever endpoint got there first."""
        from apps.identity.scoping import REGISTRY

        assert {
            "user",
            "contact",
            "lead",
            "deal",
            "viewing",
            "field_session",
            "property",
            "listing",
            "media",
            "lease",
        } <= set(REGISTRY)


class TestNoRowMultiplication:
    """apply_scope deliberately does not call `.distinct()`: every anchor that traverses a
    to-many path goes through `any_of` or `nested`, which subquery instead of joining. That
    keeps the outer query single-table, so pagination's COUNT(*) does not pay for a DISTINCT.

    The invariant has to be held by a test, because the cost of being wrong is duplicate rows
    in a paginated list — which reads as missing data, not as a scoping bug.
    """

    def test_a_property_manager_matching_several_ways_sees_one_row(
        self, with_scope, make_property
    ):
        from apps.inventory.models import Listing, Property

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        prop = make_property(managed_by=pm)
        for n in range(3):  # three listings on the one property
            Listing.objects.create(
                property=prop,
                reference_code=f"LST-DUP-{n}",
                listing_type=Listing.ListingType.SALE,
                title=f"Listing {n}",
                status=Listing.Status.ACTIVE,
            )
        rows = list(
            apply_scope(Property.objects.all(), pm, "property").values_list("id", flat=True)
        )
        assert rows == [prop.id]

    def test_an_agent_with_several_listings_on_one_property_sees_one_row(
        self, with_scope, make_property
    ):
        from apps.inventory.models import Listing, Property

        agent = with_scope(DataScope.OWN)
        prop = make_property()
        for n in range(3):
            Listing.objects.create(
                property=prop,
                reference_code=f"LST-AG-{n}",
                listing_type=Listing.ListingType.SALE,
                title=f"Listing {n}",
                status=Listing.Status.ACTIVE,
                assigned_agent=agent,
            )
        rows = list(
            apply_scope(Property.objects.all(), agent, "property").values_list("id", flat=True)
        )
        assert rows == [prop.id]

    def test_a_contact_owning_several_managed_properties_appears_once(
        self, with_scope, make_contact, make_property
    ):
        from apps.contacts.models import Contact
        from apps.inventory.models import PropertyOwner

        pm = with_scope(DataScope.MANAGED_PROPERTIES)
        contact = make_contact()
        for _ in range(3):
            PropertyOwner.objects.create(
                property=make_property(managed_by=pm),
                contact=contact,
                ownership_percentage=100,
                is_primary_owner=True,
                start_date="2026-01-01",
            )
        rows = list(
            apply_scope(Contact.objects.all(), pm, "contact").values_list("id", flat=True)
        )
        assert rows.count(contact.id) == 1


class TestScopeUnionDoctrine:
    """Two hard-won properties of the combiner, re-proved against domain resources rather than
    only the people directory."""

    def test_a_broad_role_is_not_narrowed_by_a_narrow_one(self, with_scope, make_deal):
        """`Q()` is Django's identity element, so `Q() | Q(owner=me)` collapses to
        `Q(owner=me)` — a role granting everything would *narrow* access when OR-ed with a
        narrower one. Hence the UNFILTERED sentinel, which short-circuits instead."""
        from apps.crm.models import Deal
        from apps.identity.models import UserRole

        user = with_scope(DataScope.OWN)
        UserRole.objects.create(
            user=user,
            role=Role.objects.create(code="t_dual_all", name="Dual", data_scope=DataScope.ALL),
        )
        make_deal()
        make_deal(owner=user)
        assert visible(user, "deal", Deal) == {d.id for d in Deal.objects.all()}

    def test_two_narrow_roles_union(self, with_scope, make_deal, other_branch):
        from apps.crm.models import Deal
        from apps.identity.models import Team, UserRole

        far_team = Team.objects.create(branch=other_branch, name="Far", code="FARU")
        user = with_scope(DataScope.OWN)
        UserRole.objects.create(
            user=user,
            role=Role.objects.create(
                code="t_dual_branch", name="Dual B", data_scope=DataScope.BRANCH
            ),
        )
        own = make_deal(owner=user)
        same_branch = make_deal(owner=with_scope(DataScope.OWN))
        far = make_deal(owner=with_scope(DataScope.OWN, branch=other_branch, team=far_team))
        seen = visible(user, "deal", Deal)
        assert {own.id, same_branch.id} <= seen
        assert far.id not in seen

    def test_a_user_with_no_roles_sees_nothing(self, make_user, make_deal):
        from apps.crm.models import Deal

        make_deal()
        assert visible(make_user(), "deal", Deal) == set()

    def test_an_anonymous_user_sees_nothing(self, make_deal):
        from django.contrib.auth.models import AnonymousUser

        from apps.crm.models import Deal

        make_deal()
        assert visible(AnonymousUser(), "deal", Deal) == set()


class TestScopedQuerysetMixin:
    """The endpoint-side half of §2. A wrong anchor shows up in the tests above; an endpoint
    that never scopes at all looks completely normal and returns the whole table."""

    def test_a_view_with_no_scope_resource_raises(self, agent):
        from django.core.exceptions import ImproperlyConfigured

        from apps.identity.models import User as UserModel
        from apps.identity.scoping import ScopedQuerysetMixin

        class Base:
            def get_queryset(self):
                return UserModel.objects.all()

        class Forgetful(ScopedQuerysetMixin, Base):
            pass

        view = Forgetful()
        view.request = type("R", (), {"user": agent})()
        with pytest.raises(ImproperlyConfigured, match="scope_resource"):
            view.get_queryset()

    def test_overriding_get_queryset_is_refused_at_import_time(self):
        """Adding `select_related` by overriding get_queryset is the obvious move, and it
        silently removes scoping — the subclass method wins the MRO and the mixin never runs.
        Caught when the class is defined, not when the endpoint is called."""
        from apps.identity.scoping import ScopedQuerysetMixin

        with pytest.raises(TypeError, match="bypasses row scoping"):

            class Sneaky(ScopedQuerysetMixin):
                scope_resource = "contact"

                def get_queryset(self):
                    from apps.contacts.models import Contact

                    return Contact.objects.all()

    def test_the_mixin_scopes_through_the_unscoped_hook(self, with_scope, make_contact):
        from apps.contacts.models import Contact
        from apps.identity.scoping import ScopedQuerysetMixin

        me = with_scope(DataScope.OWN)
        mine = make_contact(assigned_agent=me)
        make_contact()

        class Base:
            def get_queryset(self):
                return Contact.objects.all()

        class ContactView(ScopedQuerysetMixin, Base):
            scope_resource = "contact"

            def get_unscoped_queryset(self):
                return super().get_unscoped_queryset().select_related("assigned_agent")

        view = ContactView()
        view.request = type("R", (), {"user": me})()
        assert set(view.get_queryset().values_list("id", flat=True)) == {mine.id}
