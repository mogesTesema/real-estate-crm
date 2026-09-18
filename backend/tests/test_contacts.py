"""`contacts` — the centralised contact database (SRS §3.2).

Written against the service layer where the rule belongs to the service, and against HTTP
where the rule belongs to the endpoint. De-duplication and merge get the most attention:
they are the two operations that are destructive or leaky when wrong, and both were wrong in
the pre-rebuild implementation.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.contacts import selectors, services
from apps.contacts.models import Consent, Contact, ContactRelationship, ContactRole


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def owner_user(make_user):
    return make_user("owner")


def csv_file(text, name="contacts.csv"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, text.encode(), content_type="text/csv")


# --- normalisation ------------------------------------------------------------------------


class TestNormalisation:
    """Stored canonical, not normalised at read time: de-duplication is an indexed equality
    lookup, and a transform applied on the way out would never make two rows collide."""

    def test_the_whole_email_is_lower_cased(self):
        assert services.normalize_email("  Sam.Rivera@Example.COM ") == "sam.rivera@example.com"

    def test_a_local_number_becomes_e164(self, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        assert services.normalize_phone("050 123 4567") == "+971501234567"

    def test_the_same_number_written_four_ways_collapses_to_one_key(self, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        forms = ["0501234567", "050 123 4567", "+971 50 123 4567", "00971501234567"]
        assert len({services.normalize_phone(f) for f in forms}) == 1

    def test_an_unparseable_number_is_kept_not_rejected(self):
        """A decade of legacy contacts contains numbers no parser accepts. Losing the contact
        to keep the format tidy is the wrong trade."""
        assert services.normalize_phone("ext. 4417") == "4417"

    def test_a_national_id_ignores_separators(self):
        assert services.normalize_national_id("784-1985-1234567-8") == "784198512345678"


# --- create / update ----------------------------------------------------------------------


class TestCreate:
    def test_a_contact_is_stored_normalised(self, db, agent_user, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        contact = services.create_contact(
            actor=agent_user,
            first_name="Sam",
            last_name="Rivera",
            email="Sam@Example.COM",
            phone="050 123 4567",
        )
        contact.refresh_from_db()
        assert contact.email == "sam@example.com"
        assert contact.phone == "+971501234567"
        assert contact.created_by_id == agent_user.id

    def test_roles_are_a_set(self, db, agent_user):
        """SRS 3.2.2 — the same person is a buyer today and a past seller at the same time."""
        contact = services.create_contact(
            actor=agent_user, first_name="Sam", last_name="Rivera", roles=["BUYER", "SELLER"]
        )
        assert set(contact.roles.values_list("role", flat=True)) == {"BUYER", "SELLER"}

    def test_a_person_needs_a_name(self, db, agent_user):
        with pytest.raises(ValidationError):
            services.create_contact(actor=agent_user, email="nobody@example.test")

    def test_a_company_needs_a_company_name(self, db, agent_user):
        with pytest.raises(ValidationError):
            services.create_contact(
                actor=agent_user, contact_type=Contact.ContactType.COMPANY, first_name="Sam"
            )

    def test_fields_the_service_does_not_own_are_refused(self, db, agent_user):
        """`deleted_at` is this module's to manage. A caller that could set it could
        resurrect or bury a record without going through delete_contact."""
        with pytest.raises(ValidationError):
            services.create_contact(
                actor=agent_user, first_name="Sam", deleted_at="2026-01-01T00:00:00Z"
            )

    def test_setting_an_unknown_role_is_refused(self, db, agent_user):
        contact = services.create_contact(actor=agent_user, first_name="Sam")
        with pytest.raises(ValidationError):
            services.set_roles(contact, ["ARCHITECT"], actor=agent_user)

    def test_replacing_roles_removes_the_old_ones(self, db, agent_user):
        contact = services.create_contact(actor=agent_user, first_name="Sam", roles=["BUYER"])
        services.set_roles(contact, ["TENANT"], actor=agent_user)
        assert set(contact.roles.values_list("role", flat=True)) == {"TENANT"}


class TestDelete:
    def test_delete_is_soft(self, db, agent_user):
        contact = services.create_contact(actor=agent_user, first_name="Sam")
        services.delete_contact(contact, actor=agent_user)
        contact.refresh_from_db()
        assert contact.deleted_at is not None
        assert Contact.objects.filter(pk=contact.pk).exists()
        assert contact.pk not in selectors.live_contacts().values_list("pk", flat=True)

    def test_deleting_twice_is_a_no_op(self, db, agent_user):
        contact = services.create_contact(actor=agent_user, first_name="Sam")
        services.delete_contact(contact, actor=agent_user)
        first = contact.deleted_at
        services.delete_contact(contact, actor=agent_user)
        contact.refresh_from_db()
        assert contact.deleted_at == first


class TestConsent:
    def test_withdrawal_does_not_erase_the_grant(self, db, agent_user):
        """SRS 5.5 — the evidence that consent *was* given has to survive its withdrawal, or
        there is nothing to show a regulator asking why the contact was ever mailed."""
        contact = services.create_contact(actor=agent_user, first_name="Sam")
        services.record_consent(
            contact=contact,
            channel=Consent.Channel.EMAIL,
            status=Consent.Status.OPTED_IN,
            source="web form",
            actor=agent_user,
        )
        services.record_consent(
            contact=contact,
            channel=Consent.Channel.EMAIL,
            status=Consent.Status.OPTED_OUT,
            source="unsubscribe link",
            actor=agent_user,
        )
        rows = list(contact.consents.order_by("consented_at", "withdrawn_at"))
        assert len(rows) == 2
        assert rows[0].consented_at and rows[1].withdrawn_at


class TestRelationships:
    def test_a_contact_cannot_be_related_to_themselves(self, db, agent_user):
        contact = services.create_contact(actor=agent_user, first_name="Sam")
        with pytest.raises(ValidationError):
            services.add_relationship(
                from_contact=contact,
                to_contact=contact,
                relationship_type=ContactRelationship.RelationshipType.SPOUSE,
            )


# --- de-duplication (SRS 3.1.3, 3.1.10, 3.2.7) --------------------------------------------


class TestFindDuplicates:
    @pytest.fixture
    def existing(self, db, agent_user, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        return services.create_contact(
            actor=agent_user,
            first_name="Sam",
            last_name="Rivera",
            email="sam@example.test",
            phone="+971501234567",
            national_id="784198512345678",
            assigned_agent=agent_user,
        )

    def test_an_email_in_a_different_case_matches(self, existing, agent_user):
        matches = selectors.find_duplicates(agent_user, email="SAM@EXAMPLE.TEST")
        assert [m["matched_on"] for m in matches] == ["email"]
        assert matches[0]["id"] == str(existing.pk)

    def test_a_phone_written_locally_matches_the_e164_record(self, existing, agent_user):
        matches = selectors.find_duplicates(agent_user, phone="050 123 4567")
        assert matches and matches[0]["matched_on"] == "phone"

    def test_a_national_id_with_separators_matches(self, existing, agent_user):
        matches = selectors.find_duplicates(agent_user, national_id="784-1985-1234567-8")
        assert matches and matches[0]["matched_on"] == "national_id"

    def test_a_misspelt_name_is_offered_as_similar(self, existing, agent_user):
        """The trigram indexes added in v3.3 are what make this cheap enough to run at
        capture time (SRS 5.1)."""
        matches = selectors.find_duplicates(
            agent_user, first_name="Samm", last_name="Rivera"
        )
        assert matches and matches[0]["matched_on"] == "name"
        assert 0 < matches[0]["score"] < 1

    def test_an_unrelated_name_is_not_offered(self, existing, agent_user):
        assert selectors.find_duplicates(agent_user, first_name="Quentin", last_name="Zhao") == []

    def test_an_exact_match_outranks_a_fuzzy_one(self, existing, agent_user):
        services.create_contact(
            actor=agent_user, first_name="Sam", last_name="Riverra", assigned_agent=agent_user
        )
        matches = selectors.find_duplicates(
            agent_user, email="sam@example.test", first_name="Sam", last_name="Rivera"
        )
        assert matches[0]["matched_on"] == "email"
        assert matches[0]["score"] == 1.0

    def test_the_contact_being_edited_can_be_excluded(self, existing, agent_user):
        """Otherwise every edit reports the record as its own duplicate."""
        assert (
            selectors.find_duplicates(
                agent_user, email="sam@example.test", exclude_id=existing.pk
            )
            == []
        )

    def test_a_soft_deleted_contact_is_not_a_duplicate(self, existing, agent_user):
        services.delete_contact(existing, actor=agent_user)
        assert selectors.find_duplicates(agent_user, email="sam@example.test") == []


class TestDuplicateDisclosure:
    """The tension at the heart of this endpoint.

    De-duplication must search the whole book — a scoped lookup hides the record another
    agent owns and creates the duplicate SRS 3.1.10/3.1.11 exist to prevent. But returning
    that record in full would make `GET /contacts/duplicates/` a way to read any contact in
    the company by guessing a phone number.
    """

    @pytest.fixture
    def other_agents_contact(self, db, make_user, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        other = make_user("agent")
        return other, services.create_contact(
            actor=other,
            first_name="Nadia",
            last_name="Haddad",
            email="nadia@example.test",
            phone="+971509998888",
            assigned_agent=other,
        )

    def test_a_match_outside_the_callers_scope_is_still_reported(
        self, other_agents_contact, agent_user
    ):
        _, contact = other_agents_contact
        matches = selectors.find_duplicates(agent_user, email="nadia@example.test")
        assert len(matches) == 1
        assert matches[0]["in_scope"] is False

    def test_but_its_detail_is_withheld(self, other_agents_contact, agent_user):
        _, contact = other_agents_contact
        match = selectors.find_duplicates(agent_user, email="nadia@example.test")[0]
        assert match["id"] is None
        assert "email" not in match
        assert "phone" not in match
        assert "Haddad" not in match["display_name"]

    def test_and_the_caller_is_told_who_to_ask(self, other_agents_contact, agent_user):
        other, _ = other_agents_contact
        match = selectors.find_duplicates(agent_user, email="nadia@example.test")[0]
        assert match["owned_by"] == other.full_name
        assert "second record" in match["hint"]

    def test_a_match_the_caller_owns_comes_back_in_full(self, db, agent_user):
        contact = services.create_contact(
            actor=agent_user,
            first_name="Own",
            last_name="Record",
            email="own@example.test",
            assigned_agent=agent_user,
        )
        match = selectors.find_duplicates(agent_user, email="own@example.test")[0]
        assert match["in_scope"] is True
        assert match["id"] == str(contact.pk)
        assert match["email"] == "own@example.test"


# --- merge (SRS 3.2.7) --------------------------------------------------------------------


class TestMerge:
    """The old implementation repointed `leads` and nothing else, silently orphaning every
    other child — and would have gone on orphaning each relation added by each new module.
    This walks `Contact._meta.related_objects` instead, so these tests are partly about the
    rows that move and partly about the reflection that finds them."""

    @pytest.fixture
    def pair(self, db, agent_user):
        survivor = services.create_contact(
            actor=agent_user,
            first_name="Sam",
            last_name="Rivera",
            email="sam@example.test",
            assigned_agent=agent_user,
        )
        duplicate = services.create_contact(
            actor=agent_user,
            first_name="Sam",
            last_name="Rivera",
            phone="+971501234567",
            national_id="784198512345678",
            city="Dubai",
            assigned_agent=agent_user,
        )
        return survivor, duplicate

    def test_the_duplicate_is_soft_deleted_and_points_at_the_survivor(self, pair, agent_user):
        survivor, duplicate = pair
        services.merge_contacts(survivor=survivor, duplicate=duplicate, actor=agent_user)
        duplicate.refresh_from_db()
        assert duplicate.deleted_at is not None
        assert duplicate.custom_data["merged_into"] == str(survivor.pk)

    def test_the_survivor_inherits_only_what_it_was_missing(self, pair, agent_user):
        """The survivor is the record the operator chose to keep, so its own values win."""
        survivor, duplicate = pair
        merged = services.merge_contacts(
            survivor=survivor, duplicate=duplicate, actor=agent_user
        )
        assert merged.email == "sam@example.test"  # survivor's own, kept
        assert merged.phone == "+971501234567"  # the gap, filled
        assert merged.city == "Dubai"

    def test_leads_move_to_the_survivor(self, pair, agent_user):
        from apps.crm.models import Lead

        survivor, duplicate = pair
        lead = Lead.objects.create(
            contact=duplicate, lead_type=Lead.LeadType.BUY, title="Villa enquiry"
        )
        services.merge_contacts(survivor=survivor, duplicate=duplicate, actor=agent_user)
        lead.refresh_from_db()
        assert lead.contact_id == survivor.pk

    def test_every_relation_moves_not_just_leads(self, pair, agent_user, make_property):
        """The regression this rewrite exists for. Five different apps hang rows off a
        contact; naming them one by one is how the sixth gets missed."""
        from apps.crm.models import Lead
        from apps.inventory.models import PropertyOwner

        survivor, duplicate = pair
        Lead.objects.create(contact=duplicate, lead_type=Lead.LeadType.BUY, title="L")
        services.record_consent(
            contact=duplicate,
            channel=Consent.Channel.SMS,
            status=Consent.Status.OPTED_IN,
            source="form",
        )
        PropertyOwner.objects.create(
            property=make_property(),
            contact=duplicate,
            ownership_percentage=100,
            is_primary_owner=True,
            start_date="2026-01-01",
        )
        services.merge_contacts(survivor=survivor, duplicate=duplicate, actor=agent_user)

        assert Lead.objects.filter(contact=survivor).count() == 1
        assert survivor.consents.count() == 1
        assert survivor.owned_properties.count() == 1
        assert not Lead.objects.filter(contact=duplicate).exists()
        assert not duplicate.consents.exists()
        assert not duplicate.owned_properties.exists()

    def test_a_colliding_unique_child_is_dropped_not_duplicated(self, pair, agent_user):
        """`contacts_contact_role` is UNIQUE(contact, role). Both records marked BUYER means
        repointing collides; the row is redundant by definition, so it goes."""
        survivor, duplicate = pair
        services.set_roles(survivor, ["BUYER"], actor=agent_user)
        services.set_roles(duplicate, ["BUYER", "TENANT"], actor=agent_user)
        merged = services.merge_contacts(
            survivor=survivor, duplicate=duplicate, actor=agent_user
        )
        assert set(merged.roles.values_list("role", flat=True)) == {"BUYER", "TENANT"}
        assert ContactRole.objects.filter(contact=duplicate).count() == 0

    def test_relationships_from_both_ends_survive_without_self_loops(self, pair, agent_user):
        survivor, duplicate = pair
        third = services.create_contact(actor=agent_user, first_name="Third", last_name="Party")
        services.add_relationship(
            from_contact=duplicate,
            to_contact=third,
            relationship_type=ContactRelationship.RelationshipType.SPOUSE,
        )
        services.add_relationship(
            from_contact=third,
            to_contact=duplicate,
            relationship_type=ContactRelationship.RelationshipType.SPOUSE,
        )
        # And an edge that becomes a self-loop once the two are one record.
        services.add_relationship(
            from_contact=survivor,
            to_contact=duplicate,
            relationship_type=ContactRelationship.RelationshipType.FAMILY_MEMBER,
        )
        services.merge_contacts(survivor=survivor, duplicate=duplicate, actor=agent_user)

        assert not ContactRelationship.objects.filter(
            from_contact=survivor, to_contact=survivor
        ).exists()
        assert ContactRelationship.objects.filter(
            from_contact=survivor, to_contact=third
        ).exists()
        assert ContactRelationship.objects.filter(
            from_contact=third, to_contact=survivor
        ).exists()

    def test_record_shares_follow_the_survivor(self, pair, agent_user, make_user):
        """Shares point at a bare UUID with no foreign key, so nothing moves them
        automatically. Left behind, the grant silently evaporates for whoever held it."""
        from apps.core.choices import ScopedEntityType
        from apps.identity.models import RecordShare

        survivor, duplicate = pair
        colleague = make_user("agent")
        RecordShare.objects.create(
            entity_type=ScopedEntityType.CONTACT,
            entity_id=duplicate.pk,
            shared_with_user=colleague,
            access_level=RecordShare.AccessLevel.VIEW,
            shared_by=agent_user,
        )
        services.merge_contacts(survivor=survivor, duplicate=duplicate, actor=agent_user)
        assert RecordShare.objects.filter(
            shared_with_user=colleague, entity_id=survivor.pk
        ).exists()
        assert not RecordShare.objects.filter(entity_id=duplicate.pk).exists()

    def test_merging_two_portal_clients_is_refused(self, agent_user, portal_tenant, make_contact, make_lease_for, make_user):
        """A one-to-one child cannot be held twice, and silently discarding one would revoke
        somebody's portal access without a word."""
        from apps.identity.models import PortalProfile

        first = portal_tenant["contact"]
        second = make_contact()
        PortalProfile.objects.create(
            user=make_user(), contact=second, portal_type=PortalProfile.PortalType.TENANT
        )
        with pytest.raises(ValidationError, match="Resolve it before merging"):
            services.merge_contacts(survivor=first, duplicate=second, actor=agent_user)

    def test_a_portal_profile_moves_when_only_one_side_has_one(
        self, agent_user, portal_tenant, make_contact
    ):
        survivor = make_contact()
        holder = portal_tenant["contact"]
        services.merge_contacts(survivor=survivor, duplicate=holder, actor=agent_user)
        survivor.refresh_from_db()
        assert survivor.portal_profile.user_id == portal_tenant["user"].id

    def test_a_contact_cannot_be_merged_into_itself(self, pair, agent_user):
        survivor, _ = pair
        with pytest.raises(ValidationError):
            services.merge_contacts(survivor=survivor, duplicate=survivor, actor=agent_user)

    def test_an_already_merged_contact_cannot_be_merged_again(self, pair, agent_user):
        survivor, duplicate = pair
        services.merge_contacts(survivor=survivor, duplicate=duplicate, actor=agent_user)
        with pytest.raises(ValidationError):
            services.merge_contacts(survivor=survivor, duplicate=duplicate, actor=agent_user)

    def test_the_merge_is_audited_with_what_moved(self, pair, agent_user):
        """A merge is irreversible in practice — the children have moved — so what it moved is
        the only way to reconstruct the record. SRS 5.3."""
        from apps.crm.models import Lead
        from apps.platform.models import AuditEvent

        survivor, duplicate = pair
        Lead.objects.create(contact=duplicate, lead_type=Lead.LeadType.BUY, title="L")
        services.merge_contacts(survivor=survivor, duplicate=duplicate, actor=agent_user)

        event = AuditEvent.objects.filter(entity_type="CONTACT", entity_id=survivor.pk).first()
        assert event is not None
        assert event.old_values["merged_contact_id"] == str(duplicate.pk)
        assert any("Lead" in key for key in event.new_values["moved"])


