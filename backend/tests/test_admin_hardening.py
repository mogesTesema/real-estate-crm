"""SRS 3.17: the runtime permission matrix, field-level permissions, custom fields.

The doctrine: everything here is BEHAVIOR-PRESERVING until an admin acts — the matrix seed
mirrors day-one reach, absent field-permission rows mean READ_WRITE, and an empty
custom-field registry validates everything. These tests prove both halves: the quiet
default AND the bite after an admin writes rows.
"""
import pytest
from django.core.exceptions import ValidationError

from apps.core.models import CustomField
from apps.identity.models import FieldPermission, Permission, RolePermission


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


class TestPermissionMatrix:
    def test_the_catalogue_and_grants_are_admin_only(
        self, db, auth_client, agent_user, super_admin
    ):
        assert auth_client(agent_user).get("/api/v1/permissions/").status_code == 403
        assert auth_client(agent_user).get("/api/v1/role-permissions/").status_code == 403
        catalogue = auth_client(super_admin).get("/api/v1/permissions/?page_size=100")
        assert catalogue.status_code == 200
        codes = {row["code"] for row in catalogue.data["results"]}
        assert "offers.manage" in codes

    def test_revoking_a_code_bites_at_runtime(
        self, db, auth_client, agent_user, super_admin, make_deal, make_property
    ):
        """The whole point of the matrix: the admin tightens, the endpoint obeys —
        no deploy in between."""
        from apps.crm.models import DealProperty

        deal = make_deal(owner=agent_user)
        DealProperty.objects.create(deal=deal, property=make_property(), is_primary=True)
        payload = {
            "deal": str(deal.pk), "direction": "BUYER_TO_SELLER", "amount": "500000",
        }
        assert auth_client(agent_user).post(
            "/api/v1/offers/", payload
        ).status_code == 201  # seeded: agents may manage offers

        grant = RolePermission.objects.get(
            role__code="agent", permission__code="offers.manage"
        )
        response = auth_client(super_admin).delete(
            f"/api/v1/role-permissions/{grant.pk}/"
        )
        assert response.status_code == 204
        assert auth_client(agent_user).post(
            "/api/v1/offers/", payload
        ).status_code == 403  # tightened at runtime
        # Reads were never gated by the code.
        assert auth_client(agent_user).get("/api/v1/offers/").status_code == 200

    def test_matrix_edits_are_audited(self, db, auth_client, super_admin, roles):
        from apps.platform.models import AuditEvent

        permission = Permission.objects.get(code="reports.schedule")
        response = auth_client(super_admin).post(
            "/api/v1/role-permissions/",
            {"role": str(roles["agent"].pk), "permission": str(permission.pk)},
        )
        assert response.status_code == 201
        assert AuditEvent.objects.filter(
            entity_type="ROLE", entity_id=roles["agent"].pk,
            new_values__granted="reports.schedule",
        ).exists()

    def test_contacts_export_rides_the_matrix(
        self, db, auth_client, agent_user, make_contact
    ):
        make_contact()
        assert auth_client(agent_user).get(
            "/api/v1/contacts/export/"
        ).status_code == 200  # seeded all-staff: today's behavior
        RolePermission.objects.filter(
            role__code="agent", permission__code="contacts.export"
        ).delete()
        assert auth_client(agent_user).get(
            "/api/v1/contacts/export/"
        ).status_code == 403


