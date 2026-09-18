"""Expire stale SUBMITTED offers (SRS 3.6.1). Status is the idempotency key.

    python manage.py sweep_offers
"""
from django.core.management.base import BaseCommand

from apps.crm.services import sweep_offers


class Command(BaseCommand):
    help = "Mark expired offers EXPIRED."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        expired = sweep_offers()
        if expired:
            self.stdout.write(self.style.WARNING(f"Expired {len(expired)} offer(s)."))
        elif not options["quiet"]:
            self.stdout.write("Nothing to expire.")
