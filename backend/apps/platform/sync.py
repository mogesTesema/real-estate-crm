"""The connection sync engine (SRS 3.10.1–3.10.4).

Outbound: push ACTIVE listings through the connection's adapter, skipping unchanged rows by
`sync_hash` and upserting `ExternalMapping` (its two unique constraints are what make
re-runs idempotent in both directions). Inbound: pull leads and hand each to `crm` over the
`inbound_lead_received` signal — a satellite may not import `crm.services`, so the
dependency inverts the same way `verify_portal_eligibility` does.

Every run closes a `SyncLog` row SUCCESS/PARTIAL/FAILED, and a failed run stamps the
connection's `last_error` / `status=ERROR` — that pair is the 3.10.4 alerting surface.
"""
import hashlib
import json
import logging

from django.utils import timezone

from .integrations import get_adapter
from .models import Connection, ExternalMapping, SyncLog

logger = logging.getLogger(__name__)


def _listing_payload(listing):
    """What the portal is told about a listing. Whitelist — the public card's doctrine."""
    prop = listing.property
    return {
        "id": str(listing.pk),
        "title": listing.title,
        "description": listing.description,
        "listing_type": listing.listing_type,
        "asking_price": str(listing.asking_price) if listing.asking_price else None,
        "rent_amount": str(listing.rent_amount) if listing.rent_amount else None,
        "city": prop.city,
        "country": prop.country,
        "bedrooms": prop.bedrooms,
        "bathrooms": prop.bathrooms,
        "built_area": str(prop.built_area) if prop.built_area else None,
        "property_type": prop.property_type.name,
    }


def _hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()


def run_connection_sync(connection):
    """One sync run for one connection. Returns the closed SyncLog."""
    started = timezone.now()
    processed = failed = 0
    errors = []

    try:
        adapter = get_adapter(connection)
        if connection.direction in (
            Connection.Direction.OUTBOUND, Connection.Direction.BIDIRECTIONAL
        ) and hasattr(adapter, "push_listing"):
            processed_out, failed_out, errors_out = _push_listings(connection, adapter)
            processed += processed_out
            failed += failed_out
            errors += errors_out
        if connection.direction in (
            Connection.Direction.INBOUND, Connection.Direction.BIDIRECTIONAL
        ) and hasattr(adapter, "pull_leads"):
            processed_in, failed_in, errors_in = _pull_leads(connection, adapter)
            processed += processed_in
            failed += failed_in
            errors += errors_in
        run_status = (
            SyncLog.Status.SUCCESS if not failed
            else SyncLog.Status.PARTIAL if processed
            else SyncLog.Status.FAILED
        )
    except Exception as exc:  # noqa: BLE001 - a broken adapter is a FAILED run, not a crash
        logger.exception("Sync failed for connection %s", connection.pk)
        run_status = SyncLog.Status.FAILED
        errors.append(str(exc))

    log = SyncLog.objects.create(
        connection=connection,
        direction=connection.direction,
        status=run_status,
        records_processed=processed,
        records_failed=failed,
        started_at=started,
        finished_at=timezone.now(),
        error_detail="\n".join(errors) or None,
    )
    connection.last_sync_at = log.finished_at
    if run_status == SyncLog.Status.FAILED:
        # SRS 3.10.4's alerting surface: the connection wears its failure.
        connection.status = Connection.Status.ERROR
        connection.last_error = log.error_detail
        connection.save(update_fields=["last_sync_at", "status", "last_error", "updated_at"])
    else:
        connection.last_error = None
        if connection.status == Connection.Status.ERROR:
            connection.status = Connection.Status.ACTIVE
        connection.save(update_fields=["last_sync_at", "status", "last_error", "updated_at"])
    return log


def _push_listings(connection, adapter):
    """Outbound syndication over `inventory.selectors` — the satellite-legal read path."""
    from apps.inventory.models import Listing
    from apps.inventory.selectors import live_listings

    processed = failed = 0
    errors = []
    for listing in live_listings().filter(
        status=Listing.Status.ACTIVE
    ).select_related("property", "property__property_type"):
        payload = _listing_payload(listing)
        digest = _hash(payload)
        mapping = ExternalMapping.objects.filter(
            connection=connection, entity_type="LISTING", local_id=listing.pk
        ).first()
        if mapping is not None and mapping.sync_hash == digest:
            continue  # unchanged since the last push
        try:
            result = adapter.push_listing(connection, payload)
            ExternalMapping.objects.update_or_create(
                connection=connection, entity_type="LISTING", local_id=listing.pk,
                defaults={
                    "external_id": result["external_id"],
                    "sync_hash": digest,
                    "last_synced_at": timezone.now(),
                },
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001 - one bad listing must not sink the batch
            logger.exception("Push failed for listing %s", listing.pk)
            failed += 1
            errors.append(f"listing {listing.pk}: {exc}")

    # Inbound status reconciliation REPORTS, it does not mutate: a satellite cannot call
    # inventory.services, so a portal-side takedown lands in the sync log for an admin to
    # act on (documented consequence of the DAG, not an oversight).
    if hasattr(adapter, "fetch_statuses"):
        external_ids = list(
            ExternalMapping.objects.filter(
                connection=connection, entity_type="LISTING"
            ).values_list("external_id", flat=True)
        )
        if external_ids:
            try:
                statuses = adapter.fetch_statuses(connection, external_ids)
                dead = [eid for eid, s in statuses.items() if s not in ("LIVE", "ACCEPTED")]
                if dead:
                    errors.append(
                        f"portal reports {len(dead)} listing(s) not live: {dead[:5]}"
                    )
                    failed += len(dead)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Status fetch failed for connection %s", connection.pk)
                errors.append(f"status fetch: {exc}")
    return processed, failed, errors


def _pull_leads(connection, adapter):
    """Inbound leads → the `inbound_lead_received` signal → crm's capture pipeline.

    Dedup: an `ExternalMapping` LEAD row per external id — a re-pulled lead is skipped, so
    running the sync twice cannot create two leads.
    """
    from apps.crm.signals import inbound_lead_received

    processed = failed = 0
    errors = []
    for raw in adapter.pull_leads(connection):
        external_id = str(raw.get("external_id") or "")
        if not external_id:
            failed += 1
            errors.append("lead without external_id skipped")
            continue
        if ExternalMapping.objects.filter(
            connection=connection, entity_type="LEAD", external_id=external_id
        ).exists():
            continue  # already captured on a previous run
        responses = inbound_lead_received.send_robust(
            sender=None, payload=raw, connection_name=connection.name
        )
        lead_id = next(
            (r for _, r in responses if isinstance(r, str)), None
        )
        if lead_id is None:
            failed += 1
            errors.append(f"lead {external_id} refused by capture")
            continue
        ExternalMapping.objects.create(
            connection=connection, entity_type="LEAD", local_id=lead_id,
            external_id=external_id, last_synced_at=timezone.now(),
        )
        processed += 1
    return processed, failed, errors


def run_all_syncs():
    """Every ACTIVE connection, one run each. ERROR connections are retried too — that is
    how they recover; PAUSED/DISABLED are respected."""
    logs = []
    for connection in Connection.objects.filter(
        status__in=(Connection.Status.ACTIVE, Connection.Status.ERROR)
    ).exclude(provider=Connection.Provider.WEBHOOK):
        logs.append(run_connection_sync(connection))
    return logs
