"""Document repository, access levels and versions (SRS 3.7.1, 3.7.4, 3.7.5).

The heart of these tests is `document_access_level` — one function answers for the scoping
arm, the download gate and every write service, so the tests probe the function AND the two
surfaces that must agree with it.
"""
import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.collaboration import selectors, services

CONTENT = b"%PDF-1.4 fake deed bytes"


def upload(name="deed.pdf"):
    return SimpleUploadedFile(name, CONTENT, content_type="application/pdf")


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def make_document(agent_user, make_contact):
    def _make(actor=None, *, confidential=False, links=None, **kwargs):
        actor = actor or agent_user
        file = services.store_file(actor=actor, uploaded_file=upload())
        return services.create_document(
            actor=actor,
            file=file,
            title=kwargs.pop("title", "Title deed"),
            document_type=kwargs.pop("document_type", "PROPERTY_DEED"),
            is_confidential=confidential,
            links=links or [{"contact": make_contact()}],
            **kwargs,
        )

    return _make


class TestCreate:
    def test_a_document_requires_a_link(self, db, agent_user):
        file = services.store_file(actor=agent_user, uploaded_file=upload())
        with pytest.raises(ValidationError, match="linked"):
            services.create_document(
                actor=agent_user, file=file, title="Orphan",
                document_type="OTHER", links=[],
            )

    def test_attaching_a_strangers_file_is_refused(self, db, make_user, make_contact):
        """Any staff member publishing files they cannot even download would be a leak."""
        a, b = make_user("agent"), make_user("agent")
        file = services.store_file(actor=a, uploaded_file=upload())
        with pytest.raises(ValidationError, match="uploaded"):
            services.create_document(
                actor=b, file=file, title="Not mine", document_type="OTHER",
                links=[{"contact": make_contact()}],
            )

    def test_creation_is_audited(self, db, make_document):
        from apps.platform.models import AuditEvent

        document = make_document()
        assert AuditEvent.objects.filter(
            entity_type="DOCUMENT", entity_id=document.pk, action="CREATE"
        ).exists()


class TestAccessLevels:
    def test_the_uploader_always_edits(self, db, make_document, agent_user):
        document = make_document(confidential=True)
        assert selectors.document_access_level(agent_user, document) == "EDIT"

    def test_all_scope_edits_non_confidential(self, db, make_document, owner):
        assert selectors.document_access_level(owner, make_document()) == "EDIT"

    def test_the_confidential_inversion(self, db, make_document, owner, agent_user):
        """SRS 3.7.4's one inversion: ALL-scope staff get NOTHING on a confidential
        document without an explicit grant — otherwise "confidential" is a label."""
        document = make_document(confidential=True)
        assert selectors.document_access_level(owner, document) is None
        services.grant_access(
            document, actor=agent_user, user=owner, access_level="VIEW"
        )
        assert selectors.document_access_level(owner, document) == "VIEW"

    def test_grants_take_the_max_across_roles_and_user(
        self, db, make_document, agent_user, make_user, roles
    ):
        colleague = make_user("agent")
        document = make_document(confidential=True)
        services.grant_access(
            document, actor=agent_user, role=roles["agent"], access_level="VIEW"
        )
        services.grant_access(
            document, actor=agent_user, user=colleague, access_level="EDIT"
        )
        assert selectors.document_access_level(colleague, document) == "EDIT"

    def test_a_stranger_staff_member_gets_nothing(self, db, make_document, make_user):
        """OWN-scope staff reach a document only through a linked record they can see."""
        stranger = make_user("agent")
        assert selectors.document_access_level(stranger, make_document()) is None

    def test_scoping_arm_agrees_with_the_function(
        self, db, auth_client, make_document, owner, agent_user
    ):
        """The list endpoint and the access function must answer identically — here for
        the confidential inversion."""
        plain = make_document()
        secret = make_document(confidential=True)
        listed = {
            row["id"]
            for row in auth_client(owner).get("/api/v1/documents/").data["results"]
        }
        assert str(plain.pk) in listed
        assert str(secret.pk) not in listed
        services.grant_access(secret, actor=agent_user, user=owner, access_level="VIEW")
        listed = {
            row["id"]
            for row in auth_client(owner).get("/api/v1/documents/").data["results"]
        }
        assert str(secret.pk) in listed


