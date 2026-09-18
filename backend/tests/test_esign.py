"""Built-in e-signature (SRS 3.7.3, 3.7.5): envelope → token URL → click-to-sign →
immutable events with the document hash.

The token is stateless (`django.core.signing`), so revocation is state-based: every public
request re-checks the envelope — the tests prove that voiding kills a live token instantly.
"""
import re

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.collaboration import services
from apps.collaboration.models import EsignEnvelope, SignatureEvent

CONTENT = b"%PDF-1.4 the contract"
TOKEN_RE = re.compile(r"/public/esign/([^/\s]+)/")


def upload():
    return SimpleUploadedFile("contract.pdf", CONTENT, content_type="application/pdf")


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def document(agent_user, make_contact):
    file = services.store_file(actor=agent_user, uploaded_file=upload())
    return services.create_document(
        actor=agent_user, file=file, title="Sale contract",
        document_type="SALE_CONTRACT", links=[{"contact": make_contact()}],
    )


@pytest.fixture
def two_signer_envelope(agent_user, document, make_contact):
    first = make_contact(email="buyer@example.test")
    second = make_contact(email="seller@example.test")
    return services.create_envelope(
        actor=agent_user, document=document, subject="Please sign the sale contract",
        signers=[
            {"contact": first, "signing_order": 1},
            {"contact": second, "signing_order": 2},
        ],
    )


def token_from_last_email():
    return TOKEN_RE.search(mail.outbox[-1].body).group(1)


class TestSequentialFlow:
    def test_the_full_two_signer_walkthrough(
        self, db, api_client, agent_user, two_signer_envelope
    ):
        envelope = two_signer_envelope
        services.send_envelope(envelope, actor=agent_user)
        envelope.refresh_from_db()
        assert envelope.status == EsignEnvelope.Status.SENT

        # Sequential: only the first signer has been emailed.
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["buyer@example.test"]
        token = token_from_last_email()

        # The signer opens the page — VIEWED is recorded.
        page = api_client.get(f"/api/v1/public/esign/{token}/")
        assert page.status_code == 200
        assert page.data["signers"][0]["status"] == "VIEWED"
        assert envelope.signature_events.filter(
            event_type=SignatureEvent.EventType.VIEWED
        ).exists()

        # The document itself streams off the same token.
        doc = api_client.get(f"/api/v1/public/esign/{token}/document/")
        assert doc.status_code in (200, 302)

        # First signature: envelope moves to PARTIALLY_SIGNED, second signer is emailed.
        signed = api_client.post(
            f"/api/v1/public/esign/{token}/sign/",
            {"typed_name": "B. Uyer", "consent": True},
        )
        assert signed.status_code == 200, signed.data
        assert signed.data["envelope_status"] == "PARTIALLY_SIGNED"
        assert len(mail.outbox) == 2
        assert mail.outbox[1].to == ["seller@example.test"]

        # Second signature completes the envelope; both hash events carry the fingerprint.
        token2 = token_from_last_email()
        signed = api_client.post(
            f"/api/v1/public/esign/{token2}/sign/",
            {"typed_name": "S. Eller", "consent": True},
        )
        assert signed.status_code == 200, signed.data
        envelope.refresh_from_db()
        assert envelope.status == EsignEnvelope.Status.COMPLETED
        assert envelope.completed_at is not None

        expected_hash = envelope.document.file.checksum
        completed = envelope.signature_events.get(
            event_type=SignatureEvent.EventType.COMPLETED
        )
        assert completed.metadata["document_sha256"] == expected_hash
        for event in envelope.signature_events.filter(
            event_type=SignatureEvent.EventType.SIGNED
        ):
            assert event.metadata["document_sha256"] == expected_hash
            assert event.metadata["consent"] is True

    def test_out_of_turn_signing_is_refused(
        self, db, api_client, agent_user, two_signer_envelope
    ):
        services.send_envelope(two_signer_envelope, actor=agent_user)
        second = two_signer_envelope.signers.get(signing_order=2)
        token = services.sign_token(second)  # a keen signer bookmarking ahead
        response = api_client.post(
            f"/api/v1/public/esign/{token}/sign/",
            {"typed_name": "S. Eller", "consent": True},
        )
        assert response.status_code == 400
        assert "turn" in str(response.data)

    def test_consent_and_name_are_both_required(
        self, db, api_client, agent_user, two_signer_envelope
    ):
        services.send_envelope(two_signer_envelope, actor=agent_user)
        token = token_from_last_email()
        assert api_client.post(
            f"/api/v1/public/esign/{token}/sign/",
            {"typed_name": "B. Uyer", "consent": False},
        ).status_code == 400
        assert api_client.post(
            f"/api/v1/public/esign/{token}/sign/",
            {"typed_name": "   ", "consent": True},
        ).status_code == 400

    def test_decline_closes_the_envelope(
        self, db, api_client, agent_user, two_signer_envelope
    ):
        services.send_envelope(two_signer_envelope, actor=agent_user)
        token = token_from_last_email()
        response = api_client.post(
            f"/api/v1/public/esign/{token}/decline/", {"reason": "Price changed"}
        )
        assert response.status_code == 200
        two_signer_envelope.refresh_from_db()
        assert two_signer_envelope.status == EsignEnvelope.Status.DECLINED
        event = two_signer_envelope.signature_events.get(
            event_type=SignatureEvent.EventType.DECLINED
        )
        assert event.metadata["reason"] == "Price changed"


