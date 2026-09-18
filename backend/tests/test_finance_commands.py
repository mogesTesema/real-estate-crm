"""The no-Celery recurring commands (§1.3): every one run twice back-to-back changes
nothing the second time — the scheduler retries, overlaps, and double-fires, and the
business row must be the idempotency key."""
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.finance.models import Invoice
from apps.property_ops import services as po_services
from apps.property_ops.models import Lease, RentSchedule


@pytest.fixture
def pm(make_user):
    return make_user("property_manager")


@pytest.fixture
def active_lease(pm, make_property, make_contact):
    def _make(**kwargs):
        kwargs.setdefault("property", make_property(managed_by=pm))
        kwargs.setdefault("tenant", make_contact())
        kwargs.setdefault("landlord", make_contact())
        kwargs.setdefault("property_manager", pm)
        kwargs.setdefault("lease_type", Lease.LeaseType.RESIDENTIAL)
        kwargs.setdefault("start_date", "2026-01-01")
        kwargs.setdefault("end_date", "2026-12-31")
        kwargs.setdefault("rent_amount", Decimal("1000"))
        kwargs.setdefault("billing_frequency", Lease.BillingFrequency.MONTHLY)
        kwargs.setdefault("security_deposit", Decimal("0"))
        lease = po_services.create_lease(actor=pm, **kwargs)
        po_services.activate_lease(lease, actor=pm)
        return lease

    return _make


class TestGenerateRentInvoices:
    def test_run_twice_invoices_once(self, db, active_lease):
        lease = active_lease()
        before = Invoice.objects.filter(lease=lease).count()
        call_command("generate_rent_invoices", "--quiet")
        first = Invoice.objects.filter(lease=lease).count()
        call_command("generate_rent_invoices", "--quiet")
        assert Invoice.objects.filter(lease=lease).count() == first
        assert first >= before

    def test_a_terminated_lease_is_not_invoiced(self, db, active_lease, pm):
        lease = active_lease()
        po_services.terminate_lease(lease, actor=pm)
        before = Invoice.objects.filter(lease=lease).count()
        call_command("generate_rent_invoices", "--quiet")
        assert Invoice.objects.filter(lease=lease).count() == before


class TestSweepOverdue:
    def test_late_fee_is_one_shot(self, db, active_lease):
        """A fee that compounds on every sweep run is a bug, not a policy."""
        from apps.identity.models import Company

        company = Company.objects.first()
        company.settings = {"late_fee": {"grace_days": 0, "mode": "FIXED", "value": "100"}}
        company.save(update_fields=["settings"])

        active_lease()
        call_command("sweep_overdue", "--quiet")
        fees = list(
            RentSchedule.objects.filter(status=RentSchedule.Status.OVERDUE)
            .values_list("late_fee_amount", flat=True)
        )
        assert fees and all(fee == Decimal("100") for fee in fees)
        call_command("sweep_overdue", "--quiet")
        fees_after = list(
            RentSchedule.objects.filter(status=RentSchedule.Status.OVERDUE)
            .values_list("late_fee_amount", flat=True)
        )
        assert fees_after == fees  # not 200 — one shot

    def test_overdue_invoices_flip_status(self, db, active_lease):
        active_lease()
        call_command("sweep_overdue", "--quiet")
        assert Invoice.objects.filter(status=Invoice.Status.OVERDUE).exists()


class TestSweepLeaseExpiry:
    def test_expiring_is_flagged_once_with_one_alert(self, db, active_lease, pm):
        """SRS 3.5.5 — "alert on upcoming lease expirations". The status is the idempotency
        key, so the alert cannot repeat on the next run."""
        from apps.collaboration.models import Notification

        today = timezone.localdate()
        lease = active_lease(
            start_date=str(today - timezone.timedelta(days=300)),
            end_date=str(today + timezone.timedelta(days=30)),
        )
        call_command("sweep_lease_expiry", "--quiet")
        lease.refresh_from_db()
        assert lease.status == Lease.Status.EXPIRING
        assert Notification.objects.filter(type="LEASE_EXPIRING", recipient=pm).count() == 1

        call_command("sweep_lease_expiry", "--quiet")
        assert Notification.objects.filter(type="LEASE_EXPIRING", recipient=pm).count() == 1

    def test_a_lease_past_its_end_expires(self, db, active_lease):
        today = timezone.localdate()
        lease = active_lease(
            start_date=str(today - timezone.timedelta(days=400)),
            end_date=str(today - timezone.timedelta(days=5)),
        )
        call_command("sweep_lease_expiry", "--quiet")
        lease.refresh_from_db()
        assert lease.status == Lease.Status.EXPIRED

    def test_the_renewal_follow_up_task_lands_once(self, db, active_lease, pm):
        from apps.collaboration.models import Activity

        today = timezone.localdate()
        active_lease(
            start_date=str(today - timezone.timedelta(days=300)),
            end_date=str(today + timezone.timedelta(days=30)),
        )
        call_command("sweep_lease_expiry", "--quiet")
        call_command("sweep_lease_expiry", "--quiet")
        assert Activity.objects.filter(
            subject__startswith="Lease renewal follow-up"
        ).count() == 1
