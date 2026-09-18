"""Inbound webhook receiver (SRS 3.19.2) — the one unauthenticated write door, locked by
HMAC.

`POST /api/public/webhooks/{id}/` — the id is the registered inbound Webhook row.
Verification is `hmac.compare_digest` over the exact request body; a bad or missing
signature is a 401 with no detail. Routing is by the registration's `event_type`:
`lead` events go to crm over the `inbound_lead_received` signal, `message.status`
callbacks go to collaboration's monotonic updater. Unknown events are acknowledged and
logged — a provider adding event types must not start bouncing deliveries.
"""
import hmac as hmac_lib
import json
import logging

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from ..models import SyncLog, Webhook
from ..webhooks import sign

logger = logging.getLogger(__name__)


class InboundWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "webhook_inbound"

    @extend_schema(auth=[], request=dict, responses={200: dict})
    def post(self, request, pk):
        webhook = Webhook.objects.filter(
            pk=pk, direction=Webhook.Direction.INBOUND, is_active=True
        ).select_related("connection").first()
        if webhook is None:
            from rest_framework.exceptions import NotFound

            raise NotFound

        body = request.body or b""
        provided = request.headers.get("X-Webhook-Signature", "")
        if not webhook.secret or not hmac_lib.compare_digest(
            sign(webhook.secret, body), provided
        ):
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            payload = json.loads(body.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            return Response(
                {"detail": "Body must be JSON."}, status=status.HTTP_400_BAD_REQUEST
            )

        started = timezone.now()
        outcome, detail = self._route(webhook, payload)
        if webhook.connection_id:
            SyncLog.objects.create(
                connection=webhook.connection,
                entity_type="WEBHOOK",
                direction=SyncLog.Direction.INBOUND,
                status=(
                    SyncLog.Status.SUCCESS if outcome else SyncLog.Status.FAILED
                ),
                records_processed=1 if outcome else 0,
                records_failed=0 if outcome else 1,
                started_at=started,
                finished_at=timezone.now(),
                error_detail=None if outcome else detail,
                payload_snapshot={"event_type": webhook.event_type},
            )
        return Response({"received": True})

    @staticmethod
    def _route(webhook, payload):
        event = webhook.event_type
        if event.startswith("lead"):
            from apps.crm.signals import inbound_lead_received

            responses = inbound_lead_received.send_robust(
                sender=None, payload=payload,
                connection_name=webhook.connection.name if webhook.connection_id else None,
            )
            lead_id = next((r for _, r in responses if isinstance(r, str)), None)
            return (lead_id is not None), (
                None if lead_id else "capture refused the payload"
            )
        if event.startswith("message"):
            from apps.collaboration import services as collaboration_services

            collaboration_services.update_message_status(
                channel=payload.get("channel", ""),
                provider_message_id=payload.get("provider_message_id", ""),
                status=payload.get("status", ""),
            )
            return True, None
        logger.info("Inbound webhook %s: unrouted event %r acknowledged", webhook.pk, event)
        return True, None
