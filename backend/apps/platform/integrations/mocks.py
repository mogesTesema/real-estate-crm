"""Deterministic mock adapters — the test double AND the dev default.

Deterministic on purpose: `push_listing` derives the external id from the local id, so a
re-push maps to the same external record and the idempotent-upsert path is exercised for
real. `pull_leads` reads fixtures from `connection.config["fixture_leads"]`, so a test (or
a demo) controls exactly what "the portal" hands back.
"""


class MockPortalAdapter:
    """Outbound listing syndication (SRS 3.10.1). Accepts everything."""

    def push_listing(self, connection, payload):
        """Returns the provider's id for the listing. Deterministic: local id in, stable
        external id out."""
        return {"external_id": f"ext-{payload['id']}", "status": "ACCEPTED"}

    def fetch_statuses(self, connection, external_ids):
        """Inbound status reconciliation: the mock says every pushed listing is LIVE."""
        return {external_id: "LIVE" for external_id in external_ids}


class MockLeadFeedAdapter:
    """Inbound lead feed (SRS 3.10.2). Hands back whatever the connection's config holds."""

    def pull_leads(self, connection):
        return list((connection.config or {}).get("fixture_leads", []))
