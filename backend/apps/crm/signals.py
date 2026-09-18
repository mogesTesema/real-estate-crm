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

#: SRS 3.4.4 — every stage move is audit- and webhook-worthy.
#: kwargs: deal, actor, from_stage, to_stage, reason
deal_stage_moved = Signal()

#: SRS 3.6.1 — an accepted offer settles the negotiation.
#: kwargs: offer, actor
offer_accepted = Signal()

#: SRS 3.6.6 — transaction lifecycle for audit + webhook fan-out.
#: kwargs: transaction_obj, actor, from_status, to_status
transaction_status_changed = Signal()

#: Request/response (the `verify_portal_eligibility` precedent): `platform` receives an
#: external lead and asks `crm` to capture it — platform may not import crm.services, so
#: the dependency inverts through this signal. The receiver returns the created lead's id.
#: kwargs: payload (dict with contact_data + lead fields), connection_name
inbound_lead_received = Signal()
