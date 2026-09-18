"""Signals `property_ops` sends, and `platform` answers.

Same inversion as every other domain app: §1.3 requires sensitive mutations to reach
`platform.services.record_event`, and the import DAG forbids `property_ops -> platform`.
Connected in `apps/platform/apps.py::ready()`.

`lease_status_changed` is doing double duty on purpose: property_ops has no
LeaseStatusHistory table (and EXPECTED_TABLES is an exact set), so the audit row this signal
produces IS the lease's status history — sent inside the mutating transaction, with
`record_event`'s own savepoint keeping an audit failure from poisoning the money path.
"""
from django.dispatch import Signal

#: kwargs: lease, actor
lease_created = Signal()

#: kwargs: lease, from_status, to_status, actor, reason
lease_status_changed = Signal()

#: kwargs: application, actor, decision
application_decided = Signal()

#: kwargs: deposit, actor, kind, amount
deposit_movement = Signal()

#: kwargs: request, from_status, to_status, actor
maintenance_status_changed = Signal()

#: kwargs: work_order, actor, final_amount, expense
work_order_completed = Signal()
