"""Send new-listing alerts for saved searches (SRS 3.8.4). `last_run_at` always advances —
at-most-once delivery by design.

    python manage.py run_saved_search_alerts
"""
from django.core.management.base import BaseCommand

from apps.crm.services import run_saved_search_alerts


class Command(BaseCommand):
    help = "Send saved-search alerts whose window has elapsed."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        sent = run_saved_search_alerts()
        if sent:
            self.stdout.write(self.style.SUCCESS(f"Sent {len(sent)} alert(s)."))
        elif not options["quiet"]:
            self.stdout.write("Nothing to send.")
