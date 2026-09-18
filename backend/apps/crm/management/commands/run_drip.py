"""Advance due drip enrollments (SRS 3.8.1). SKIP LOCKED claim on `next_send_at` — safe to
run overlapping. External scheduler, no Celery.

    python manage.py run_drip
"""
from django.core.management.base import BaseCommand

from apps.crm.services import run_drip


class Command(BaseCommand):
    help = "Send due drip-campaign steps."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        processed = run_drip()
        if processed:
            self.stdout.write(self.style.SUCCESS(f"Processed {len(processed)}: {processed}"))
        elif not options["quiet"]:
            self.stdout.write("Nothing due.")
