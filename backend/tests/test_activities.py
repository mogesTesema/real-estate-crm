"""Tasks, recurrence and the calendar (SRS §3.12).

Recurrence has no scheduler: completing a recurring task IS the tick that materializes the
next occurrence. The reminder sweep's idempotency stamp is `reminder_sent_at`.
"""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.collaboration import services
from apps.collaboration.models import Activity


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


class TestCreate:
    def test_a_plain_task(self, db, agent_user):
        activity = services.create_activity(
            actor=agent_user, activity_type="TASK", subject="Call the notary",
            due_at=timezone.now() + timedelta(days=1),
        )
        assert activity.status == Activity.Status.OPEN
        assert activity.assigned_to == agent_user  # defaults to the creator

    def test_viewing_and_inspection_types_are_refused(self, db, agent_user):
        """§13's invariant: those rows exist only as mirrors of their domain records."""
        for activity_type in ("VIEWING", "INSPECTION"):
            with pytest.raises(ValidationError, match="domain records"):
                services.create_activity(
                    actor=agent_user, activity_type=activity_type, subject="Sneaky"
                )

    def test_due_before_start_is_refused(self, db, agent_user):
        now = timezone.now()
        with pytest.raises(ValidationError, match="due before it starts"):
            services.create_activity(
                actor=agent_user, activity_type="TASK", subject="Impossible",
                start_at=now, due_at=now - timedelta(hours=1),
            )

    def test_a_bad_rrule_is_refused_at_write_time(self, db, agent_user):
        with pytest.raises(ValidationError, match="RRULE"):
            services.create_activity(
                actor=agent_user, activity_type="TASK", subject="Weekly?",
                start_at=timezone.now(), recurrence_rule="FREQ=FORTNIGHTLYISH",
            )

    def test_a_source_mirror_cannot_be_edited_directly(self, db, agent_user):
        mirror = Activity.objects.create(
            activity_type="VIEWING", subject="Viewing at the marina",
            assigned_to=agent_user, created_by=agent_user,
            source_type="VIEWING", source_id=agent_user.pk,  # any uuid will do
        )
        with pytest.raises(ValidationError, match="Reschedule the viewing"):
            services.update_activity(mirror, actor=agent_user, subject="Hijack")


class TestRecurrence:
    def _weekly_task(self, user, **kwargs):
        # rrule arithmetic is whole-second; a microsecond anchor would shift the clone.
        start = kwargs.pop("start_at", timezone.now().replace(microsecond=0))
        return services.create_activity(
            actor=user, activity_type="TASK", subject="Weekly owner call",
            start_at=start, due_at=start + timedelta(hours=1),
            recurrence_rule="FREQ=WEEKLY", **kwargs,
        )

    def test_completion_materializes_exactly_one_next_occurrence(self, db, agent_user):
        activity = self._weekly_task(agent_user)
        _, next_occurrence = services.change_activity_status(
            activity, "COMPLETED", actor=agent_user
        )
        assert next_occurrence is not None
        assert next_occurrence.start_at == activity.start_at + timedelta(weeks=1)
        assert next_occurrence.due_at == activity.due_at + timedelta(weeks=1)
        assert next_occurrence.status == Activity.Status.OPEN
        assert next_occurrence.recurrence_rule == "FREQ=WEEKLY"
        assert Activity.objects.count() == 2  # original + clone, nothing else

        # Completing the same task again is a no-op — no second clone.
        _, again = services.change_activity_status(
            activity, "COMPLETED", actor=agent_user
        )
        assert again is None
        assert Activity.objects.count() == 2

    def test_cancelling_ends_the_chain(self, db, agent_user):
        activity = self._weekly_task(agent_user)
        _, next_occurrence = services.change_activity_status(
            activity, "CANCELLED", actor=agent_user
        )
        assert next_occurrence is None
        assert Activity.objects.count() == 1

    def test_a_count_limited_rule_stops(self, db, agent_user):
        """COUNT=1 has no date after the anchor — the chain simply ends."""
        activity = services.create_activity(
            actor=agent_user, activity_type="TASK", subject="One-off after all",
            start_at=timezone.now(), recurrence_rule="FREQ=WEEKLY;COUNT=1",
        )
        _, next_occurrence = services.change_activity_status(
            activity, "COMPLETED", actor=agent_user
        )
        assert next_occurrence is None


