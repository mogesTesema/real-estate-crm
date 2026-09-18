"""Viewings and their calendar entry (SRS 3.3.11), plus GPS field tracking (SRS 3.16.5–3.16.7).

Two requirements that are only half met if you stop at the obvious part.

architecture.md §1.2 lists the viewing → activity upsert as a *required* orchestration and §13
forbids a standalone VIEWING activity — so a viewing with no calendar entry, or with two, is a
diary that disagrees with the CRM.

SRS 3.16.7 says GPS tracks are "role-restricted **and** audited". Row scoping is the
restriction. Without the audit row, a manager can read an employee's whole day and leave no
trace, which is the half most easily forgotten.
"""
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.collaboration.models import Activity
from apps.crm import services
from apps.crm.models import AgentFieldSession, Viewing


@pytest.fixture
def agent_user(make_user):
    return make_user("agent")


@pytest.fixture
def book(agent_user, make_property, make_contact):
    def _book(actor=None, agent=None, **kwargs):
        start = kwargs.pop("scheduled_start", timezone.now() + timezone.timedelta(days=1))
        end = kwargs.pop("scheduled_end", start + timezone.timedelta(hours=1))
        return services.schedule_viewing(
            actor=actor or agent_user,
            property=kwargs.pop("property", None) or make_property(),
            contact=kwargs.pop("contact", None) or make_contact(),
            agent=agent or agent_user,
            scheduled_start=start,
            scheduled_end=end,
            **kwargs,
        )

    return _book


# --- the calendar orchestration -------------------------------------------------------------


class TestViewingActivity:
    def test_scheduling_creates_exactly_one_activity(self, db, book):
        viewing = book()
        activity = Activity.objects.get(
            source_type=Activity.SourceType.VIEWING, source_id=viewing.pk
        )
        assert activity.activity_type == Activity.ActivityType.VIEWING
        assert activity.assigned_to_id == viewing.agent_id
        assert activity.start_at == viewing.scheduled_start

    def test_rescheduling_updates_rather_than_duplicating(self, db, book, agent_user):
        """The whole reason this goes through one upsert function."""
        viewing = book()
        new_start = viewing.scheduled_start + timezone.timedelta(days=2)
        services.reschedule_viewing(
            viewing,
            actor=agent_user,
            scheduled_start=new_start,
            scheduled_end=new_start + timezone.timedelta(hours=1),
        )
        activities = Activity.objects.filter(
            source_type=Activity.SourceType.VIEWING, source_id=viewing.pk
        )
        assert activities.count() == 1
        assert activities.get().start_at == new_start

    def test_completing_closes_the_calendar_entry(self, db, book, agent_user):
        viewing = book()
        services.complete_viewing(viewing, actor=agent_user, feedback="Liked it.", rating=4)
        activity = Activity.objects.get(source_id=viewing.pk)
        assert activity.status == Activity.Status.COMPLETED
        assert activity.completed_at is not None

    def test_cancelling_cancels_the_calendar_entry(self, db, book, agent_user):
        viewing = book()
        services.complete_viewing(viewing, actor=agent_user, status=Viewing.Status.CANCELLED)
        assert Activity.objects.get(source_id=viewing.pk).status == Activity.Status.CANCELLED

    def test_the_database_allows_only_one_activity_per_source(self, db, book, agent_user):
        """The partial unique index is what makes the upsert an upsert rather than a race that
        inserts duplicate diary rows for one viewing."""
        from django.db import IntegrityError, transaction

        viewing = book()
        with pytest.raises(IntegrityError), transaction.atomic():
            Activity.objects.create(
                activity_type=Activity.ActivityType.VIEWING,
                subject="Duplicate",
                assigned_to=agent_user,
                created_by=agent_user,
                source_type=Activity.SourceType.VIEWING,
                source_id=viewing.pk,
            )

    def test_an_unknown_source_type_is_refused(self, db, agent_user):
        from apps.collaboration import services as collaboration_services

        with pytest.raises(ValidationError):
            collaboration_services.upsert_activity_for_source(
                source_type="INVOICE",
                source_id=agent_user.pk,
                activity_type=Activity.ActivityType.TASK,
                subject="Nope",
                assigned_to=agent_user,
                actor=agent_user,
            )


