"""Run due report schedules (SRS 3.13.3).

Claim: `select_for_update(skip_locked=True)` on due rows, and **`next_run_at` advances
from the scheduled time BEFORE executing** — no drift (a 09:00 daily stays 09:00 however
late the runner fires) and no crash-loop (a failing report does not re-run every minute).
Executes as the CREATOR's scope; recipients are the creator's responsibility, documented.
CSV inline via the email gateway; PDF/XLSX refused at the serializer until their
libraries arrive (third-part-needed.md).

    python manage.py run_report_schedules
"""
import csv
import io
import logging

from dateutil.relativedelta import relativedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.platform.models import ReportSchedule
from apps.platform.reporting import execute_and_audit

logger = logging.getLogger(__name__)

_STEP = {
    "DAILY": relativedelta(days=1),
    "WEEKLY": relativedelta(weeks=1),
    "MONTHLY": relativedelta(months=1),
}


class Command(BaseCommand):
    help = "Execute report schedules whose next_run_at has arrived."

    def add_arguments(self, parser):
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        now = timezone.now()
        ran = 0
        while True:
            with transaction.atomic():
                schedule = (
                    ReportSchedule.objects.select_for_update(skip_locked=True)
                    .filter(is_active=True, next_run_at__lte=now)
                    .select_related("saved_report", "created_by")
                    .order_by("next_run_at")
                    .first()
                )
                if schedule is None:
                    break
                # Advance from the SCHEDULED time first: no drift, no crash-loop.
                step = _STEP[schedule.frequency]
                next_run = schedule.next_run_at + step
                while next_run <= now:  # catch up after downtime without a burst
                    next_run += step
                schedule.next_run_at = next_run
                schedule.last_run_at = now
                schedule.save(update_fields=["next_run_at", "last_run_at"])
            try:
                self._execute(schedule)
                ran += 1
            except Exception:  # noqa: BLE001 - the next schedule still deserves its run
                logger.exception("Report schedule %s failed", schedule.pk)
        if ran:
            self.stdout.write(self.style.SUCCESS(f"Ran {ran} schedule(s)."))
        elif not options["quiet"]:
            self.stdout.write("Nothing due.")

    @staticmethod
    def _execute(schedule):
        from apps.collaboration.gateways import get_gateway

        result = execute_and_audit(schedule.saved_report, schedule.created_by)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        if "rows" in result:
            writer.writerow(result["columns"])
            for row in result["rows"]:
                writer.writerow([row.get(name) for name in result["columns"]])
        else:  # analytic payload — flatten to key,value lines
            writer.writerow(["key", "value"])
            for key, value in (result.get("data") or {}).items() if isinstance(
                result.get("data"), dict
            ) else enumerate(result.get("data") or []):
                writer.writerow([key, value])
        body = (
            f"Scheduled report: {schedule.saved_report.name}\n\n{buffer.getvalue()}"
        )
        gateway = get_gateway("EMAIL")
        for recipient in schedule.recipients or []:
            gateway.send(
                to_address=recipient,
                subject=f"Report: {schedule.saved_report.name}",
                body=body,
            )
