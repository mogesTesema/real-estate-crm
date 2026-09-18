"""Pre-compute dashboard KPIs per org/branch/team (SRS 3.13.1). The NULLS-NOT-DISTINCT
unique key makes the upsert idempotent — run it nightly, run it twice, same rows.

    python manage.py build_dashboard_snapshots [--as-of YYYY-MM-DD]
"""
import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.platform.selectors import build_dashboard_snapshots


class Command(BaseCommand):
    help = "Upsert today's KPI snapshots for the org, each branch and each team."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", help="Snapshot date (default: today)")

    def handle(self, *args, **options):
        as_of = None
        if options["as_of"]:
            try:
                as_of = datetime.date.fromisoformat(options["as_of"])
            except ValueError as exc:
                raise CommandError("--as-of is YYYY-MM-DD.") from exc
        written = build_dashboard_snapshots(as_of=as_of)
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(written)} snapshot(s)."))