# --- feedback and lifecycle ------------------------------------------------------------------


class TestViewingLifecycle:
    def test_feedback_and_rating_are_captured_on_completion(self, db, book, agent_user):
        """SRS 3.3.11 — captured on the call that closes the appointment rather than left to a
        separate step nobody takes."""
        viewing = book()
        services.complete_viewing(
            viewing, actor=agent_user, feedback="Too small.", rating=2
        )
        viewing.refresh_from_db()
        assert viewing.status == Viewing.Status.COMPLETED
        assert viewing.feedback == "Too small."
        assert viewing.rating == 2

    def test_a_rating_outside_one_to_five_is_refused(self, db, book, agent_user):
        with pytest.raises(ValidationError):
            services.complete_viewing(book(), actor=agent_user, rating=9)

    def test_a_closed_viewing_cannot_be_rescheduled(self, db, book, agent_user):
        viewing = book()
        services.complete_viewing(viewing, actor=agent_user)
        start = timezone.now() + timezone.timedelta(days=5)
        with pytest.raises(ValidationError):
            services.reschedule_viewing(
                viewing, actor=agent_user, scheduled_start=start,
                scheduled_end=start + timezone.timedelta(hours=1),
            )

    def test_a_closed_viewing_cannot_be_completed_again(self, db, book, agent_user):
        viewing = book()
        services.complete_viewing(viewing, actor=agent_user)
        with pytest.raises(ValidationError, match="already closed"):
            services.complete_viewing(viewing, actor=agent_user)

    def test_an_appointment_must_end_after_it_starts(self, db, book):
        start = timezone.now()
        with pytest.raises(ValidationError):
            book(scheduled_start=start, scheduled_end=start - timezone.timedelta(hours=1))

    def test_only_the_running_agent_can_check_in(self, db, book, make_user):
        viewing = book()
        with pytest.raises(ValidationError):
            services.check_in_to_viewing(viewing, actor=make_user("agent"))

    def test_check_in_stamps_the_time_and_coordinates(self, db, book, agent_user):
        viewing = services.check_in_to_viewing(
            book(), actor=agent_user, latitude=25.08, longitude=55.14
        )
        assert viewing.check_in_at is not None
        assert float(viewing.check_in_latitude) == 25.08


# --- GPS field tracking (SRS 3.16.5–3.16.7) ----------------------------------------------------


