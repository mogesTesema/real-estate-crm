"""
Celery task: flag leads that breached their follow-up SLA (SRS §3.1.9).

Runs on a Celery Beat schedule with NO request context, so it iterates tenants and
binds each in turn — RLS then scopes each pass to that tenant's rows. (A sweep that
forgot to bind a tenant would simply see nothing, which is the isolation guarantee
working as intended.)
"""
from celery import shared_task
from django.utils import timezone

from apps.core.models import Tenant
from apps.core.tenancy import tenant_context

from .models import Lead


@shared_task
def sweep_sla_breaches() -> int:
    """Mark leads past sla_due_at with no first response as breached."""
    total = 0
    now = timezone.now()
    for tenant_id in Tenant.objects.values_list("id", flat=True):
        with tenant_context(tenant_id):
            breached = Lead.objects.filter(
                sla_breached=False,
                first_response_at__isnull=True,
                sla_due_at__lt=now,
                status__in=[Lead.Status.NEW, Lead.Status.CONTACTED],
            )
            total += breached.update(sla_breached=True)
    return total