class TestTokens:
    def test_a_tampered_token_is_a_404(
        self, db, api_client, agent_user, two_signer_envelope
    ):
        services.send_envelope(two_signer_envelope, actor=agent_user)
        token = token_from_last_email()
        assert api_client.get(f"/api/v1/public/esign/{token}x/").status_code == 404
        assert api_client.get("/api/v1/public/esign/not-a-token/").status_code == 404

    def test_voiding_kills_every_outstanding_token(
        self, db, api_client, agent_user, two_signer_envelope
    ):
        """Stateless tokens, state-based revocation: the same URL that worked a moment ago
        is dead the instant the envelope is voided."""
        services.send_envelope(two_signer_envelope, actor=agent_user)
        token = token_from_last_email()
        assert api_client.get(f"/api/v1/public/esign/{token}/").status_code == 200
        services.void_envelope(two_signer_envelope, actor=agent_user, reason="re-issue")
        assert api_client.get(f"/api/v1/public/esign/{token}/").status_code == 404
        assert api_client.post(
            f"/api/v1/public/esign/{token}/sign/",
            {"typed_name": "B. Uyer", "consent": True},
        ).status_code == 404

    def test_a_draft_envelope_token_does_not_resolve(self, db, two_signer_envelope):
        signer = two_signer_envelope.signers.get(signing_order=1)
        assert services.resolve_token(services.sign_token(signer)) is None


class TestEnvelopeLifecycle:
    def test_creating_requires_edit_access(self, db, make_user, document, make_contact):
        stranger = make_user("agent")
        with pytest.raises(ValidationError, match="edit access"):
            services.create_envelope(
                actor=stranger, document=document, subject="Nope",
                signers=[{"contact": make_contact(email="x@example.test")}],
            )

    def test_signer_is_exactly_contact_or_user(
        self, db, agent_user, document, make_contact
    ):
        with pytest.raises(ValidationError, match="exactly one"):
            services.create_envelope(
                actor=agent_user, document=document, subject="Ambiguous",
                signers=[{"contact": make_contact(), "user": agent_user}],
            )

    def test_a_completed_envelope_cannot_be_voided(
        self, db, api_client, agent_user, document, make_contact
    ):
        envelope = services.create_envelope(
            actor=agent_user, document=document, subject="One signer",
            signers=[{"contact": make_contact(email="solo@example.test")}],
        )
        services.send_envelope(envelope, actor=agent_user)
        token = token_from_last_email()
        api_client.post(
            f"/api/v1/public/esign/{token}/sign/",
            {"typed_name": "S. Olo", "consent": True},
        )
        envelope.refresh_from_db()
        assert envelope.status == EsignEnvelope.Status.COMPLETED
        with pytest.raises(ValidationError, match="Cannot move"):
            services.void_envelope(envelope, actor=agent_user, reason="too late")

    def test_the_staff_api_is_closed_to_portal_clients(
        self, db, auth_client, portal_tenant
    ):
        user = portal_tenant["user"]
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        assert (
            auth_client(user).get("/api/v1/esign/envelopes/").status_code == 403
        )

    def test_creator_is_notified_on_completion(
        self, db, api_client, agent_user, document, make_contact
    ):
        from apps.collaboration.models import Notification

        envelope = services.create_envelope(
            actor=agent_user, document=document, subject="One signer",
            signers=[{"contact": make_contact(email="solo@example.test")}],
        )
        services.send_envelope(envelope, actor=agent_user)
        api_client.post(
            f"/api/v1/public/esign/{token_from_last_email()}/sign/",
            {"typed_name": "S. Olo", "consent": True},
        )
        assert Notification.objects.filter(
            recipient=agent_user, entity_type="ESIGN_ENVELOPE", entity_id=envelope.pk
        ).exists()


class TestSweep:
    def test_expiry_pass_is_idempotent(self, db, agent_user, two_signer_envelope):
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils import timezone

        envelope = two_signer_envelope
        services.send_envelope(envelope, actor=agent_user)
        envelope.expires_at = timezone.now() - timedelta(hours=1)
        envelope.save(update_fields=["expires_at"])

        call_command("sweep_esign")
        envelope.refresh_from_db()
        assert envelope.status == EsignEnvelope.Status.EXPIRED
        voided = envelope.signature_events.filter(
            event_type=SignatureEvent.EventType.VOIDED
        )
        assert voided.count() == 1
        call_command("sweep_esign")  # run-twice-changes-nothing
        assert voided.count() == 1

    def test_remind_pass_nudges_and_records(self, db, agent_user, two_signer_envelope):
        from django.core.management import call_command

        services.send_envelope(two_signer_envelope, actor=agent_user)
        assert len(mail.outbox) == 1
        # Default window (3 days): the SENT event is fresh, nothing happens.
        call_command("sweep_esign")
        assert len(mail.outbox) == 1
        # Forced window: the current signer is re-emailed and the nudge is on the record.
        call_command("sweep_esign", "--remind-after-days", "0")
        assert len(mail.outbox) == 2
        assert mail.outbox[1].to == ["buyer@example.test"]
        assert two_signer_envelope.signature_events.filter(
            event_type=SignatureEvent.EventType.REMINDER_SENT
        ).count() == 1
