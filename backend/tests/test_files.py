"""File storage (SRS 3.3.3, 3.7.1) — the first code that can put a byte in object storage.

`collaboration_file` sat as the target of every deferred `*_file_id` FK with nothing able to
write it; these tests prove the single ingestion point and the gated egress.
"""
import hashlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.collaboration import services

CONTENT = b"\x89PNG fake image bytes for testing"


def upload(name="photo.png", content=CONTENT, content_type="image/png"):
    return SimpleUploadedFile(name, content, content_type=content_type)


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


class TestStoreFile:
    def test_the_row_records_what_was_actually_stored(self, db, agent_user):
        file = services.store_file(actor=agent_user, uploaded_file=upload())
        assert file.original_name == "photo.png"
        assert file.mime_type == "image/png"
        assert file.size_bytes == len(CONTENT)
        assert file.checksum == hashlib.sha256(CONTENT).hexdigest()
        assert file.uploaded_by_id == agent_user.pk
        # The stored key is what the storage returned, and the bytes round-trip.
        with services.files.open_file(file) as handle:
            assert handle.read() == CONTENT

    def test_an_oversize_file_is_refused(self, db, agent_user, settings):
        from django.core.exceptions import ValidationError

        settings.FILE_UPLOAD_MAX_BYTES = 10
        with pytest.raises(ValidationError, match="limit"):
            services.store_file(actor=agent_user, uploaded_file=upload())

    def test_a_disallowed_type_is_refused(self, db, agent_user):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="Unsupported"):
            services.store_file(
                actor=agent_user,
                uploaded_file=upload("run.exe", b"MZ...", "application/x-msdownload"),
            )

    def test_an_empty_file_is_refused(self, db, agent_user):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="empty"):
            services.store_file(actor=agent_user, uploaded_file=upload(content=b""))

    def test_the_upload_is_audited(self, db, agent_user):
        from apps.platform.models import AuditEvent

        file = services.store_file(actor=agent_user, uploaded_file=upload())
        event = AuditEvent.objects.get(entity_type="FILE", entity_id=file.pk)
        assert event.new_values["checksum"] == file.checksum

    def test_rehash_matches(self, db, agent_user):
        """`file_sha256` re-reads the stored bytes — the e-sign completion fingerprint must
        come from storage, not from the row."""
        file = services.store_file(actor=agent_user, uploaded_file=upload())
        assert services.file_sha256(file) == file.checksum


class TestFileApi:
    def test_upload_and_download_round_trip(self, db, auth_client, agent_user):
        client = auth_client(agent_user)
        created = client.post(
            "/api/v1/files/", {"file": upload()}, format="multipart"
        )
        assert created.status_code == 201, created.data
        assert "storage_key" not in created.data  # internal pointer, never serialized

        response = client.get(f"/api/v1/files/{created.data['id']}/download/")
        # Filesystem storage in tests streams; S3 would 302. Either is a success.
        assert response.status_code in (200, 302)
        if response.status_code == 200:
            assert b"".join(response.streaming_content) == CONTENT

    def test_a_strangers_file_is_a_404(self, db, auth_client, agent_user, make_user):
        """Not 403: confirming the id exists is itself a disclosure."""
        file = services.store_file(actor=agent_user, uploaded_file=upload())
        response = auth_client(make_user("agent")).get(
            f"/api/v1/files/{file.pk}/download/"
        )
        assert response.status_code == 404

    def test_an_owner_scope_reads_any_file(self, db, auth_client, agent_user, owner):
        file = services.store_file(actor=agent_user, uploaded_file=upload())
        assert (
            auth_client(owner).get(f"/api/v1/files/{file.pk}/download/").status_code
            in (200, 302)
        )

    def test_a_file_behind_visible_media_is_downloadable(
        self, db, auth_client, agent_user, make_user, make_property
    ):
        """Access follows the wrapping aggregate: the agent who can see the listing's media
        can pull its bytes, whoever uploaded them."""
        from apps.inventory import services as inventory_services
        from apps.inventory.models import Listing, Media

        pm = make_user("property_manager")
        file = services.store_file(actor=pm, uploaded_file=upload())
        listing = inventory_services.create_listing(
            actor=pm, property=make_property(managed_by=pm),
            listing_type=Listing.ListingType.SALE, title="With photo",
            assigned_agent=agent_user,
        )
        inventory_services.add_media(
            actor=pm, listing=listing, media_type=Media.MediaType.PHOTO, file=file
        )
        assert (
            auth_client(agent_user)
            .get(f"/api/v1/files/{file.pk}/download/")
            .status_code
            in (200, 302)
        )

    def test_a_portal_client_cannot_upload(self, db, auth_client, portal_tenant):
        client = auth_client(portal_tenant["user"])
        client.post(
            "/api/v1/auth/change-password/",
            {"current_password": "client-pass-55512", "new_password": "client-own-88221"},
            format="json",
        )
        response = client.post("/api/v1/files/", {"file": upload()}, format="multipart")
        assert response.status_code == 403  # StaffWrite: uploads arrive with maintenance's carve-out
