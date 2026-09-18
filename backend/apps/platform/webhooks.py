"""Outbound webhook fan-out (SRS 3.19.2) and its retry loop.

Design notes, in decision order:
* **Attempt log = SyncLog rows** — no schema change. `create_webhook` auto-creates (or
  reuses) a `Connection(provider=WEBHOOK)` so SyncLog's NOT NULL connection FK is always
  satisfiable. The attempt counter and event id ride in `payload_snapshot`. Graduation
  path: if delivery volume ever warrants it, a dedicated delivery table replaces this —
  documented here so the trade-off is not rediscovered as a bug.
* **Delivery is synchronous best-effort and never raises** — the caller is a domain
  receiver inside a user's transaction; a slow endpoint costs the timeout (5s), a broken
  one costs nothing but a FAILED row that `retry_webhooks` picks up.
* **Signature**: `X-Webhook-Signature: sha256=<HMAC-SHA256(secret, body)>` over the exact
  bytes sent, plus `X-Webhook-Delivery: <event id>` for receiver-side dedup.
* **Transport**: `WEBHOOK_TRANSPORT=mock` (an importable outbox list, mail.outbox
  ergonomics) or `urllib` (stdlib — no new dependency; `requests` is the documented
  production hardening, third-part-needed.md §16).
"""
import hashlib
import hmac
import json
import logging
import uuid

from django.conf import settings
from django.utils import timezone

from .models import Connection, SyncLog, Webhook

logger = logging.getLogger(__name__)

#: The mock transport's outbox. Tests read and clear it.
outbox = []

DELIVERY_TIMEOUT_SECONDS = 5
MAX_ATTEMPTS = 5
BACKOFF_BASE_MINUTES = 5


def sign(secret, body: bytes) -> str:
    return "sha256=" + hmac.new(
        (secret or "").encode(), body, hashlib.sha256
    ).hexdigest()


def _transport():
    return getattr(settings, "WEBHOOK_TRANSPORT", "mock")


def _post(url, body: bytes, headers) -> tuple[bool, str]:
    """(delivered, detail). Never raises."""
    if _transport() == "mock":
        fail = getattr(settings, "WEBHOOK_MOCK_FAIL", False)
        outbox.append({"url": url, "body": body, "headers": dict(headers)})
        return (not fail), ("mock delivered" if not fail else "mock forced failure")
    try:
        from urllib import request as urllib_request

        req = urllib_request.Request(
            url, data=body, headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=DELIVERY_TIMEOUT_SECONDS) as response:
            ok = 200 <= response.status < 300
            return ok, f"HTTP {response.status}"
    except Exception as exc:  # noqa: BLE001 - delivery failure is data, not a crash
        return False, str(exc)


def _webhook_connection():
    """The housekeeping Connection every outbound webhook hangs its logs on."""
    connection = Connection.objects.filter(
        provider=Connection.Provider.WEBHOOK, name="Outbound webhooks"
    ).first()
    if connection is None:
        from apps.identity.models import User

        # Any superuser satisfies the NOT NULL created_by; webhooks configured before any
        # superuser exists is not a real sequence (the admin creating them is one).
        owner = User.objects.filter(is_superuser=True).first() or User.objects.first()
        connection = Connection.objects.create(
            name="Outbound webhooks",
            provider=Connection.Provider.WEBHOOK,
            direction=Connection.Direction.OUTBOUND,
            auth_type=Connection.AuthType.NONE,
            created_by=owner,
        )
    return connection


def create_webhook(*, actor, event_type, target_url, secret=None, connection=None):
    return Webhook.objects.create(
        connection=connection or _webhook_connection(),
        direction=Webhook.Direction.OUTBOUND,
        event_type=event_type,
        target_url=target_url,
        secret=secret,
    )


def dispatch_webhooks(event_type, payload):
    """Fan one event out to every active outbound webhook subscribed to it. Called from
    platform receivers; **never raises**."""
    try:
        hooks = list(
            Webhook.objects.filter(
                direction=Webhook.Direction.OUTBOUND,
                event_type=event_type,
                is_active=True,
            ).select_related("connection")
        )
        if not hooks:
            return []
        event_id = str(uuid.uuid4())
        body = json.dumps(
            {"event": event_type, "id": event_id, "data": payload}, default=str
        ).encode()
        return [_attempt(hook, event_id, body, attempt=1) for hook in hooks]
    except Exception:  # noqa: BLE001 - fan-out must never break the domain write
        logger.exception("Webhook fan-out failed for %s", event_type)
        return []


def _attempt(hook, event_id, body: bytes, *, attempt):
    started = timezone.now()
    delivered, detail = _post(
        hook.target_url, body,
        {
            "X-Webhook-Signature": sign(hook.secret, body),
            "X-Webhook-Delivery": event_id,
        },
    )
    return SyncLog.objects.create(
        connection=hook.connection or _webhook_connection(),
        entity_type="WEBHOOK",
        direction=SyncLog.Direction.OUTBOUND,
        status=SyncLog.Status.SUCCESS if delivered else SyncLog.Status.FAILED,
        records_processed=1 if delivered else 0,
        records_failed=0 if delivered else 1,
        started_at=started,
        finished_at=timezone.now(),
        error_detail=None if delivered else detail,
        payload_snapshot={
            "webhook_id": str(hook.pk),
            "event_id": event_id,
            "attempt": attempt,
            "body": body.decode(),
        },
    )


def retry_webhooks(now=None):
    """Re-deliver FAILED attempts: exponential backoff (5min × 2^(n-1)), capped at
    MAX_ATTEMPTS, and superseded-aware — if a later attempt for the same event already
    succeeded, the failure is history, not work."""
    from datetime import timedelta

    now = now or timezone.now()
    retried = []
    failures = SyncLog.objects.filter(
        entity_type="WEBHOOK", status=SyncLog.Status.FAILED
    ).order_by("started_at")
    for log in failures:
        snapshot = log.payload_snapshot or {}
        event_id = snapshot.get("event_id")
        attempt = int(snapshot.get("attempt", 1))
        if not event_id or attempt >= MAX_ATTEMPTS:
            continue
        # Superseded? A newer row for this event that succeeded, or a newer failure that
        # owns the retry chain now.
        newer = SyncLog.objects.filter(
            entity_type="WEBHOOK",
            payload_snapshot__event_id=event_id,
            started_at__gt=log.started_at,
        )
        if newer.exists():
            continue
        due_at = log.started_at + timedelta(
            minutes=BACKOFF_BASE_MINUTES * (2 ** (attempt - 1))
        )
        if due_at > now:
            continue
        hook = Webhook.objects.filter(pk=snapshot.get("webhook_id"), is_active=True).first()
        if hook is None:
            continue
        retried.append(
            _attempt(hook, event_id, snapshot.get("body", "").encode(),
                     attempt=attempt + 1)
        )
    return retried
