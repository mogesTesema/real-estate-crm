"""Signals `contacts` sends, and `platform` answers.

Same inversion as `identity.signals`, for the same reason. architecture.md §1.3 requires
"every sensitive service mutation calls `platform.services.record_event`", and SRS 5.3 names
data **export** as one of the sensitive actions — but the import DAG (§1.2) forbids
`contacts -> platform`. `platform` may import `contacts`, so `contacts` sends and `platform`
receives, connected in `apps/platform/apps.py::ready()`.

Only genuinely sensitive mutations are signalled. Ordinary creates and edits are not: an audit
row per contact edit would bury the events that matter under noise, and `created_by` /
`updated_by` already carry that information on the row itself.
"""
from django.dispatch import Signal

#: Two records became one and one of them stopped existing. Irreversible in practice — the
#: children have moved — so it is the contacts mutation most worth a permanent trail.
#: kwargs: survivor, duplicate, actor, moved (dict of relation label -> row count)
contacts_merged = Signal()

#: kwargs: contact, actor
contact_deleted = Signal()

#: SRS 5.3 names data export explicitly. A CSV of the contact base is the single highest-value
#: thing a departing employee can take, so who exported what, and how much, is recorded.
#: kwargs: actor, row_count, fields
contacts_exported = Signal()

#: kwargs: actor, created, skipped, invalid, filename
contacts_imported = Signal()
