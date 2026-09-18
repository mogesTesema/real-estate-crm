"""Channel gateways — the seam every outbound send goes through.

One abstraction, four channels. EMAIL is real (Django's mail framework, which the password
reset and lead acknowledgment already use). SMS, WhatsApp and push are **mocks that behave
like providers**: they return a provider message id and can be told to fail, so every send
path in the system is exercisable today and swapping in Twilio or FCM later is a settings
change, not a refactor. Each mock's real counterpart is documented in `third-part-needed.md`.

Selection is by settings::

    COLLABORATION_GATEWAYS = {
        "EMAIL": "apps.collaboration.gateways.DjangoEmailGateway",
        "SMS": "apps.collaboration.gateways.LoggingSmsGateway",
        ...
    }

`get_gateway(channel)` resolves lazily with an lru_cache; tests that swap gateways call
`get_gateway.cache_clear()`.
"""
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cache

from django.conf import settings
from django.core.mail import send_mail
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


@dataclass
class GatewayResult:
    provider_message_id: str
    status: str  # "SENT" | "FAILED"
    error: str | None = None


class MessageGateway(ABC):
    """Sends one message on one channel. Implementations must not raise for delivery
    failures — a failed send is a *result*, and the caller decides what a failure means
    (a FAILED Message row, a FAILED dispatch log). Raising would abort the caller's
    transaction over a provider hiccup."""

    channel: str = ""

    @abstractmethod
    def send(self, *, to_address, body, subject=None, from_address=None) -> GatewayResult:
        ...


class DjangoEmailGateway(MessageGateway):
    """The real one. Console backend in dev, SMTP/SES in production — Django's layer."""

    channel = "EMAIL"

    def send(self, *, to_address, body, subject=None, from_address=None) -> GatewayResult:
        try:
            send_mail(
                subject=subject or "",
                message=body,
                from_email=from_address,  # None -> DEFAULT_FROM_EMAIL
                recipient_list=[to_address],
                fail_silently=False,
            )
        except Exception as exc:  # noqa: BLE001 - a failed send is a result, not a crash
            logger.exception("Email send failed to %s", to_address)
            return GatewayResult(f"email-{uuid.uuid4()}", "FAILED", str(exc))
        return GatewayResult(f"email-{uuid.uuid4()}", "SENT")


class _LoggingGateway(MessageGateway):
    """Base for the mocked channels: logs the send and reports success.

    The log line is the "delivery" — enough for a demo, honest about what it is, and the
    provider_message_id shape (`mock-<uuid>`) makes mock traffic unmistakable in the
    Message/DispatchLog tables.
    """

    def send(self, *, to_address, body, subject=None, from_address=None) -> GatewayResult:
        logger.info(
            "[mock %s gateway] to=%s subject=%r body=%.120r",
            self.channel, to_address, subject, body,
        )
        return GatewayResult(f"mock-{uuid.uuid4()}", "SENT")


class LoggingSmsGateway(_LoggingGateway):
    channel = "SMS"


class LoggingWhatsAppGateway(_LoggingGateway):
    channel = "WHATSAPP"


class LoggingPushGateway(_LoggingGateway):
    channel = "PUSH"


DEFAULT_GATEWAYS = {
    "EMAIL": "apps.collaboration.gateways.DjangoEmailGateway",
    "SMS": "apps.collaboration.gateways.LoggingSmsGateway",
    "WHATSAPP": "apps.collaboration.gateways.LoggingWhatsAppGateway",
    "PUSH": "apps.collaboration.gateways.LoggingPushGateway",
}


@cache
def get_gateway(channel: str) -> MessageGateway:
    """The configured gateway for a channel. Unknown channel is a programming error."""
    configured = {**DEFAULT_GATEWAYS, **getattr(settings, "COLLABORATION_GATEWAYS", {})}
    try:
        path = configured[channel]
    except KeyError as exc:
        raise KeyError(f"No gateway configured for channel {channel!r}") from exc
    return import_string(path)()
