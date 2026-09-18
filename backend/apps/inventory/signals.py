"""Signals `inventory` sends, and `platform` answers.

Same inversion as `identity.signals` and `contacts.signals`, for the same reason: §1.3
requires sensitive service mutations to reach `platform.services.record_event`, and the import
DAG forbids `inventory -> platform`. `platform` may import `inventory`, so `inventory` sends
and `platform` receives, connected in `apps/platform/apps.py::ready()`.
"""
from django.dispatch import Signal

#: A property or unit moved availability state (SRS 3.3.4). The history table already records
#: the move; this is what puts it in the one place a compliance reviewer reads.
#: kwargs: target, from_status, to_status, actor, reason
status_changed = Signal()

#: kwargs: listing, actor, from_status, to_status
listing_published = Signal()