class TestFieldPermissions:
    def test_no_rows_means_todays_behavior(self, db, auth_client, agent_user, make_contact):
        contact = make_contact(email="visible@example.test")
        contact.assigned_agent = agent_user
        contact.save(update_fields=["assigned_agent"])
        row = auth_client(agent_user).get(f"/api/v1/contacts/{contact.pk}/").data
        assert row["email"] == "visible@example.test"

    def test_hidden_disappears_and_read_only_ignores_writes(
        self, db, auth_client, agent_user, owner, make_contact, roles
    ):
        contact = make_contact(email="secret@example.test", phone="+971500000001")
        contact.assigned_agent = agent_user
        contact.save(update_fields=["assigned_agent"])
        FieldPermission.objects.create(
            role=roles["agent"], entity_type="CONTACT",
            field_name="email", access_level="HIDDEN",
        )
        FieldPermission.objects.create(
            role=roles["agent"], entity_type="CONTACT",
            field_name="phone", access_level="READ_ONLY",
        )
        row = auth_client(agent_user).get(f"/api/v1/contacts/{contact.pk}/").data
        assert "email" not in row          # HIDDEN for the agent
        assert row["phone"] == "+971500000001"  # READ_ONLY still reads

        # The write silently drops the READ_ONLY field (DRF read_only convention).
        response = auth_client(agent_user).patch(
            f"/api/v1/contacts/{contact.pk}/",
            {"phone": "+971509999999", "first_name": "Edited"},
        )
        assert response.status_code == 200, response.data
        contact.refresh_from_db()
        assert contact.phone == "+971500000001"
        assert contact.first_name == "Edited"

        # The owner's roles carry no rows — they still see everything.
        assert (
            auth_client(owner).get(f"/api/v1/contacts/{contact.pk}/").data["email"]
            == "secret@example.test"
        )

    def test_most_permissive_role_wins(self, db, agent_user, roles):
        from apps.identity.field_access import field_rules
        from apps.identity.models import UserRole

        FieldPermission.objects.create(
            role=roles["agent"], entity_type="LEAD",
            field_name="budget_max", access_level="HIDDEN",
        )
        FieldPermission.objects.create(
            role=roles["manager"], entity_type="LEAD",
            field_name="budget_max", access_level="READ_WRITE",
        )
        assert field_rules(agent_user, "LEAD") == {"budget_max": "HIDDEN"}
        UserRole.objects.create(user=agent_user, role=roles["manager"])
        fresh = type(agent_user).objects.get(pk=agent_user.pk)  # cache lives on instance
        assert field_rules(fresh, "LEAD") == {}  # the wider role wins

    def test_the_admin_surface_is_gated(self, db, auth_client, agent_user, super_admin, roles):
        assert auth_client(agent_user).get("/api/v1/field-permissions/").status_code == 403
        response = auth_client(super_admin).post(
            "/api/v1/field-permissions/",
            {"role": str(roles["agent"].pk), "entity_type": "CONTACT",
             "field_name": "email", "access_level": "HIDDEN"},
        )
        assert response.status_code == 201


class TestCustomFields:
    @pytest.fixture
    def field(self, db):
        return CustomField.objects.create(
            entity_type="CONTACT", key="nationality", label="Nationality",
            data_type="SINGLE_SELECT", choices=["AE", "SA", "EG"], is_required=False,
        )

    def test_an_empty_registry_validates_everything(self, db, agent_user):
        from apps.contacts import services

        contact = services.create_contact(
            actor=agent_user, first_name="Free", last_name="Form",
            custom_data={"anything": "goes"},
        )
        assert contact.custom_data == {"anything": "goes"}

    def test_registered_fields_are_enforced(self, db, agent_user, field):
        from apps.contacts import services

        with pytest.raises(ValidationError, match="Pick one of"):
            services.create_contact(
                actor=agent_user, first_name="Bad", last_name="Choice",
                custom_data={"nationality": "XX"},
            )
        contact = services.create_contact(
            actor=agent_user, first_name="Good", last_name="Choice",
            custom_data={"nationality": "AE"},
        )
        assert contact.custom_data["nationality"] == "AE"

    def test_required_fields_bite_on_create_not_patch(self, db, agent_user):
        from apps.contacts import services

        CustomField.objects.create(
            entity_type="CONTACT", key="kyc_ref", label="KYC ref",
            data_type="TEXT", is_required=True,
        )
        with pytest.raises(ValidationError, match="required"):
            services.create_contact(
                actor=agent_user, first_name="No", last_name="Kyc", custom_data={},
            )
        contact = services.create_contact(
            actor=agent_user, first_name="Has", last_name="Kyc",
            custom_data={"kyc_ref": "KYC-1"},
        )
        # A PATCH that doesn't mention the required key is not removing it.
        services.update_contact(contact, actor=agent_user, custom_data={"kyc_ref": "KYC-2"})

    def test_strict_mode_rejects_unknown_keys(self, db, agent_user, field, settings):
        from apps.contacts import services

        settings.CUSTOM_FIELD_STRICT = True
        with pytest.raises(ValidationError, match="Unknown custom field"):
            services.create_contact(
                actor=agent_user, first_name="Strict", last_name="Mode",
                custom_data={"rogue": 1},
            )

    def test_types_are_checked(self, db, agent_user):
        from apps.core.services import validate_custom_data

        CustomField.objects.create(
            entity_type="LEAD", key="floors", label="Floors", data_type="NUMBER",
        )
        CustomField.objects.create(
            entity_type="LEAD", key="move_in", label="Move in", data_type="DATE",
        )
        with pytest.raises(ValidationError, match="number"):
            validate_custom_data("LEAD", {"floors": "three"})
        with pytest.raises(ValidationError, match="ISO date"):
            validate_custom_data("LEAD", {"move_in": "someday"})
        validate_custom_data("LEAD", {"floors": 3, "move_in": "2026-10-01"})

    def test_the_registry_api_is_admin_only_and_delete_deactivates(
        self, db, auth_client, agent_user, super_admin, field
    ):
        assert auth_client(agent_user).get("/api/v1/custom-fields/").status_code == 403
        response = auth_client(super_admin).delete(
            f"/api/v1/custom-fields/{field.pk}/"
        )
        assert response.status_code == 204
        field.refresh_from_db()
        assert field.is_active is False  # deactivated, not destroyed
