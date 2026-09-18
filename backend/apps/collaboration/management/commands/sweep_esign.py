"""E-sign housekeeping: expire stale envelopes, remind the current signer.

Idempotent both ways: expiry is a status transition (an EXPIRED envelope is not
re-selected), and the reminder pass keys on the age of the latest SENT/REMINDER_SENT
event — overlapping runs send at most one reminder per window.

    python manage.py sweep_esign
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.collaboration import services
from apps.collaboration.models import EsignEnvelope, SignatureEvent


class Command(BaseCommand):
    help = "Expire stale envelopes and nudge pending signers."

    def add_arguments(self, parser):
        parser.add_argument("--remind-after-days", type=int, default=None)
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **options):
        from django.conf import settings

        now = timezone.now()
        remind_after = options["remind_after_days"]
        if remind_after is None:  # 0 is a valid, immediate window
            remind_after = getattr(settings, "ESIGN_REMINDER_AFTER_DAYS", 3)
        expired = reminded = 0

        live = (EsignEnvelope.Status.SENT, EsignEnvelope.Status.PARTIALLY_SIGNED)
        for envelope in EsignEnvelope.objects.filter(
            status__in=live, expires_at__lt=now
        ):
            envelope.status = EsignEnvelope.Status.EXPIRED
            envelope.save(update_fields=["status", "updated_at"])
            SignatureEvent.objects.create(
                envelope=envelope, event_type=SignatureEvent.EventType.VOIDED,
                metadata={"expired": True}, occurred_at=now,
            )
            expired += 1

        cutoff = now - timedelta(days=remind_after)
        for envelope in EsignEnvelope.objects.filter(status__in=live):
            latest = (
                envelope.signature_events.filter(
                    event_type__in=(
                        SignatureEvent.EventType.SENT,
                        SignatureEvent.EventType.REMINDER_SENT,
                    )
                )
                .order_by("-occurred_at")
                .first()
            )
            if latest is not None and latest.occurred_at <= cutoff:
                services.remind(envelope)
                reminded += 1

        if expired or reminded:
            self.stdout.write(self.style.WARNING(f"expired={expired} reminded={reminded}"))
        elif not options["quiet"]:
            self.stdout.write("Nothing to do.")