class TestFieldSessions:
    def test_a_session_without_gps_is_refused(self, db, agent_user):
        """SRS 3.16.5's whole force. A session that opens without GPS is an untracked visit
        wearing a tracked visit's name — worse than no session, because the record implies a
        trail that does not exist."""
        with pytest.raises(ValidationError, match="Enable GPS"):
            services.start_field_session(
                actor=agent_user,
                session_type=AgentFieldSession.SessionType.VIEWING,
                gps_enabled=False,
            )

    def test_a_session_with_gps_opens(self, db, agent_user):
        session = services.start_field_session(
            actor=agent_user,
            session_type=AgentFieldSession.SessionType.VIEWING,
            gps_enabled=True,
        )
        assert session.status == AgentFieldSession.Status.ACTIVE

    def test_a_session_where_gps_is_not_required_may_open_without_it(self, db, agent_user):
        """`gps_required=False` is a deliberate, recorded exception — office work logged as a
        session — not a way around the rule, since the flag is on the row for anyone auditing."""
        session = services.start_field_session(
            actor=agent_user,
            session_type=AgentFieldSession.SessionType.OTHER_FIELD,
            gps_required=False,
            gps_enabled=False,
        )
        assert session.gps_enabled is False

    def test_an_agent_cannot_open_a_session_for_someone_else(self, db, agent_user, make_user):
        """Supervisors read trails; they do not manufacture them."""
        with pytest.raises(ValidationError):
            services.start_field_session(
                actor=agent_user,
                agent=make_user("agent"),
                session_type=AgentFieldSession.SessionType.VIEWING,
                gps_enabled=True,
            )

    def test_two_open_sessions_at_once_are_refused(self, db, agent_user):
        services.start_field_session(
            actor=agent_user, session_type=AgentFieldSession.SessionType.VIEWING,
            gps_enabled=True,
        )
        with pytest.raises(ValidationError, match="already has an open"):
            services.start_field_session(
                actor=agent_user, session_type=AgentFieldSession.SessionType.SALE_TRIP,
                gps_enabled=True,
            )

    @pytest.fixture
    def session(self, db, agent_user):
        return services.start_field_session(
            actor=agent_user,
            session_type=AgentFieldSession.SessionType.VIEWING,
            gps_enabled=True,
        )

    def test_points_are_appended_with_a_geography(self, session, agent_user):
        services.record_location_points(
            session,
            actor=agent_user,
            points=[
                {"latitude": 25.0800, "longitude": 55.1400},
                {"latitude": 25.0810, "longitude": 55.1410},
            ],
        )
        points = list(session.location_points.order_by("recorded_at"))
        assert len(points) == 2
        assert points[0].geo_point is not None

    def test_a_recorded_point_cannot_be_updated(self, session, agent_user):
        """Append-only at the database-role level — a guarantee about the *employee's* record
        as much as the company's."""
        from django.db import InternalError, ProgrammingError, transaction

        services.record_location_points(
            session, actor=agent_user, points=[{"latitude": 25.08, "longitude": 55.14}]
        )
        point = session.location_points.get()
        with pytest.raises((ProgrammingError, InternalError)), transaction.atomic():
            session.location_points.filter(pk=point.pk).update(latitude=0)

    def test_a_recorded_point_cannot_be_deleted(self, session, agent_user):
        from django.db import InternalError, ProgrammingError, transaction

        services.record_location_points(
            session, actor=agent_user, points=[{"latitude": 25.08, "longitude": 55.14}]
        )
        with pytest.raises((ProgrammingError, InternalError)), transaction.atomic():
            session.location_points.all().delete()

    def test_only_the_tracked_agent_can_append(self, session, make_user):
        """A supervisor writing points into someone else's trail would make the trail evidence
        of nothing."""
        with pytest.raises(ValidationError):
            services.record_location_points(
                session,
                actor=make_user("manager"),
                points=[{"latitude": 25.08, "longitude": 55.14}],
            )

    def test_a_closed_session_takes_no_more_points(self, session, agent_user):
        services.end_field_session(session, actor=agent_user)
        with pytest.raises(ValidationError, match="closed"):
            services.record_location_points(
                session, actor=agent_user, points=[{"latitude": 25.08, "longitude": 55.14}]
            )

    def test_a_supervisory_read_is_audited(self, session, agent_user, make_user):
        """SRS 3.16.7 — "role-restricted **and** audited". Without this the requirement is
        half met: a manager reads an employee's whole day and leaves no trace."""
        from apps.platform.models import AuditEvent

        services.record_location_points(
            session, actor=agent_user, points=[{"latitude": 25.08, "longitude": 55.14}]
        )
        manager = make_user("manager")
        services.read_location_trail(session, actor=manager)

        event = AuditEvent.objects.get(
            entity_type="AGENT_FIELD_SESSION", action=AuditEvent.Action.VIEW
        )
        assert event.actor_user_id == manager.pk
        assert event.new_values["points_disclosed"] == 1
        assert event.new_values["agent_id"] == str(agent_user.pk)

    def test_an_agent_reading_their_own_trail_is_not_audited(self, session, agent_user):
        """The control exists to make *supervisory* access visible. Logging self-reads buries
        the ones that matter."""
        from apps.platform.models import AuditEvent

        services.read_location_trail(session, actor=agent_user)
        assert not AuditEvent.objects.filter(entity_type="AGENT_FIELD_SESSION").exists()


