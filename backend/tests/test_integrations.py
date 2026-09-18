"""Connection sync and webhook fan-out (SRS §3.10, 3.19.2).

The doctrines under test: sync re-runs are idempotent in both directions (hash-skip
outbound, mapping-skip inbound); a failed run is the alerting surface (connection wears
ERROR + last_error); webhook delivery is signed, logged as SyncLog rows, retried with
backoff, and superseded-aware.
"""
import json

import pytest
from django.utils import timezone

from apps.platform import webhooks as webhook_module
from apps.platform.models import Connection, ExternalMapping, SyncLog, Webhook
from apps.platform.sync import run_connection_sync
from apps.platform.webhooks import create_webhook, dispatch_webhooks, retry_webhooks, sign


@pytest.fixture(autouse=True)
def _clear_outbox():
    webhook_module.outbox.clear()
    yield
    webhook_module.outbox.clear()


@pytest.fixture
def admin(make_user):
    return make_user("super_admin")


@pytest.fixture
def portal_connection(admin):
    return Connection.objects.create(
        name="PropertyFinder", provider="PROPERTY_FINDER",
        direction="BIDIRECTIONAL", auth_type="API_KEY",
        credentials_ref="secrets/propertyfinder", created_by=admin,
    )


@pytest.fixture
def active_listing(make_property, make_user):
    from apps.inventory import services as inventory_services

    pm = make_user("property_manager")

    def _make(**kwargs):
        listing = inventory_services.create_listing(
            actor=pm, property=make_property(), listing_type="SALE",
            title=kwargs.pop("title", "Synced listing"),
            asking_price=kwargs.pop("asking_price", 1000000), **kwargs,
        )
        inventory_services.change_listing_status(listing, "ACTIVE", actor=pm)
        return listing

    return _make


class TestOutboundSync:
    def test_push_maps_and_rerun_skips_unchanged(
        self, db, portal_connection, active_listing
    ):
        listing = active_listing()
        log = run_connection_sync(portal_connection)
        assert log.status == "SUCCESS"
        assert log.records_processed == 1
        mapping = ExternalMapping.objects.get(
            connection=portal_connection, entity_type="LISTING", local_id=listing.pk
        )
        assert mapping.external_id == f"ext-{listing.pk}"

        # Re-run: nothing changed, nothing pushed — the hash skip.
        log = run_connection_sync(portal_connection)
        assert log.records_processed == 0
        assert ExternalMapping.objects.filter(entity_type="LISTING").count() == 1

    def test_a_change_is_re_pushed_to_the_same_external_id(
        self, db, portal_connection, active_listing, make_user
    ):
        from apps.inventory import services as inventory_services

        listing = active_listing()
        run_connection_sync(portal_connection)
        inventory_services.update_listing(
            listing, actor=make_user("property_manager"), asking_price=1100000
        )
        log = run_connection_sync(portal_connection)
        assert log.records_processed == 1
        assert ExternalMapping.objects.filter(entity_type="LISTING").count() == 1

    def test_a_broken_adapter_marks_the_connection(self, db, admin):
        connection = Connection.objects.create(
            name="Broken portal", provider="PROPERTY_FINDER", direction="OUTBOUND",
            auth_type="API_KEY", created_by=admin,
            config={"adapter": "apps.nonexistent.Adapter"},
        )
        log = run_connection_sync(connection)
        assert log.status == "FAILED"
        connection.refresh_from_db()
        assert connection.status == Connection.Status.ERROR
        assert connection.last_error  # 3.10.4's alerting surface

    def test_recovery_clears_the_error(self, db, portal_connection):
        portal_connection.status = Connection.Status.ERROR
        portal_connection.last_error = "old failure"
        portal_connection.save()
        run_connection_sync(portal_connection)
        portal_connection.refresh_from_db()
        assert portal_connection.status == Connection.Status.ACTIVE
        assert portal_connection.last_error is None


class TestInboundLeads:
    def test_pulled_leads_run_the_capture_pipeline_once(self, db, admin):
        from apps.crm.models import Lead

        connection = Connection.objects.create(
            name="MLS feed", provider="MLS", direction="INBOUND",
            auth_type="API_KEY", created_by=admin,
            config={"fixture_leads": [
                {
                    "external_id": "mls-1",
                    "lead_type": "BUY",
                    "contact_data": {
                        "contact_type": "PERSON", "first_name": "Feed",
                        "last_name": "Buyer", "email": "feed@example.test",
                    },
                },
            ]},
        )
        log = run_connection_sync(connection)
        assert log.status == "SUCCESS", log.error_detail
        lead = Lead.objects.get()
        assert lead.contact.email == "feed@example.test"
        assert lead.source.name == "MLS feed"
        assert lead.sla_due_at is not None  # the full pipeline ran

        # Re-pull: the mapping remembers, no duplicate lead.
        run_connection_sync(connection)
        assert Lead.objects.count() == 1

    def test_a_malformed_lead_is_a_partial_not_a_crash(self, db, admin):
        connection = Connection.objects.create(
            name="MLS feed", provider="MLS", direction="INBOUND",
            auth_type="API_KEY", created_by=admin,
            config={"fixture_leads": [
                {"external_id": "bad-1"},  # no contact_data
                {
                    "external_id": "good-1",
                    "contact_data": {
                        "contact_type": "PERSON", "first_name": "Ok",
                        "last_name": "Lead", "email": "ok@example.test",
                    },
                },
            ]},
        )
        log = run_connection_sync(connection)
        assert log.status == "PARTIAL"
        assert log.records_processed == 1
        assert log.records_failed == 1


