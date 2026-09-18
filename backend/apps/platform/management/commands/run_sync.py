"""Run portal/lead-feed syncs (SRS §3.10). Idempotent: unchanged listings are hash-skipped,
already-imported leads are mapping-skipped.

    python manage.py run_sync [--connection <uuid>]
"""
from django.core.management.base import BaseCommand, CommandError

from apps.platform.models import Connection
from apps.platform.sync import run_all_syncs, run_connection_sync


class Command(BaseCommand):
    help = "Sync every active connection (or one, with --connection)."

    def add_arguments(self, parser):
        parser.add_argument("--connection", help="Sync only this connection id")
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        if options["connection"]:
            try:
                connection = Connection.objects.get(pk=options["connection"])
            except (Connection.DoesNotExist, ValueError) as exc:
                raise CommandError("No such connection.") from exc
            logs = [run_connection_sync(connection)]
        else:
            logs = run_all_syncs()
        for log in logs:
            line = (
                f"{log.connection.name}: {log.status} "
                f"(+{log.records_processed} / -{log.records_failed})"
            )
            if log.status == "SUCCESS":
                self.stdout.write(self.style.SUCCESS(line))
            else:
                self.stdout.write(self.style.WARNING(line))
        if not logs and not options["quiet"]:
            self.stdout.write("No active connections.")