class TestStatusMachine:
    def test_the_transition_table_is_enforced(self, db, agent_user):
        activity = services.create_activity(
            actor=agent_user, activity_type="TASK", subject="Once only"
        )
        services.change_activity_status(activity, "COMPLETED", actor=agent_user)
        with pytest.raises(ValidationError, match="Cannot move"):
            services.change_activity_status(activity, "IN_PROGRESS", actor=agent_user)

    def test_reopen_via_the_api(self, db, auth_client, agent_user):
        activity = services.create_activity(
            actor=agent_user, activity_type="TASK", subject="Back again",
        )
        client = auth_client(agent_user)
        assert client.post(
            f"/api/v1/activities/{activity.pk}/start/"
        ).data["status"] == "IN_PROGRESS"
        response = client.post(f"/api/v1/activities/{activity.pk}/reopen/")
        assert response.status_code == 200
        assert response.data["status"] == "OPEN"
        # CANCELLED is terminal — the chain does not resurrect.
        client.post(f"/api/v1/activities/{activity.pk}/cancel/")
        assert client.post(
            f"/api/v1/activities/{activity.pk}/reopen/"
        ).status_code == 400


class TestReminderSweep:
    def test_due_reminder_fires_once(self, db, agent_user):
        from django.core.management import call_command

        from apps.collaboration.models import Notification

        services.create_activity(
            actor=agent_user, activity_type="TASK", subject="File the contract",
            start_at=timezone.now() + timedelta(minutes=10),
            reminder_minutes_before=30,  # fire window opened 20 minutes ago
        )
        call_command("sweep_reminders")
        call_command("sweep_reminders")  # run-twice-changes-nothing
        assert Notification.objects.filter(
            recipient=agent_user, type="TASK_DUE"
        ).count() == 1

    def test_a_future_reminder_stays_quiet(self, db, agent_user):
        from django.core.management import call_command

        from apps.collaboration.models import Notification

        services.create_activity(
            actor=agent_user, activity_type="TASK", subject="Not yet",
            start_at=timezone.now() + timedelta(hours=2),
            reminder_minutes_before=15,
        )
        call_command("sweep_reminders")
        assert not Notification.objects.filter(type="TASK_DUE").exists()


class TestCalendar:
    def test_the_window_returns_scoped_activities(self, db, auth_client, agent_user):
        now = timezone.now()
        mine = services.create_activity(
            actor=agent_user, activity_type="TASK", subject="Mine",
            start_at=now + timedelta(days=1),
        )
        start = now.date().isoformat()
        end = (now + timedelta(days=7)).date().isoformat()
        response = auth_client(agent_user).get(
            f"/api/v1/activities/calendar/?start={start}&end={end}"
        )
        assert response.status_code == 200
        assert str(mine.pk) in {row["id"] for row in response.data}

    def test_a_colleagues_diary_is_not_mine(self, db, auth_client, agent_user, make_user):
        """OWN scope: another agent's tasks are invisible — the registry does the work."""
        other = make_user("agent")
        now = timezone.now()
        services.create_activity(
            actor=other, activity_type="TASK", subject="Theirs",
            start_at=now + timedelta(days=1),
        )
        start, end = now.date().isoformat(), (now + timedelta(days=7)).date().isoformat()
        response = auth_client(agent_user).get(
            f"/api/v1/activities/calendar/?start={start}&end={end}"
        )
        assert response.data == []

    def test_an_oversized_window_is_refused(self, db, auth_client, agent_user):
        response = auth_client(agent_user).get(
            "/api/v1/activities/calendar/?start=2026-01-01&end=2026-12-31"
        )
        assert response.status_code == 400

    def test_portal_clients_see_no_activities(self, db, auth_client, portal_tenant):
        user = portal_tenant["user"]
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        client = auth_client(user)
        assert client.get("/api/v1/activities/").data["results"] == []
        assert client.post(
            "/api/v1/activities/", {"activity_type": "TASK", "subject": "Nope"}
        ).status_code == 403
