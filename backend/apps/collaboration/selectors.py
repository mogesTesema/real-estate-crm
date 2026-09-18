"""Public reads / scoped querysets for `collaboration` (architecture.md §1.2).

Other apps read `collaboration` rows through this module. They must never import
`apps.collaboration.api` — that package is the HTTP surface and is private to this app.
"""
from .models import Activity


def activity_for_source(source_type, source_id):
    """The one activity mirroring a domain record, or None."""
    return Activity.objects.filter(source_type=source_type, source_id=source_id).first()


def calendar_for(user, *, start=None, end=None):
    """A user's own calendar. Not `apply_scope`d: an activity is assigned *to* someone, and
    "my calendar" is the assignment, not a row-visibility question. A manager reading a
    colleague's diary is a separate endpoint with its own scoping, which arrives with the
    activities pass."""
    qs = Activity.objects.filter(assigned_to=user)
    if start:
        qs = qs.filter(start_at__gte=start)
    if end:
        qs = qs.filter(start_at__lte=end)
    return qs.order_by("start_at")
