"""Models for `collaboration` (architecture.md §12, §13, §17, §18).

architecture.md §1.2 makes this package layout normative, not optional:

    models/documents.py
    models/activities.py
    models/communications.py
    models/notifications.py

Every model is re-exported here so `from apps.collaboration.models import X` works regardless
of which submodule defines it.

Note the intra-app ordering: `DocumentLink` (documents.py) references `Thread` and `Message`
(communications.py) by lazy string, so a single `makemigrations collaboration` resolves the
order itself and no follow-up migration is needed.
"""
from .activities import Activity, CalendarLink
from .communications import CallLog, InternalNote, Message, Template, Thread
from .documents import (
    AccessGrant,
    Document,
    DocumentLink,
    EsignEnvelope,
    EsignSigner,
    File,
    SignatureEvent,
)
from .notifications import Notification, NotificationDispatchLog, NotificationPreference

__all__ = [
    "AccessGrant",
    "Activity",
    "CalendarLink",
    "CallLog",
    "Document",
    "DocumentLink",
    "EsignEnvelope",
    "EsignSigner",
    "File",
    "InternalNote",
    "Message",
    "Notification",
    "NotificationDispatchLog",
    "NotificationPreference",
    "SignatureEvent",
    "Template",
    "Thread",
]
