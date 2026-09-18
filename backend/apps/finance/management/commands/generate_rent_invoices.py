"""Invoice due rent periods (SRS 3.5.3).

A management command an external scheduler calls, not Celery beat — the deployment target
blocks background workers. Idempotent by the business row: a RentSchedule with an invoice is
never selected again, and the selection runs under a row lock, so overlapping runs cannot
double-invoice.

    python manage.py generate_rent_invoices [--horizon-days N]
"""
from django.core.management.base import BaseCommand

from apps.finance.services import generate_rent_schedule_invoices


class Command(BaseCommand):
    help = "Create invoices for rent periods due within the horizon (SRS 3.5.3)."

    def add_arguments(self, parser):
        parser.add_argument("--horizon-days", type=int, default=None)
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        created = generate_rent_schedule_invoices(horizon_days=options["horizon_days"])
        if created:
            self.stdout.write(self.style.SUCCESS(f"Issued {len(created)} rent invoice(s)."))
        elif not options["quiet"]:
            self.stdout.write("No rent periods due.")
