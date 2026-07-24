"""
Run the lead SLA-breach sweep once (SRS §3.1.9).

On the single-service staging deploy there is no Celery Beat, so this command is invoked
by an external scheduler (e.g. a cron-job.org ping or `render` cron) instead.
    python manage.py sweep_sla
"""
from django.core.management.base import BaseCommand

from apps.leads.tasks import sweep_sla_breaches


class Command(BaseCommand):
    help = "Flag leads that breached their follow-up SLA."

    def handle(self, *args, **options):
        flagged = sweep_sla_breaches()
        self.stdout.write(self.style.SUCCESS(f"SLA sweep: {flagged} lead(s) flagged."))