# --- the endpoints ------------------------------------------------------------------------------


@pytest.mark.django_db
class TestViewingApi:
    URL = "/api/v1/viewings/"

    def test_booking_over_http_creates_the_calendar_entry(
        self, auth_client, agent_user, make_property, make_contact, make_user
    ):
        pm = make_user("property_manager")
        prop = make_property(managed_by=pm)
        start = timezone.now() + timezone.timedelta(days=1)
        response = auth_client(pm).post(
            self.URL,
            {
                "property": str(prop.id),
                "contact": str(make_contact().id),
                "scheduled_start": start.isoformat(),
                "scheduled_end": (start + timezone.timedelta(hours=1)).isoformat(),
            },
            format="json",
        )
        assert response.status_code == 201, response.data
        assert Activity.objects.filter(source_id=response.data["id"]).exists()

    def test_a_property_outside_scope_is_refused(
        self, auth_client, agent_user, make_property, make_contact
    ):
        prop = make_property()
        start = timezone.now() + timezone.timedelta(days=1)
        response = auth_client(agent_user).post(
            self.URL,
            {
                "property": str(prop.id),
                "contact": str(make_contact().id),
                "scheduled_start": start.isoformat(),
                "scheduled_end": (start + timezone.timedelta(hours=1)).isoformat(),
            },
            format="json",
        )
        assert response.status_code == 404


@pytest.mark.django_db
class TestFieldSessionApi:
    URL = "/api/v1/field-sessions/"

    def test_starting_without_gps_is_a_400(self, auth_client, agent_user):
        response = auth_client(agent_user).post(
            self.URL, {"session_type": "VIEWING", "gps_enabled": False}, format="json"
        )
        assert response.status_code == 400
        assert "GPS" in str(response.data)

    def test_the_full_trail_round_trip(self, auth_client, agent_user):
        started = auth_client(agent_user).post(
            self.URL, {"session_type": "VIEWING", "gps_enabled": True}, format="json"
        )
        assert started.status_code == 201, started.data
        session_id = started.data["id"]

        appended = auth_client(agent_user).post(
            f"{self.URL}{session_id}/points/",
            {"points": [{"latitude": "25.080000", "longitude": "55.140000"}]},
            format="json",
        )
        assert appended.status_code == 201, appended.data

        trail = auth_client(agent_user).get(f"{self.URL}{session_id}/points/")
        assert len(trail.data) == 1

        ended = auth_client(agent_user).post(f"{self.URL}{session_id}/end/", {}, format="json")
        assert ended.data["status"] == "COMPLETED"

    def test_a_manager_read_is_audited_and_an_outsider_sees_nothing(
        self, auth_client, agent_user, make_user, other_branch
    ):
        from apps.identity.models import Team
        from apps.platform.models import AuditEvent

        session = services.start_field_session(
            actor=agent_user,
            session_type=AgentFieldSession.SessionType.VIEWING,
            gps_enabled=True,
        )
        services.record_location_points(
            session, actor=agent_user, points=[{"latitude": 25.08, "longitude": 55.14}]
        )

        manager = make_user("manager")
        assert auth_client(manager).get(f"{self.URL}{session.id}/points/").status_code == 200
        assert AuditEvent.objects.filter(entity_type="AGENT_FIELD_SESSION").count() == 1

        far_team = Team.objects.create(branch=other_branch, name="Far", code="FARG")
        outsider = make_user("manager", branch=other_branch, team=far_team)
        assert auth_client(outsider).get(f"{self.URL}{session.id}/points/").status_code == 404
