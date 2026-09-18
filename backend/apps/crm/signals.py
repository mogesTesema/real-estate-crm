"""Signals `crm` sends, and `platform` answers.

Same inversion as `identity`, `contacts` and `inventory`: §1.3 requires sensitive service
mutations to reach `platform.services.record_event`, and the import DAG forbids
`crm -> platform`. Connected in `apps/platform/apps.py::ready()`.
"""
from django.dispatch import Signal

#: kwargs: lead, actor, assigned_to, rule
lead_captured = Signal()

#: kwargs: lead, actor, from_user, to_user, reason
lead_assigned = Signal()

#: kwargs: lead, actor, deal
lead_converted = Signal()

#: SRS 3.16.7 — "access to GPS tracks shall be role-restricted and audited". Row visibility is
#: only half that control; this is the other half.
#: kwargs: session, actor, point_count
gps_track_accessed = Signal()
