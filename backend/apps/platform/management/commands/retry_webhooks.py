"""Retry failed webhook deliveries (SRS 3.19.2). Backoff 5min×2^n, cap 5 attempts,
superseded-aware — safe to run on any schedule.

    python manage.py retry_webhooks
"""
from django.core.management.base import BaseCommand

from apps.platform.webhooks import retry_webhooks


class Command(BaseCommand):
    help = "Re-deliver failed outbound webhooks whose backoff has elapsed."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        retried = retry_webhooks()
        if retried:
            ok = sum(1 for log in retried if log.status == "SUCCESS")
            self.stdout.write(
                self.style.WARNING(f"Retried {len(retried)}, {ok} delivered.")
            )
        elif not options["quiet"]:
            self.stdout.write("Nothing to retry.")
