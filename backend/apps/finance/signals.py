"""Signals `finance` sends, and `platform` answers.

§1.3: "every sensitive service mutation calls `platform.services.record_event`" — and the
DAG forbids `finance -> platform`, so finance sends and platform receives. Money events are
the reason `AuditEvent.Action` has PAYMENT_POSTED and COMMISSION_APPROVED members.
"""
from django.dispatch import Signal

#: kwargs: invoice, actor
invoice_issued = Signal()

#: kwargs: invoice, actor, reason, old_status
invoice_voided = Signal()

#: kwargs: payment, actor, allocations
payment_posted = Signal()

#: kwargs: payment, actor, reason, refund
payment_reversed = Signal()

#: kwargs: commission, actor
commission_approved = Signal()

#: kwargs: commission, actor, account
commission_paid = Signal()

#: kwargs: cheque, actor, reason
cheque_bounced = Signal()

#: kwargs: statement, actor
statement_issued = Signal()
