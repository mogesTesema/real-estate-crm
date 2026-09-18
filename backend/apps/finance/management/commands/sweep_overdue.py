"""Mark overdue money and apply one-shot late fees (SRS 3.6.7, 3.6.8).

Idempotent: status transitions select only not-yet-overdue rows, and the late fee applies
only while `late_fee_amount == 0` — a fee that compounds on every run is a bug, not a
policy.

    python manage.py sweep_overdue
"""
from django.core.management.base import BaseCommand

from apps.finance.services import sweep_overdue


class Command(BaseCommand):
    help = "Flag overdue invoices, rent periods (with one-shot late fee) and milestones."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        counts = sweep_overdue()
        total = sum(counts.values())
        if total:
            self.stdout.write(self.style.WARNING(str(counts)))
        elif not options["quiet"]:
            self.stdout.write("Nothing overdue.")
