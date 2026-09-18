"""Maintenance (SRS §3.14): the portal-create carve-out, the work-order → expense hand-off,
and applications/renewals/inspections from phase C."""
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.property_ops import services
from apps.property_ops.models import (
    Application,
    Inspection,
    Lease,
    MaintenanceRequest,
    Renewal,
    WorkOrder,
)


@pytest.fixture
def pm(make_user):
    return make_user("property_manager")


@pytest.fixture
def tenant_world(pm, make_property, make_contact, make_user):
    from apps.identity.models import PortalProfile, Role, User, UserRole

    tenant = make_contact()
    prop = make_property(managed_by=pm)
    lease = services.create_lease(
        actor=pm, property=prop, tenant=tenant, landlord=make_contact(),
        property_manager=pm, lease_type=Lease.LeaseType.RESIDENTIAL,
        start_date="2026-01-01", end_date="2026-12-31",
        rent_amount=Decimal("1000"), billing_frequency=Lease.BillingFrequency.MONTHLY,
        security_deposit=Decimal("0"),
    )
    services.activate_lease(lease, actor=pm)
    user = User.objects.create_user(
        email=tenant.email, password="portal-pass-9911", first_name="T", last_name="P",
    )
    UserRole.objects.create(user=user, role=Role.objects.get(code="portal"))
    PortalProfile.objects.create(
        user=user, contact=tenant, portal_type=PortalProfile.PortalType.TENANT,
        eligibility_status=PortalProfile.EligibilityStatus.ACTIVE,
    )
    return {"pm": pm, "tenant": tenant, "prop": prop, "lease": lease, "user": user}


class TestPortalMaintenanceCreate:
    def test_a_tenant_reports_against_their_own_tenancy(self, db, tenant_world):
        """SRS 3.14.1 — and the service FORCES the reporter identity; a portal client cannot
        report as anyone else."""
        request = services.create_maintenance_request(
            actor=tenant_world["user"], property=tenant_world["prop"],
            title="AC broken", description="No cold air in the bedroom.",
        )
        assert request.reported_by_contact_id == tenant_world["tenant"].pk
        assert request.reported_by_user is None
        assert request.lease_id == tenant_world["lease"].pk  # resolved automatically

    def test_a_tenant_cannot_report_against_a_strangers_property(
        self, db, tenant_world, make_property
    ):
        with pytest.raises(ValidationError, match="own tenancy"):
            services.create_maintenance_request(
                actor=tenant_world["user"], property=make_property(),
                title="Not mine", description="…",
            )

    def test_the_pm_is_notified_on_creation(self, db, tenant_world):
        from apps.collaboration.models import Notification

        services.create_maintenance_request(
            actor=tenant_world["user"], property=tenant_world["prop"],
            title="Leak", description="Kitchen sink.",
        )
        assert Notification.objects.filter(
            recipient=tenant_world["pm"], type="MAINTENANCE_UPDATE"
        ).exists()

    def test_every_status_move_notifies_the_reporter(self, db, tenant_world, make_contact):
        """The tenant's visibility into "is anyone doing anything?" IS SRS 3.14.2."""
        from apps.collaboration.models import Notification

        request = services.create_maintenance_request(
            actor=tenant_world["user"], property=tenant_world["prop"],
            title="Leak", description="Kitchen sink.",
        )
        vendor = services.create_vendor(
            actor=tenant_world["pm"], contact=make_contact(), service_category="Plumbing"
        )
        services.assign_maintenance(request, actor=tenant_world["pm"], vendor=vendor)
        services.change_maintenance_status(
            request, MaintenanceRequest.Status.IN_PROGRESS, actor=tenant_world["pm"]
        )
        assert Notification.objects.filter(
            recipient=tenant_world["user"], type="MAINTENANCE_UPDATE"
        ).count() >= 1


