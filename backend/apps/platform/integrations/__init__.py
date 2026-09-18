"""Adapter seam for external portals and lead feeds (SRS §3.10).

Every provider hides behind one of two small protocols; the CRM never talks to a portal
directly. Today's implementations are deterministic mocks (third-part-needed.md §§6–9
documents the real ones); swapping in PropertyFinder/Bayut later means writing an adapter
class and pointing the connection's config at it — the sync engine does not change.
"""
from functools import cache

from django.conf import settings
from django.utils.module_loading import import_string

#: provider code -> adapter path. A connection's `config["adapter"]` may override.
DEFAULT_ADAPTERS = {
    "PROPERTY_FINDER": "apps.platform.integrations.mocks.MockPortalAdapter",
    "BAYUT": "apps.platform.integrations.mocks.MockPortalAdapter",
    "DUBIZZLE": "apps.platform.integrations.mocks.MockPortalAdapter",
    "MLS": "apps.platform.integrations.mocks.MockLeadFeedAdapter",
    "OTHER": "apps.platform.integrations.mocks.MockPortalAdapter",
}


def get_adapter(connection):
    """The adapter instance for a connection. Config wins, then settings, then defaults."""
    path = (
        (connection.config or {}).get("adapter")
        or getattr(settings, "INTEGRATION_ADAPTERS", {}).get(connection.provider)
        or DEFAULT_ADAPTERS.get(connection.provider)
    )
    if path is None:
        raise KeyError(f"No adapter for provider {connection.provider!r}")
    return _load(path)


@cache
def _load(path):
    return import_string(path)()
