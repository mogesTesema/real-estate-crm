"""Public reads / scoped querysets for `property_ops` (architecture.md §1.2).

Other apps read `property_ops` rows through this module. They must never import
`apps.property_ops.api` — that package is the HTTP surface and is private to this app.
"""
from datetime import timedelta

from django.utils import timezone

from apps.identity.selectors import apply_scope

from .models import Deposit, Lease, RentSchedule


def live_leases():
    return Lease.objects.select_related(
        "property", "unit", "tenant", "landlord", "property_manager"
    ).prefetch_related("parties")


def visible_leases(user):
    return apply_scope(live_leases(), user, "lease")


def live_rent_schedules():
    return RentSchedule.objects.select_related("lease", "invoice")


def visible_rent_schedules(user):
    return apply_scope(live_rent_schedules(), user, "rent_schedule")


def live_deposits():
    return Deposit.objects.select_related("lease")


def visible_deposits(user):
    return apply_scope(live_deposits(), user, "deposit")


def expiring_leases(*, as_of=None, default_notice_days=90):
    """ACTIVE leases inside their notice window (SRS 3.5.5) — the sweep's and the
    dashboard's shared question."""
    as_of = as_of or timezone.localdate()
    horizon = as_of + timedelta(days=default_notice_days)
    return Lease.objects.filter(status=Lease.Status.ACTIVE, end_date__lte=horizon)
