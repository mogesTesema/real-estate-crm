"""
Signal-based audit logger (SRS §3.17.3, plan §2.6).

Any model registered via ``register_audit(Model)`` gets create/update/delete rows
written to AuditLog automatically. Registration happens in each app's AppConfig.ready().
"""
from django.db.models.signals import post_delete, post_save

from .models import AuditLog
from .tenancy import get_current_tenant

_current_user = None  # set by CurrentUserMiddleware-free path; wired in signals below

_AUDITED = set()


def register_audit(model):
    label = model._meta.label
    if label in _AUDITED:
        return
    _AUDITED.add(label)
    post_save.connect(_on_save, sender=model, dispatch_uid=f"audit_save_{label}")
    post_delete.connect(_on_delete, sender=model, dispatch_uid=f"audit_del_{label}")


def _tenant_for(instance):
    return getattr(instance, "tenant_id", None) or get_current_tenant()


def _on_save(sender, instance, created, **kwargs):
    AuditLog.objects.create(
        tenant_id=_tenant_for(instance),
        model_label=sender._meta.label,
        object_id=str(instance.pk),
        action=AuditLog.Action.CREATE if created else AuditLog.Action.UPDATE,
    )


def _on_delete(sender, instance, **kwargs):
    AuditLog.objects.create(
        tenant_id=_tenant_for(instance),
        model_label=sender._meta.label,
        object_id=str(instance.pk),
        action=AuditLog.Action.DELETE,
    )