class TestDownloadGate:
    def test_download_audits_the_view(self, db, auth_client, make_document, agent_user):
        from apps.platform.models import AuditEvent

        document = make_document()
        response = auth_client(agent_user).get(
            f"/api/v1/documents/{document.pk}/download/"
        )
        assert response.status_code in (200, 302)
        assert AuditEvent.objects.filter(
            entity_type="DOCUMENT", entity_id=document.pk, action="VIEW", actor_user=agent_user,
        ).exists()

    def test_a_view_only_grant_cannot_download(
        self, db, auth_client, make_document, make_user, agent_user
    ):
        colleague = make_user("agent")
        document = make_document(confidential=True)
        services.grant_access(
            document, actor=agent_user, user=colleague, access_level="VIEW"
        )
        assert (
            auth_client(colleague)
            .get(f"/api/v1/documents/{document.pk}/download/")
            .status_code
            == 404
        )

    def test_portal_client_downloads_their_lease_document(
        self, db, auth_client, portal_tenant, agent_user
    ):
        file = services.store_file(actor=agent_user, uploaded_file=upload())
        document = services.create_document(
            actor=agent_user, file=file, title="Lease copy", document_type="LEASE",
            links=[{"lease": portal_tenant["lease"]}],
        )
        user = portal_tenant["user"]
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        client = auth_client(user)
        assert (
            client.get(f"/api/v1/documents/{document.pk}/download/").status_code
            in (200, 302)
        )

    def test_a_confidential_lease_document_is_invisible_to_the_portal(
        self, db, auth_client, portal_tenant, agent_user
    ):
        file = services.store_file(actor=agent_user, uploaded_file=upload())
        document = services.create_document(
            actor=agent_user, file=file, title="Internal valuation",
            document_type="OTHER", is_confidential=True,
            links=[{"lease": portal_tenant["lease"]}],
        )
        user = portal_tenant["user"]
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        client = auth_client(user)
        assert (
            client.get(f"/api/v1/documents/{document.pk}/download/").status_code == 404
        )
        listed = client.get("/api/v1/documents/").data["results"]
        assert str(document.pk) not in {row["id"] for row in listed}


class TestVersions:
    def test_a_new_version_bumps_in_place_and_audits(
        self, db, make_document, agent_user
    ):
        from apps.platform.models import AuditEvent

        document = make_document()
        old_file_id = document.file_id
        replacement = services.store_file(actor=agent_user, uploaded_file=upload("v2.pdf"))
        services.add_document_version(document, actor=agent_user, file=replacement)
        document.refresh_from_db()
        assert document.version == 2
        assert document.file_id == replacement.pk
        event = AuditEvent.objects.filter(
            entity_type="DOCUMENT", entity_id=document.pk, action="UPDATE"
        ).latest("created_at")
        assert event.old_values["file_id"] == str(old_file_id)
        assert event.new_values["version"] == 2

    def test_versioning_is_blocked_while_an_envelope_is_live(
        self, db, make_document, agent_user, make_contact
    ):
        """You cannot swap the paper under someone's pen."""
        document = make_document()
        envelope = services.create_envelope(
            actor=agent_user, document=document, subject="Sign this",
            signers=[{"contact": make_contact(email="s1@example.test")}],
        )
        services.send_envelope(envelope, actor=agent_user)
        replacement = services.store_file(actor=agent_user, uploaded_file=upload())
        with pytest.raises(ValidationError, match="void"):
            services.add_document_version(document, actor=agent_user, file=replacement)
        services.void_envelope(envelope, actor=agent_user, reason="re-draft")
        services.add_document_version(document, actor=agent_user, file=replacement)


class TestLinksAndLifecycle:
    def test_the_last_link_is_protected(self, db, make_document, agent_user):
        document = make_document()
        link = document.links.get()
        with pytest.raises(ValidationError, match="at least one link"):
            services.unlink_document(link, actor=agent_user)

    def test_unlink_works_once_a_second_link_exists(
        self, db, make_document, agent_user, make_contact
    ):
        document = make_document()
        first = document.links.get()
        services.link_document(document, actor=agent_user, contact=make_contact())
        services.unlink_document(first, actor=agent_user)
        assert document.links.count() == 1

    def test_delete_is_soft_and_hides_the_row(
        self, db, auth_client, make_document, agent_user
    ):
        document = make_document()
        response = auth_client(agent_user).delete(f"/api/v1/documents/{document.pk}/")
        assert response.status_code == 204
        document.refresh_from_db()
        assert document.deleted_at is not None
        assert (
            auth_client(agent_user).get(f"/api/v1/documents/{document.pk}/").status_code
            == 404
        )

    def test_only_edit_access_mutates(self, db, make_document, make_user):
        stranger = make_user("agent")
        document = make_document()
        with pytest.raises(ValidationError, match="edit access"):
            services.update_document(document, actor=stranger, title="Hijacked")


class TestGeneration:
    def test_generate_from_template_merges_and_stores(
        self, db, agent_user, make_contact
    ):
        template = services.create_template(
            actor=agent_user, name="Welcome", channel="EMAIL",
            body="Dear {{ contact.first_name }}, welcome.",
        )
        contact = make_contact(first_name="Amina")
        document = services.generate_document_from_template(
            actor=agent_user, template=template, title="Welcome letter",
            document_type="OTHER", links=[{"contact": contact}], contact=contact,
        )
        with services.files.open_file(document.file) as handle:
            html = handle.read().decode()
        assert "Dear Amina, welcome." in html
        assert document.links.get().contact_id == contact.pk
