"""The notification engine (SRS §3.18) — `collaboration.services.notify`.

Two properties carry this module: `notify()` never raises (a lease activation must not roll
back because SMTP is down), and suppression is an *outcome* — when a preference or quiet
hours block a channel, the dispatch log records the decision, because "why didn't I get
notified?" is only answerable if the decision not to notify was written down.
"""
from datetime import UTC

import pytest
from django.core import mail

from apps.collaboration import services
from apps.collaboration.models import Notification, NotificationDispatchLog


@pytest.fixture
def recipient(make_user):
    return make_user("agent")


@pytest.fixture(autouse=True)
def _clear_gateway_cache():
    from apps.collaboration.gateways import get_gateway

    get_gateway.cache_clear()
    yield
    get_gateway.cache_clear()


def send(recipient, **kwargs):
    kwargs.setdefault("type", "LEAD_ASSIGNED")
    kwargs.setdefault("title", "Lead assigned: Marina villa")
    return services.notify(recipient=recipient, **kwargs)


class TestNotify:
    def test_creates_the_feed_item_and_sends_email(self, db, recipient):
        mail.outbox.clear()
        notification = send(recipient, body="Go get it.")
        assert notification is not None
        assert notification.recipient_id == recipient.pk
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [recipient.email]

        logs = {log.channel: log for log in NotificationDispatchLog.objects.all()}
        assert logs["IN_APP"].status == "SENT"
        assert logs["EMAIL"].status == "SENT"

    def test_sms_is_off_by_default(self, db, recipient):
        """The model default. An unconfigured user gets in-app/email/push, not texts."""
        send(recipient)
        sms = NotificationDispatchLog.objects.get(channel="SMS")
        assert sms.status == "SUPPRESSED_PREFERENCE"

    def test_a_disabled_channel_is_logged_not_silent(self, db, recipient):
        services.set_notification_preference(
            actor=recipient, notification_type="LEAD_ASSIGNED", email_enabled=False
        )
        mail.outbox.clear()
        send(recipient)
        assert mail.outbox == []
        email = NotificationDispatchLog.objects.get(channel="EMAIL")
        assert email.status == "SUPPRESSED_PREFERENCE"

    def test_quiet_hours_suppress_channels_but_never_the_feed(self, db, recipient):
        """Quiet hours silence the phone, not the record — the in-app row is where a
        suppressed email's content survives."""
        services.set_notification_preference(
            actor=recipient,
            notification_type="LEAD_ASSIGNED",
            quiet_hours_start="00:00",
            quiet_hours_end="23:59",
            timezone="Asia/Dubai",
        )
        mail.outbox.clear()
        notification = send(recipient)
        assert notification is not None  # feed row landed
        assert mail.outbox == []
        assert NotificationDispatchLog.objects.filter(
            channel="EMAIL", status="SUPPRESSED_QUIET_HOURS"
        ).exists()

    def test_a_midnight_crossing_window_works(self, db, recipient):
        """22:00–07:00 means `t >= start or t < end` — the naive comparison inverts it."""
        from datetime import time

        from apps.collaboration.models import NotificationPreference
        from apps.collaboration.services.notify import _in_quiet_hours

        pref = NotificationPreference(
            user=recipient,
            notification_type="LEAD_ASSIGNED",
            quiet_hours_start=time(22, 0),
            quiet_hours_end=time(7, 0),
            timezone="UTC",
        )
        from datetime import datetime

        assert _in_quiet_hours(pref, now=datetime(2026, 1, 1, 23, 30, tzinfo=UTC))
        assert _in_quiet_hours(pref, now=datetime(2026, 1, 1, 6, 30, tzinfo=UTC))
        assert not _in_quiet_hours(pref, now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    def test_a_broken_gateway_costs_one_channel_not_the_notification(
        self, db, recipient, settings
    ):
        """A gateway that cannot even import is a FAILED dispatch on that channel; the feed
        row and the other channels are untouched, and nothing raises into the caller."""
        settings.COLLABORATION_GATEWAYS = {
            "EMAIL": "apps.collaboration.tests_do_not_exist.Boom"
        }
        from apps.collaboration.gateways import get_gateway

        get_gateway.cache_clear()
        notification = send(recipient)  # must not raise
        assert notification is not None  # the feed write survived
        assert NotificationDispatchLog.objects.filter(
            channel="EMAIL", status="FAILED"
        ).exists()
        assert NotificationDispatchLog.objects.filter(
            channel="IN_APP", status="SENT"
        ).exists()

    def test_a_failed_send_is_logged_failed(self, db, recipient, monkeypatch):
        import importlib

        from apps.collaboration import gateways

        class Exploding(gateways.MessageGateway):
            channel = "EMAIL"

            def send(self, **kwargs):
                return gateways.GatewayResult("x", "FAILED", "smtp down")

        notify_module = importlib.import_module("apps.collaboration.services.notify")
        monkeypatch.setattr(notify_module, "get_gateway", lambda channel: Exploding())
        send(recipient)
        assert NotificationDispatchLog.objects.filter(
            channel="EMAIL", status="FAILED", error_detail="smtp down"
        ).exists()

    def test_an_inactive_recipient_gets_nothing(self, db, make_user):
        ghost = make_user("agent")
        ghost.is_active = False
        ghost.save(update_fields=["is_active"])
        assert send(ghost) is None
        assert Notification.objects.count() == 0

    def test_an_unknown_type_is_refused_quietly(self, db, recipient):
        assert send(recipient, type="NOT_A_TYPE") is None


class TestPreferences:
    def test_quiet_hours_need_both_ends_and_a_timezone(self, db, recipient):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            services.set_notification_preference(
                actor=recipient, notification_type="LEAD_ASSIGNED",
                quiet_hours_start="22:00",
            )
        with pytest.raises(ValidationError):
            services.set_notification_preference(
                actor=recipient, notification_type="LEAD_ASSIGNED",
                quiet_hours_start="22:00", quiet_hours_end="07:00",
            )

    def test_an_unknown_timezone_is_refused(self, db, recipient):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            services.set_notification_preference(
                actor=recipient, notification_type="LEAD_ASSIGNED",
                quiet_hours_start="22:00", quiet_hours_end="07:00",
                timezone="Mars/Olympus",
            )


class TestFeedApi:
    URL = "/api/v1/notifications/"

    def test_a_user_sees_only_their_own_feed(self, db, auth_client, recipient, make_user):
        other = make_user("agent")
        send(recipient)
        send(other)
        response = auth_client(recipient).get(self.URL)
        assert response.data["count"] == 1

    def test_unread_count_and_mark_read(self, db, auth_client, recipient):
        notification = send(recipient)
        client = auth_client(recipient)
        assert client.get(f"{self.URL}unread-count/").data["unread"] == 1
        assert client.post(f"{self.URL}{notification.pk}/mark-read/").status_code == 200
        assert client.get(f"{self.URL}unread-count/").data["unread"] == 0

    def test_mark_all_read(self, db, auth_client, recipient):
        for _ in range(3):
            send(recipient)
        response = auth_client(recipient).post(f"{self.URL}mark-all-read/")
        assert response.data["marked"] == 3

    def test_you_cannot_mark_someone_elses_read(self, db, auth_client, recipient, make_user):
        notification = send(recipient)
        other = make_user("agent")
        assert (
            auth_client(other)
            .post(f"{self.URL}{notification.pk}/mark-read/")
            .status_code
            == 404  # scoped queryset: it isn't even visible
        )

    def test_a_portal_client_reads_and_marks_their_own(self, db, auth_client, portal_tenant):
        user = portal_tenant["user"]
        send(user, type="LEASE_EXPIRING", title="Your lease is expiring")
        client = auth_client(user)
        response = client.get(self.URL)
        # PasswordIsCurrent blocks a fresh invitee elsewhere; the feed views allow stale?
        # No — must_change_password still gates. Settle the account first.
        if response.status_code == 403:
            client.post(
                "/api/v1/auth/change-password/",
                {"current_password": "client-pass-55512", "new_password": "client-own-88221"},
                format="json",
            )
            response = client.get(self.URL)
        assert response.status_code == 200
        assert response.data["count"] == 1
        note_id = response.data["results"][0]["id"]
        assert client.post(f"{self.URL}{note_id}/mark-read/").status_code == 200

    def test_preferences_round_trip(self, db, auth_client, recipient):
        client = auth_client(recipient)
        listing = client.get("/api/v1/notification-preferences/")
        assert listing.status_code == 200
        assert len(listing.data) == len(Notification.Type.values)
        updated = client.put(
            "/api/v1/notification-preferences/LEAD_ASSIGNED/",
            {"email_enabled": False, "sms_enabled": True},
            format="json",
        )
        assert updated.status_code == 200, updated.data
        assert updated.data["email_enabled"] is False
        assert updated.data["sms_enabled"] is True


class TestDomainCallSites:
    """The §1.2 notify orchestrations wired in Phase A — each proven from the domain side,
    because the one bug ruff caught here was a notify branch no test had ever entered."""

    def test_booking_a_viewing_for_another_agent_notifies_them(
        self, db, make_user, make_property, make_contact
    ):
        from django.utils import timezone

        from apps.crm import services as crm_services

        pm = make_user("property_manager")
        agent = make_user("agent")
        start = timezone.now() + timezone.timedelta(days=1)
        crm_services.schedule_viewing(
            actor=pm,
            property=make_property(managed_by=pm),
            contact=make_contact(),
            agent=agent,
            scheduled_start=start,
            scheduled_end=start + timezone.timedelta(hours=1),
        )
        note = Notification.objects.get(recipient=agent)
        assert note.type == "VIEWING_SCHEDULED"

    def test_booking_your_own_viewing_does_not_notify_yourself(
        self, db, make_user, make_property, make_contact
    ):
        from django.utils import timezone

        from apps.crm import services as crm_services

        agent = make_user("agent")
        start = timezone.now() + timezone.timedelta(days=1)
        crm_services.schedule_viewing(
            actor=agent,
            property=make_property(),
            contact=make_contact(),
            agent=agent,
            scheduled_start=start,
            scheduled_end=start + timezone.timedelta(hours=1),
        )
        assert not Notification.objects.filter(recipient=agent).exists()

    def test_assigning_a_lead_notifies_the_assignee(self, db, make_user, make_contact):
        from apps.crm import services as crm_services
        from apps.crm.models import Lead

        manager, agent = make_user("manager"), make_user("agent")
        lead = crm_services.capture_lead(
            actor=manager,
            contact_data={"first_name": "Sam", "email": "sam-notify@example.test"},
            lead_type=Lead.LeadType.BUY,
            title="Villa",
            route=False,
            acknowledge=False,
        )
        crm_services.assign_lead(lead, to_user=agent, actor=manager, reason="Yours.")
        note = Notification.objects.get(recipient=agent)
        assert note.type == "LEAD_ASSIGNED"
        assert str(lead.pk) == str(note.entity_id)

    def test_a_stage_move_with_a_next_action_creates_the_task(
        self, db, make_user, make_contact, pipeline
    ):
        """SRS 3.4.5 — the mandatory next action becomes a real task on the owner's list,
        not a string that scrolls away in the stage history."""
        from apps.collaboration.models import Activity
        from apps.crm import services as crm_services

        agent = make_user("agent")
        deal = crm_services.create_deal(
            actor=agent, pipeline=pipeline, primary_contact=make_contact(),
            owner=agent, title="Villa", deal_type="SALE",
            estimated_value=1_000_000, currency="AED",
        )
        stages = {s.code: s for s in pipeline.stages.all()}
        crm_services.move_stage(
            deal, stages["VIEWING"], actor=agent,
            reason="Booked.", next_action="Send the offer letter",
        )
        task = Activity.objects.get(activity_type=Activity.ActivityType.TASK)
        assert task.subject == "Send the offer letter"
        assert task.assigned_to_id == agent.pk
        assert task.deal_id == deal.pk

    def test_a_terminal_move_creates_no_task(self, db, make_user, make_contact, pipeline):
        from apps.collaboration.models import Activity
        from apps.crm import services as crm_services

        agent = make_user("agent")
        deal = crm_services.create_deal(
            actor=agent, pipeline=pipeline, primary_contact=make_contact(),
            owner=agent, title="Villa", deal_type="SALE",
            estimated_value=1_000_000, currency="AED",
        )
        stages = {s.code: s for s in pipeline.stages.all()}
        crm_services.move_stage(deal, stages["WON"], actor=agent, reason="Signed.")
        assert not Activity.objects.filter(
            activity_type=Activity.ActivityType.TASK
        ).exists()
