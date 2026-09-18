"""Lease expiry lifecycle (SRS 3.5.5): flag EXPIRING inside the notice window (with the
alert the requirement asks for), close out EXPIRED leases past their end date, and expire
stale renewal proposals.

Idempotency is the status itself: an EXPIRING lease is not re-selected, so the notification
fires exactly once per lease.

    python manage.py sweep_lease_expiry
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.collaboration import services as collaboration_services
from apps.property_ops import services
from apps.property_ops.models import Lease, Renewal


class Command(BaseCommand):
    help = "Flag expiring leases (with alerts), expire ended ones and stale renewals."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        today = timezone.localdate()
        flagged = expired = stale = 0

        default_notice = int(
            services._company_notice_days()  # noqa: SLF001 - same app
        )
        for lease in Lease.objects.filter(status=Lease.Status.ACTIVE).select_related(
            "property_manager", "tenant"
        ):
            notice = lease.notice_period_days or default_notice
            if lease.end_date <= today + timedelta(days=notice):
                services.change_lease_status(
                    lease, Lease.Status.EXPIRING, actor=None,
                    reason=f"Within the {notice}-day notice window.",
                )
                collaboration_services.notify(
                    recipient=lease.property_manager,
                    type="LEASE_EXPIRING",
                    title=f"Lease expiring: {lease.reference_code}",
                    body=f"Ends {lease.end_date}. Propose a renewal or plan the move-out.",
                    entity_type="LEASE",
                    entity_id=lease.pk,
                )
                collaboration_services.create_task_for_lease_expiry(lease)
                flagged += 1

        for lease in Lease.objects.filter(
            status__in=(Lease.Status.ACTIVE, Lease.Status.EXPIRING, Lease.Status.RENEWED),
            end_date__lt=today,
        ):
            services.change_lease_status(
                lease, Lease.Status.EXPIRED, actor=None, reason="End date passed."
            )
            expired += 1

        stale = Renewal.objects.filter(
            status__in=(Renewal.Status.PROPOSED, Renewal.Status.NEGOTIATING),
            proposed_start_date__lt=today,
        ).update(status=Renewal.Status.EXPIRED)

        if flagged or expired or stale:
            self.stdout.write(
                self.style.WARNING(
                    f"expiring={flagged} expired={expired} stale_renewals={stale}"
                )
            )
        elif not options["quiet"]:
            self.stdout.write("Nothing to do.")