class TestOutboundWebhooks:
    def test_dispatch_signs_and_logs(self, db, admin):
        hook = create_webhook(
            actor=admin, event_type="lead.captured",
            target_url="https://example.test/hook", secret="s3cret",
        )
        logs = dispatch_webhooks("lead.captured", {"lead_id": "abc"})
        assert len(logs) == 1
        assert logs[0].status == "SUCCESS"
        delivery = webhook_module.outbox[-1]
        body = delivery["body"]
        assert delivery["headers"]["X-Webhook-Signature"] == sign("s3cret", body)
        assert json.loads(body)["event"] == "lead.captured"
        assert delivery["headers"]["X-Webhook-Delivery"]

    def test_domain_events_fan_out_through_receivers(
        self, db, admin, make_user, make_contact
    ):
        from apps.crm import services as crm_services

        create_webhook(
            actor=admin, event_type="lead.captured",
            target_url="https://example.test/hook", secret="x",
        )
        crm_services.capture_lead(
            actor=make_user("agent"), contact=make_contact(), lead_type="BUY",
            acknowledge=False, route=False,
        )
        assert len(webhook_module.outbox) == 1

    def test_retry_respects_backoff_supersession_and_cap(
        self, db, admin, settings
    ):
        settings.WEBHOOK_MOCK_FAIL = True
        create_webhook(
            actor=admin, event_type="deal.stage_moved",
            target_url="https://example.test/hook", secret="x",
        )
        [failed] = dispatch_webhooks("deal.stage_moved", {"deal_id": "d1"})
        assert failed.status == "FAILED"

        # Backoff: attempt 1 retries only 5 minutes later.
        assert retry_webhooks() == []
        later = timezone.now() + timezone.timedelta(minutes=6)
        settings.WEBHOOK_MOCK_FAIL = False
        [second] = retry_webhooks(now=later)
        assert second.status == "SUCCESS"
        assert second.payload_snapshot["attempt"] == 2
        assert (
            second.payload_snapshot["event_id"] == failed.payload_snapshot["event_id"]
        )
        # The original failure is superseded — nothing left to retry.
        assert retry_webhooks(now=later + timezone.timedelta(hours=2)) == []


class TestInboundWebhookEndpoint:
    @pytest.fixture
    def inbound(self, admin, portal_connection):
        return Webhook.objects.create(
            connection=portal_connection, direction="INBOUND",
            event_type="lead.created", secret="inbound-secret",
        )

    def test_a_signed_lead_event_captures(self, db, api_client, inbound):
        from apps.crm.models import Lead

        body = json.dumps({
            "external_id": "wh-1",
            "contact_data": {
                "contact_type": "PERSON", "first_name": "Hook",
                "last_name": "Lead", "email": "hook@example.test",
            },
        }).encode()
        response = api_client.post(
            f"/api/public/webhooks/{inbound.pk}/", body,
            content_type="application/json",
            headers={"X-Webhook-Signature": sign("inbound-secret", body)},
        )
        assert response.status_code == 200, response.data
        assert Lead.objects.get().contact.email == "hook@example.test"
        assert SyncLog.objects.filter(direction="INBOUND", status="SUCCESS").exists()

    def test_a_bad_signature_is_a_401(self, db, api_client, inbound):
        from apps.crm.models import Lead

        body = b'{"anything": true}'
        response = api_client.post(
            f"/api/public/webhooks/{inbound.pk}/", body,
            content_type="application/json",
            headers={"X-Webhook-Signature": "sha256=wrong"},
        )
        assert response.status_code == 401
        assert Lead.objects.count() == 0

    def test_an_outbound_registration_is_not_a_door(self, db, api_client, admin):
        hook = create_webhook(
            actor=admin, event_type="lead.captured",
            target_url="https://example.test/hook", secret="x",
        )
        assert api_client.post(
            f"/api/public/webhooks/{hook.pk}/", b"{}",
            content_type="application/json",
        ).status_code == 404


class TestAdminApi:
    def test_the_surface_is_agency_admin_only(
        self, db, auth_client, admin, make_user, portal_connection
    ):
        agent = make_user("agent")
        for route in ("connections", "webhooks", "external-mappings", "sync-logs"):
            assert auth_client(agent).get(f"/api/v1/{route}/").status_code == 403
        assert auth_client(admin).get("/api/v1/connections/").status_code == 200

    def test_the_webhook_secret_never_comes_back(self, db, auth_client, admin):
        response = auth_client(admin).post(
            "/api/v1/webhooks/",
            {
                "direction": "OUTBOUND", "event_type": "lead.captured",
                "target_url": "https://example.test/hook", "secret": "top-secret",
            },
        )
        assert response.status_code == 201, response.data
        assert "secret" not in response.data
        listed = auth_client(admin).get("/api/v1/webhooks/").data["results"]
        assert all("secret" not in row for row in listed)

    def test_manual_sync_button(self, db, auth_client, admin, portal_connection):
        response = auth_client(admin).post(
            f"/api/v1/connections/{portal_connection.pk}/sync/"
        )
        assert response.status_code == 200
        assert response.data["status"] in ("SUCCESS", "PARTIAL", "FAILED")
