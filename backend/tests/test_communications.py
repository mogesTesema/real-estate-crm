"""Threads, messages, call logs, notes and templates (SRS §3.9).

The doctrine under test: messages are immutable history (a FAILED send stays on the record),
inbound webhooks deduplicate on (channel, provider_message_id), and mention fan-out pings
newly-mentioned users exactly once.
"""
import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.collaboration import services
from apps.collaboration.models import Message, Thread


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def contact(make_contact):
    return make_contact(email="amina@example.test", phone="+971501234567")


class TestSendMessage:
    def test_an_email_sends_and_threads(self, db, agent_user, contact):
        message = services.send_message(
            actor=agent_user, channel="EMAIL", contact=contact,
            subject="Viewing follow-up", body="Hello {{ contact.first_name }}",
        )
        assert message.status == Message.Status.SENT
        assert message.body == f"Hello {contact.first_name}"  # merge ran
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["amina@example.test"]
        thread = message.thread
        assert thread.contact == contact
        assert thread.last_message_at is not None

        # A second email lands on the SAME open thread.
        again = services.send_message(
            actor=agent_user, channel="EMAIL", contact=contact, body="And another thing"
        )
        assert again.thread_id == thread.pk

    def test_template_xor_body(self, db, agent_user, contact):
        with pytest.raises(ValidationError, match="exactly one"):
            services.send_message(actor=agent_user, channel="EMAIL", contact=contact)
        template = services.create_template(
            actor=agent_user, name="Hi", channel="EMAIL", body="Hi there"
        )
        with pytest.raises(ValidationError, match="exactly one"):
            services.send_message(
                actor=agent_user, channel="EMAIL", contact=contact,
                template=template, body="both",
            )

    def test_a_wrong_channel_template_is_refused(self, db, agent_user, contact):
        sms_template = services.create_template(
            actor=agent_user, name="SMS ping", channel="SMS", body="Ping"
        )
        with pytest.raises(ValidationError, match="different channel"):
            services.send_message(
                actor=agent_user, channel="EMAIL", contact=contact, template=sms_template
            )

    def test_a_failed_send_is_preserved_as_history(
        self, db, agent_user, contact, monkeypatch
    ):
        """SRS 3.9's "complete history" includes the messages that did not send."""
        from apps.collaboration.services import communications

        def broken_gateway(channel):
            raise RuntimeError("provider down")

        monkeypatch.setattr(communications, "get_gateway", broken_gateway)
        message = services.send_message(
            actor=agent_user, channel="EMAIL", contact=contact, body="Doomed"
        )
        assert message.status == Message.Status.FAILED
        assert Message.objects.filter(pk=message.pk).exists()

    def test_an_explicit_thread_drifts_to_mixed(self, db, agent_user, contact):
        first = services.send_message(
            actor=agent_user, channel="EMAIL", contact=contact, body="Start on email"
        )
        thread = first.thread
        services.send_message(
            actor=agent_user, channel="SMS", thread=thread, to_address=contact.phone,
            body="Continued by text",
        )
        thread.refresh_from_db()
        assert thread.channel == Thread.Channel.MIXED

    def test_sms_uses_the_logging_mock(self, db, agent_user, contact):
        message = services.send_message(
            actor=agent_user, channel="SMS", contact=contact, body="Short one"
        )
        assert message.status == Message.Status.SENT
        assert message.provider_message_id.startswith("mock-")

    def test_no_address_on_file_is_a_clean_400(self, db, agent_user, make_contact):
        phoneless = make_contact(phone=None)
        with pytest.raises(ValidationError, match="address"):
            services.send_message(
                actor=agent_user, channel="SMS", contact=phoneless, body="To whom?"
            )


class TestInbound:
    def test_a_retried_webhook_is_one_message(self, db, contact):
        kwargs = dict(
            channel="EMAIL", from_address="amina@example.test",
            body="Is the villa still available?", provider_message_id="prov-123",
        )
        first = services.record_inbound_message(**kwargs)
        second = services.record_inbound_message(**kwargs)
        assert first.pk == second.pk
        assert Message.objects.filter(provider_message_id="prov-123").count() == 1
        assert first.contact == contact  # matched by normalized email
        assert first.direction == Message.Direction.INBOUND

    def test_an_unknown_sender_still_lands_in_a_thread(self, db):
        message = services.record_inbound_message(
            channel="SMS", from_address="+971509999999", body="Who dis",
        )
        assert message.contact is None
        assert message.thread.contact is None

    def test_the_assignee_is_notified(self, db, agent_user, contact):
        from apps.collaboration.models import Notification

        outbound = services.send_message(
            actor=agent_user, channel="EMAIL", contact=contact, body="Opening move"
        )
        services.record_inbound_message(
            channel="EMAIL", from_address="amina@example.test", body="Reply",
        )
        assert Notification.objects.filter(
            recipient=agent_user, entity_type="THREAD",
            entity_id=outbound.thread.pk,
        ).exists()


