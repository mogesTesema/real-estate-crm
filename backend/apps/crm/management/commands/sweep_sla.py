"""Mark leads that have blown their follow-up SLA (SRS 3.1.9).

A management command rather than a Celery beat task, deliberately: the deployment target is
Render's free tier, which blocks background workers. An external scheduler — Render Cron, a
GitHub Action, cron on any box with the CLI — calls this. The work is idempotent, so a
scheduler that retries, or two that overlap, costs nothing.

    python manage.py sweep_sla
"""
from django.core.management.base import BaseCommand

from apps.crm.services import sweep_sla


class Command(BaseCommand):
    help = "Flag leads past their follow-up SLA with no first response (SRS 3.1.9)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Print nothing when no lead has breached. Useful under cron.",
        )

    def handle(self, *args, **options):
        breached = sweep_sla()
        if breached:
            self.stdout.write(
                self.style.WARNING(f"{len(breached)} lead(s) breached their follow-up SLA.")
            )
        elif not options["quiet"]:
            self.stdout.write(self.style.SUCCESS("No leads past SLA."))
