"""Send due task reminders (SRS 3.12.2).

`reminder_sent_at` is the idempotency stamp — two overlapping runs send at most one reminder
per task. External scheduler, no Celery.

    python manage.py sweep_reminders
"""
from django.core.management.base import BaseCommand

from apps.collaboration.services import sweep_activity_reminders


class Command(BaseCommand):
    help = "Notify assignees of tasks whose reminder window has arrived."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        sent = sweep_activity_reminders()
        if sent:
            self.stdout.write(self.style.SUCCESS(f"Sent {len(sent)} reminder(s)."))
        elif not options["quiet"]:
            self.stdout.write("Nothing due.")