class TestStatusCallbacks:
    def test_status_advances_monotonically(self, db, agent_user, contact):
        message = services.send_message(
            actor=agent_user, channel="SMS", contact=contact, body="Track me"
        )
        pid = message.provider_message_id
        services.update_message_status(
            channel="SMS", provider_message_id=pid, status="DELIVERED"
        )
        message.refresh_from_db()
        assert message.status == "DELIVERED"
        assert message.delivered_at is not None
        # A late, out-of-order SENT callback cannot regress it.
        services.update_message_status(
            channel="SMS", provider_message_id=pid, status="SENT"
        )
        message.refresh_from_db()
        assert message.status == "DELIVERED"

    def test_an_unknown_id_is_a_quiet_noop(self, db):
        assert services.update_message_status(
            channel="SMS", provider_message_id="never-heard-of-it", status="DELIVERED"
        ) is None


class TestNotes:
    def test_mention_fan_out_pings_new_mentions_only(
        self, db, agent_user, make_user, contact
    ):
        from apps.collaboration.models import Notification

        colleague = make_user("agent")
        note = services.create_note(
            actor=agent_user, body="@colleague can you take this?",
            mentions=[colleague.pk], contact=contact,
        )
        pings = Notification.objects.filter(recipient=colleague, type="MENTION")
        assert pings.count() == 1

        # An edit that keeps the mention does not re-ping.
        services.update_note(
            note, actor=agent_user, body="edited", mentions=[colleague.pk]
        )
        assert pings.count() == 1

        # Adding someone new pings only them.
        second = make_user("agent")
        services.update_note(
            note, actor=agent_user, mentions=[colleague.pk, second.pk]
        )
        assert pings.count() == 1
        assert Notification.objects.filter(recipient=second, type="MENTION").count() == 1

    def test_a_note_needs_a_target(self, db, agent_user):
        with pytest.raises(ValidationError, match="at least one record"):
            services.create_note(actor=agent_user, body="Floating thought")

    def test_only_the_author_edits(self, db, agent_user, make_user, contact):
        note = services.create_note(actor=agent_user, body="Mine", contact=contact)
        with pytest.raises(ValidationError, match="author"):
            services.update_note(note, actor=make_user("agent"), body="Not yours")


class TestTemplates:
    def test_unknown_merge_variables_fail_at_authoring(self, db, agent_user):
        """A template promising `{{ contact.middle_name }}` would fail loudly at send
        time — catch it when it is written."""
        with pytest.raises(ValidationError, match="middle_name"):
            services.create_template(
                actor=agent_user, name="Broken", channel="EMAIL",
                body="Dear {{ contact.middle_name }}",
            )

    def test_preview_renders_with_a_real_contact(
        self, db, auth_client, agent_user, contact
    ):
        template = services.create_template(
            actor=agent_user, name="Greeting", channel="EMAIL",
            body="Dear {{ contact.first_name }}",
        )
        response = auth_client(agent_user).post(
            f"/api/v1/templates/{template.pk}/preview/", {"contact": str(contact.pk)}
        )
        assert response.status_code == 200
        assert response.data["body"] == f"Dear {contact.first_name}"

    def test_templates_are_staff_only(self, db, auth_client, portal_tenant):
        user = portal_tenant["user"]
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        assert auth_client(user).get("/api/v1/templates/").status_code == 403


class TestCallLogsAndThreadsApi:
    def test_log_a_call_against_a_contact(self, db, auth_client, agent_user, contact):
        response = auth_client(agent_user).post(
            "/api/v1/call-logs/",
            {
                "direction": "OUTBOUND", "phone_number": "+971501234567",
                "started_at": timezone.now().isoformat(), "outcome": "ANSWERED",
                "duration_seconds": 90, "contact": str(contact.pk),
            },
        )
        assert response.status_code == 201, response.data
        assert response.data["user"] is not None  # the caller is always attributed

    def test_thread_close_and_reopen(self, db, auth_client, agent_user, contact):
        message = services.send_message(
            actor=agent_user, channel="EMAIL", contact=contact, body="Opening"
        )
        client = auth_client(agent_user)
        thread_id = message.thread.pk
        assert client.post(f"/api/v1/threads/{thread_id}/close/").data["is_closed"]
        # A closed thread is not picked up by the next send — a new one is born.
        second = services.send_message(
            actor=agent_user, channel="EMAIL", contact=contact, body="New topic"
        )
        assert second.thread_id != thread_id
        assert not client.post(f"/api/v1/threads/{thread_id}/reopen/").data["is_closed"]

    def test_compose_endpoint_sends(self, db, auth_client, agent_user, contact):
        response = auth_client(agent_user).post(
            "/api/v1/messages/",
            {"channel": "EMAIL", "contact": str(contact.pk), "body": "Via the API"},
        )
        assert response.status_code == 201, response.data
        assert response.data["status"] == "SENT"
        assert len(mail.outbox) == 1

    def test_a_strangers_thread_is_invisible(
        self, db, auth_client, agent_user, make_user, contact
    ):
        services.send_message(
            actor=agent_user, channel="EMAIL", contact=contact, body="Private"
        )
        stranger = make_user("agent")
        assert auth_client(stranger).get("/api/v1/threads/").data["results"] == []