class TestWorkOrderMoneyHandOff:
    def test_completion_records_the_expense_and_rolls_up_cost(
        self, db, tenant_world, make_contact
    ):
        """§1.2: "Billable maintenance: property_ops → finance.services.record_expense →
        owner statement". property_ops never writes a finance row itself."""
        from apps.finance.models import Expense

        pm = tenant_world["pm"]
        request = services.create_maintenance_request(
            actor=pm, property=tenant_world["prop"], lease=tenant_world["lease"],
            title="Boiler", description="Replace element.",
        )
        vendor = services.create_vendor(
            actor=pm, contact=make_contact(), service_category="HVAC"
        )
        services.assign_maintenance(request, actor=pm, vendor=vendor)
        work_order = services.create_work_order(
            actor=pm, maintenance_request=request, vendor=vendor
        )
        services.quote_work_order(work_order, actor=pm, quoted_amount="750")
        services.approve_work_order(work_order, actor=pm)
        services.start_work_order(work_order, actor=pm)
        work_order, expense = services.complete_work_order(
            work_order, actor=pm, final_amount="800", bill_to_owner=True
        )
        assert expense is not None
        assert expense.category == "MAINTENANCE"
        assert expense.is_billable_to_owner is True
        assert expense.status == Expense.Status.APPROVED
        assert expense.maintenance_request_id == request.pk
        assert expense.vendor_contact_id == vendor.contact_id

        services.change_maintenance_status(
            request, MaintenanceRequest.Status.IN_PROGRESS, actor=pm
        )
        services.change_maintenance_status(
            request, MaintenanceRequest.Status.COMPLETED, actor=pm
        )
        request.refresh_from_db()
        assert request.actual_cost == Decimal("800")

    def test_a_zero_cost_completion_records_no_expense(self, db, tenant_world, make_contact):
        pm = tenant_world["pm"]
        request = services.create_maintenance_request(
            actor=pm, property=tenant_world["prop"], title="Check", description="…",
        )
        vendor = services.create_vendor(
            actor=pm, contact=make_contact(), service_category="General"
        )
        work_order = services.create_work_order(
            actor=pm, maintenance_request=request, vendor=vendor
        )
        services.quote_work_order(work_order, actor=pm, quoted_amount="1")
        services.approve_work_order(work_order, actor=pm)
        services.start_work_order(work_order, actor=pm)
        _, expense = services.complete_work_order(work_order, actor=pm, final_amount="0")
        assert expense is None

    def test_the_transition_table_holds(self, db, tenant_world, make_contact):
        pm = tenant_world["pm"]
        request = services.create_maintenance_request(
            actor=pm, property=tenant_world["prop"], title="X", description="…",
        )
        vendor = services.create_vendor(
            actor=pm, contact=make_contact(), service_category="General"
        )
        work_order = services.create_work_order(
            actor=pm, maintenance_request=request, vendor=vendor
        )
        with pytest.raises(ValidationError):
            services.complete_work_order(work_order, actor=pm, final_amount="10")


class TestApplications:
    def test_the_walk_to_a_lease(self, db, pm, make_property, make_contact):
        from apps.inventory.models import PropertyOwner

        prop = make_property(managed_by=pm)
        PropertyOwner.objects.create(
            property=prop, contact=make_contact(), ownership_percentage=100,
            is_primary_owner=True, start_date="2026-01-01",
        )
        application = services.submit_application(
            actor=pm, property=prop, applicant_contact=make_contact(),
            proposed_rent=Decimal("9000"), desired_move_in="2027-02-01",
        )
        services.update_screening(
            application, actor=pm,
            background_check_status=Application.CheckStatus.PASSED,
            credit_check_status=Application.CheckStatus.PASSED,
            screening_result={"score": 720},
        )
        services.decide_application(application, Application.Status.UNDER_REVIEW, actor=pm)
        services.decide_application(application, Application.Status.APPROVED, actor=pm)
        lease = services.convert_application(
            application, actor=pm, end_date="2028-01-31",
        )
        application.refresh_from_db()
        assert application.status == Application.Status.CONVERTED
        assert application.screening_result["lease_id"] == str(lease.pk)
        assert application.screening_result["score"] == 720  # merged, not replaced
        assert lease.status == Lease.Status.DRAFT
        assert lease.rent_amount == Decimal("9000")

    def test_a_rejected_application_never_converts(self, db, pm, make_property, make_contact):
        application = services.submit_application(
            actor=pm, property=make_property(managed_by=pm),
            applicant_contact=make_contact(),
        )
        services.decide_application(application, Application.Status.REJECTED, actor=pm)
        with pytest.raises(ValidationError):
            services.convert_application(application, actor=pm, end_date="2028-01-01")


