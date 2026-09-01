"""Shared enums used across apps (avoids duplicated/typo-prone choice lists)."""
from django.db import models


class ScopedEntityType(models.TextChoices):
    """Entity types that can carry admin-defined custom fields or be explicitly
    shared with a user outside their normal row-level scope."""

    CONTACT = "CONTACT", "Contact"
    LEAD = "LEAD", "Lead"
    DEAL = "DEAL", "Deal"
    PROPERTY = "PROPERTY", "Property"
    LISTING = "LISTING", "Listing"
    LEASE = "LEASE", "Lease"
