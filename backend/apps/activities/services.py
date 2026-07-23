"""Helper for writing to the unified timeline (SRS §3.2.4)."""
from django.contrib.contenttypes.models import ContentType

from .models import Activity


def log_activity(*, tenant_id, actor, activity_type: str, body: str = "", related=None):
    ct = None
    obj_id = None
    if related is not None:
        ct = ContentType.objects.get_for_model(related.__class__)
        obj_id = str(related.pk)
    return Activity.objects.create(
        tenant_id=tenant_id,
        actor=actor,
        activity_type=activity_type,
        body=body,
        content_type=ct,
        object_id=obj_id,
    )