class TestRenewals:
    @pytest.fixture
    def active_lease(self, pm, make_property, make_contact):
        lease = services.create_lease(
            actor=pm, property=make_property(managed_by=pm), tenant=make_contact(),
            landlord=make_contact(), property_manager=pm,
            lease_type=Lease.LeaseType.RESIDENTIAL,
            start_date="2026-01-01", end_date="2026-12-31",
            rent_amount=Decimal("10000"), billing_frequency=Lease.BillingFrequency.MONTHLY,
            security_deposit=Decimal("0"),
        )
        return services.activate_lease(lease, actor=pm)

    def test_percentage_escalation_math(self, db, pm, active_lease):
        renewal = services.propose_renewal(
            active_lease, actor=pm,
            escalation_type=Renewal.EscalationType.PERCENTAGE, escalation_value=5,
            send_notice=False,
        )
        assert renewal.proposed_rent == Decimal("10500.00")
        assert str(renewal.proposed_start_date) == "2027-01-01"
        assert str(renewal.proposed_end_date) == "2027-12-31"

    def test_accept_builds_the_successor_without_tripping_the_gist(
        self, db, pm, active_lease
    ):
        """Back-to-back dates with inclusive-[] ranges — the boundary that bites."""
        renewal = services.propose_renewal(
            active_lease, actor=pm,
            escalation_type=Renewal.EscalationType.FIXED_AMOUNT, escalation_value=500,
            send_notice=False,
        )
        successor = services.accept_renewal(renewal, actor=pm)
        active_lease.refresh_from_db()
        renewal.refresh_from_db()
        assert successor.status == Lease.Status.PENDING_SIGNATURE
        assert successor.rent_amount == Decimal("10500")
        assert renewal.new_lease_id == successor.pk
        assert active_lease.status == Lease.Status.RENEWED

    def test_a_renewal_starting_inside_the_original_is_refused(self, db, pm, active_lease):
        renewal = services.propose_renewal(
            active_lease, actor=pm, proposed_start_date="2026-12-31",
            proposed_end_date="2027-12-30", send_notice=False,
        )
        with pytest.raises(ValidationError, match="inclusive"):
            services.accept_renewal(renewal, actor=pm)


class TestInspections:
    def test_the_calendar_twin_rule(self, db, pm, make_property, make_contact, make_user):
        """§1.2: inspections upsert exactly one INSPECTION activity, same as viewings."""
        from apps.collaboration.models import Activity

        lease = services.create_lease(
            actor=pm, property=make_property(managed_by=pm), tenant=make_contact(),
            landlord=make_contact(), property_manager=pm,
            lease_type=Lease.LeaseType.RESIDENTIAL,
            start_date="2026-01-01", end_date="2026-12-31",
            rent_amount=Decimal("1000"), billing_frequency=Lease.BillingFrequency.MONTHLY,
            security_deposit=Decimal("0"),
        )
        inspection = services.schedule_inspection(
            actor=pm, lease=lease, inspection_type=Inspection.InspectionType.MOVE_IN,
            scheduled_date=timezone.now(), performed_by=pm,
        )
        activity = Activity.objects.get(
            source_type=Activity.SourceType.INSPECTION, source_id=inspection.pk
        )
        assert activity.activity_type == Activity.ActivityType.INSPECTION

        services.reschedule_inspection(
            inspection, actor=pm, scheduled_date=timezone.now() + timezone.timedelta(days=1)
        )
        assert Activity.objects.filter(source_id=inspection.pk).count() == 1  # upserted

        services.complete_inspection(
            inspection, actor=pm, meter_readings={"electricity": 10432},
        )
        activity.refresh_from_db()
        inspection.refresh_from_db()
        assert activity.status == Activity.Status.COMPLETED
        assert inspection.meter_readings["electricity"] == 10432