# --- CSV import / export (SRS 3.2.6, 3.13.4) ----------------------------------------------


SAMPLE_CSV = (
    "First Name,Last Name,Email,Mobile,City\n"
    "Sam,Rivera,sam@example.test,050 123 4567,Dubai\n"
    "Nadia,Haddad,nadia@example.test,050 999 8888,Abu Dhabi\n"
)


class TestImport:
    def test_columns_are_matched_by_their_usual_names(self, db):
        headers, _ = services.read_csv(csv_file(SAMPLE_CSV))
        mapping = services.suggest_mapping(headers)
        assert mapping["first_name"] == "First Name"
        assert mapping["phone"] == "Mobile"
        assert mapping["email"] == "Email"

    def test_a_dry_run_writes_nothing(self, db, agent_user):
        """SRS 3.2.6's dry-run report. Getting this the wrong way round means a mis-mapped
        column is discovered after five thousand rows have landed."""
        headers, rows = services.read_csv(csv_file(SAMPLE_CSV))
        report = services.import_contacts(
            rows, mapping=services.suggest_mapping(headers), actor=agent_user, commit=False
        )
        assert report["summary"] == {
            "total": 2,
            "created": 2,
            "duplicates": 0,
            "invalid": 0,
            "committed": False,
        }
        assert Contact.objects.count() == 0

    def test_a_commit_writes_normalised_rows(self, db, agent_user, settings):
        settings.DEFAULT_PHONE_REGION = "AE"
        headers, rows = services.read_csv(csv_file(SAMPLE_CSV))
        services.import_contacts(
            rows, mapping=services.suggest_mapping(headers), actor=agent_user, commit=True
        )
        assert Contact.objects.count() == 2
        assert Contact.objects.filter(phone="+971501234567").exists()

    def test_a_row_matching_an_existing_contact_is_skipped(self, db, agent_user):
        services.create_contact(
            actor=agent_user, first_name="Sam", last_name="Rivera", email="sam@example.test"
        )
        headers, rows = services.read_csv(csv_file(SAMPLE_CSV))
        report = services.import_contacts(
            rows, mapping=services.suggest_mapping(headers), actor=agent_user, commit=True
        )
        assert report["summary"]["created"] == 1
        assert report["summary"]["duplicates"] == 1
        assert report["duplicates"][0]["matched_on"] == "database"

    def test_the_same_person_twice_in_one_file_is_caught(self, db, agent_user):
        """Nothing has been written yet in a dry run, so the database cannot catch this —
        the importer has to remember what it has already seen in the file."""
        doubled = SAMPLE_CSV + "Sam,Rivera,SAM@EXAMPLE.TEST,,\n"
        headers, rows = services.read_csv(csv_file(doubled))
        report = services.import_contacts(
            rows, mapping=services.suggest_mapping(headers), actor=agent_user, commit=False
        )
        assert report["summary"]["created"] == 2
        assert report["summary"]["duplicates"] == 1
        assert "row 2 of this file" in report["duplicates"][0]["matched"]

    def test_a_nameless_row_is_reported_invalid_not_fatal(self, db, agent_user):
        """One bad row must not abort the other four thousand nine hundred."""
        broken = SAMPLE_CSV + ",,noname@example.test,,\n"
        headers, rows = services.read_csv(csv_file(broken))
        report = services.import_contacts(
            rows, mapping=services.suggest_mapping(headers), actor=agent_user, commit=True
        )
        assert report["summary"] == {
            "total": 3,
            "created": 2,
            "duplicates": 0,
            "invalid": 1,
            "committed": True,
        }
        assert Contact.objects.count() == 2

    def test_an_excel_byte_order_mark_does_not_break_the_first_column(self, db):
        """A file saved from Excel starts with a BOM; read as plain UTF-8 it puts an invisible
        \\ufeff on the first header, so that column's mapping silently never matches and every
        row imports without a first name."""
        headers, _ = services.read_csv(csv_file("﻿" + SAMPLE_CSV))
        assert headers[0] == "First Name"

    def test_a_file_over_the_row_limit_is_refused(self, db, agent_user, settings):
        settings.CONTACT_IMPORT_MAX_ROWS = 2
        rows = [{"Email": f"p{n}@example.test"} for n in range(3)]
        with pytest.raises(ValidationError, match="row import limit"):
            services.import_contacts(
                rows, mapping={"email": "Email"}, actor=agent_user, commit=False
            )

    def test_an_unknown_target_field_is_refused(self, db, agent_user):
        with pytest.raises(ValidationError, match="Unknown target fields"):
            services.import_contacts(
                [], mapping={"salary": "Salary"}, actor=agent_user, commit=False
            )

    def test_a_committed_import_is_audited(self, db, agent_user):
        from apps.platform.models import AuditEvent

        headers, rows = services.read_csv(csv_file(SAMPLE_CSV))
        services.import_contacts(
            rows,
            mapping=services.suggest_mapping(headers),
            actor=agent_user,
            commit=True,
            filename="book.csv",
        )
        event = AuditEvent.objects.filter(
            entity_type="CONTACT", action=AuditEvent.Action.CREATE
        ).first()
        assert event.new_values["bulk_import"] is True
        assert event.new_values["created"] == 2
        assert event.new_values["filename"] == "book.csv"

    def test_a_dry_run_is_not_audited(self, db, agent_user):
        from apps.platform.models import AuditEvent

        headers, rows = services.read_csv(csv_file(SAMPLE_CSV))
        services.import_contacts(
            rows, mapping=services.suggest_mapping(headers), actor=agent_user, commit=False
        )
        assert not AuditEvent.objects.filter(entity_type="CONTACT").exists()


class TestExport:
    def test_the_export_records_the_row_count_it_actually_sent(self, db, agent_user):
        """SRS 5.3 names data export a sensitive action. Counted after the fact: an export the
        client abandoned halfway did not leak the whole table."""
        from apps.platform.models import AuditEvent

        for n in range(3):
            services.create_contact(actor=agent_user, first_name=f"P{n}", last_name="X")
        rows = list(services.export_rows(selectors.live_contacts(), actor=agent_user))
        assert len(rows) == 4  # header + 3
        event = AuditEvent.objects.get(
            entity_type="CONTACT", action=AuditEvent.Action.EXPORT
        )
        assert event.new_values["row_count"] == 3
        assert event.actor_user_id == agent_user.id


# --- the endpoints ------------------------------------------------------------------------


@pytest.mark.django_db
class TestContactApi:
    URL = "/api/v1/contacts/"

    def test_an_agent_creates_a_contact_with_roles(self, auth_client, agent_user):
        response = auth_client(agent_user).post(
            self.URL,
            {
                "first_name": "Sam",
                "last_name": "Rivera",
                "email": "Sam@Example.TEST",
                "phone": "050 123 4567",
                "roles": ["BUYER"],
                "assigned_agent": str(agent_user.id),
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        assert response.data["email"] == "sam@example.test"
        assert response.data["roles"] == ["BUYER"]

    def test_the_list_is_scoped(self, auth_client, agent_user, make_user):
        """Every list endpoint runs through apply_scope — §2, and the reason Step 1 came
        before this one."""
        other = make_user("agent")
        mine = services.create_contact(
            actor=agent_user, first_name="Mine", assigned_agent=agent_user
        )
        services.create_contact(actor=other, first_name="Theirs", assigned_agent=other)

        response = auth_client(agent_user).get(self.URL)
        assert response.status_code == 200
        assert [row["id"] for row in response.data["results"]] == [str(mine.id)]

    def test_a_contact_outside_scope_is_a_404_not_a_403(self, auth_client, agent_user, make_user):
        """403 would confirm the record exists. The scoped queryset makes it simply absent."""
        other = make_user("agent")
        theirs = services.create_contact(
            actor=other, first_name="Theirs", assigned_agent=other
        )
        response = auth_client(agent_user).get(f"{self.URL}{theirs.id}/")
        assert response.status_code == 404

    def test_an_owner_sees_the_whole_book(self, auth_client, owner_user, agent_user):
        services.create_contact(actor=agent_user, first_name="A", assigned_agent=agent_user)
        services.create_contact(actor=agent_user, first_name="B")
        response = auth_client(owner_user).get(self.URL)
        assert response.data["count"] == 2

    def test_delete_soft_deletes(self, auth_client, agent_user):
        contact = services.create_contact(
            actor=agent_user, first_name="Sam", assigned_agent=agent_user
        )
        response = auth_client(agent_user).delete(f"{self.URL}{contact.id}/")
        assert response.status_code == 204
        contact.refresh_from_db()
        assert contact.deleted_at is not None

    def test_roles_are_replaced_through_their_own_endpoint(self, auth_client, agent_user):
        contact = services.create_contact(
            actor=agent_user, first_name="Sam", roles=["BUYER"], assigned_agent=agent_user
        )
        response = auth_client(agent_user).put(
            f"{self.URL}{contact.id}/roles/", {"roles": ["TENANT", "INVESTOR"]}, format="json"
        )
        assert response.status_code == 200
        assert response.data["roles"] == ["INVESTOR", "TENANT"]

    def test_ordering_is_restricted_to_declared_fields(self, auth_client, agent_user):
        """With OrderingFilter on globally and no `ordering_fields`, DRF accepts any model
        field — including ones the serializer never exposes, which turns the list endpoint
        into an enumeration oracle for them. The old code had exactly that."""
        services.create_contact(
            actor=agent_user, first_name="Sam", national_id="7841985", assigned_agent=agent_user
        )
        ordered = auth_client(agent_user).get(f"{self.URL}?ordering=national_id")
        plain = auth_client(agent_user).get(f"{self.URL}?ordering=-created_at")
        assert ordered.status_code == 200
        # The unlisted field is ignored, so the result is the default ordering, not a sort.
        assert [r["id"] for r in ordered.data["results"]] == [
            r["id"] for r in plain.data["results"]
        ]

    def test_search_matches_in_the_middle_of_a_name(self, auth_client, agent_user):
        """SRS 5.1. Prefix-only search that misses "Al Maktoum" when the user types "Maktoum"
        reads as missing data — which is why v3.3 added the trigram indexes."""
        services.create_contact(
            actor=agent_user, first_name="Rashid", last_name="Al Maktoum",
            assigned_agent=agent_user,
        )
        response = auth_client(agent_user).get(f"{self.URL}?search=Maktoum")
        assert response.data["count"] == 1


@pytest.mark.django_db
class TestDuplicatesEndpoint:
    URL = "/api/v1/contacts/duplicates/"

    def test_matching_nothing_needs_at_least_one_identifier(self, auth_client, agent_user):
        assert auth_client(agent_user).get(self.URL).status_code == 400

    def test_a_match_the_caller_owns_comes_back_in_full(self, auth_client, agent_user):
        contact = services.create_contact(
            actor=agent_user, first_name="Sam", email="sam@example.test",
            assigned_agent=agent_user,
        )
        response = auth_client(agent_user).get(f"{self.URL}?email=SAM@EXAMPLE.TEST")
        assert response.status_code == 200
        assert response.data[0]["id"] == str(contact.id)

    def test_the_endpoint_is_not_a_read_oracle(self, auth_client, agent_user, make_user):
        """Guessing a phone number must not become a way to read any record in the company."""
        other = make_user("agent")
        services.create_contact(
            actor=other, first_name="Nadia", last_name="Haddad",
            email="nadia@example.test", assigned_agent=other,
        )
        match = auth_client(agent_user).get(f"{self.URL}?email=nadia@example.test").data[0]
        assert match["in_scope"] is False
        assert match["id"] is None
        assert "Haddad" not in match["display_name"]


@pytest.mark.django_db
class TestMergeEndpoint:
    def test_merging_folds_the_duplicate_in(self, auth_client, agent_user):
        survivor = services.create_contact(
            actor=agent_user, first_name="Sam", email="sam@example.test",
            assigned_agent=agent_user,
        )
        duplicate = services.create_contact(
            actor=agent_user, first_name="Sam", phone="+971501234567",
            assigned_agent=agent_user,
        )
        response = auth_client(agent_user).post(
            f"/api/v1/contacts/{survivor.id}/merge/",
            {"duplicate_id": str(duplicate.id)},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["phone"] == "+971501234567"

    def test_merging_a_record_the_caller_cannot_see_is_refused(
        self, auth_client, agent_user, make_user
    ):
        """Merge is destructive. Doing it to a record you cannot see, on a guessed id, is not
        something to allow — and 400 rather than 404 keeps the two cases indistinguishable."""
        other = make_user("agent")
        survivor = services.create_contact(
            actor=agent_user, first_name="Mine", assigned_agent=agent_user
        )
        theirs = services.create_contact(actor=other, first_name="Theirs", assigned_agent=other)
        response = auth_client(agent_user).post(
            f"/api/v1/contacts/{survivor.id}/merge/",
            {"duplicate_id": str(theirs.id)},
            format="json",
        )
        assert response.status_code == 400
        theirs.refresh_from_db()
        assert theirs.deleted_at is None


@pytest.mark.django_db
class TestImportExportEndpoints:
    def test_posting_a_file_reports_without_writing(self, auth_client, agent_user):
        response = auth_client(agent_user).post(
            "/api/v1/contacts/import/",
            {"file": csv_file(SAMPLE_CSV)},
            format="multipart",
        )
        assert response.status_code == 200, response.data
        assert response.data["summary"]["committed"] is False
        assert response.data["mapping"]["first_name"] == "First Name"
        assert Contact.objects.count() == 0

    def test_the_same_post_with_commit_writes(self, auth_client, agent_user):
        response = auth_client(agent_user).post(
            "/api/v1/contacts/import/",
            {"file": csv_file(SAMPLE_CSV), "commit": "true"},
            format="multipart",
        )
        assert response.status_code == 200, response.data
        assert response.data["summary"]["created"] == 2
        assert Contact.objects.count() == 2

    def test_the_export_returns_only_the_callers_rows(self, auth_client, agent_user, make_user):
        """An export that ignored scoping would be the widest data leak in the product."""
        other = make_user("agent")
        services.create_contact(
            actor=agent_user, first_name="Mine", last_name="X", assigned_agent=agent_user
        )
        services.create_contact(
            actor=other, first_name="Theirs", last_name="Y", assigned_agent=other
        )
        response = auth_client(agent_user).get("/api/v1/contacts/export/")
        assert response.status_code == 200
        body = b"".join(response.streaming_content).decode()
        assert "Mine" in body
        assert "Theirs" not in body

    def test_the_export_honours_the_same_filters_as_the_list(self, auth_client, agent_user):
        services.create_contact(
            actor=agent_user, first_name="Rashid", last_name="Al Maktoum",
            assigned_agent=agent_user,
        )
        services.create_contact(
            actor=agent_user, first_name="Sam", last_name="Rivera", assigned_agent=agent_user
        )
        response = auth_client(agent_user).get("/api/v1/contacts/export/?search=Maktoum")
        body = b"".join(response.streaming_content).decode()
        assert "Maktoum" in body
        assert "Rivera" not in body
