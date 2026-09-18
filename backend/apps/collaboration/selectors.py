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


def my_notifications(user):
    """A user's own feed, newest first. The scoping registry enforces the same rule for
    every caller; this is the convenience spelling."""
    from apps.identity.selectors import apply_scope

    from .models import Notification

    return apply_scope(
        Notification.objects.all(), user, "notification"
    ).order_by("-created_at")


def unread_count(user) -> int:
    return my_notifications(user).filter(is_read=False).count()


def user_can_download_file(user, file) -> bool:
    """May `user` receive this file's bytes?

    A `File` row has no owner-anchor of its own beyond the uploader — its meaning comes from
    whatever wraps it. So access is: you uploaded it, or something you are allowed to see
    carries it. Checked against the wrapping aggregates that exist so far (inventory media);
    documents and call recordings extend this in their own pass.
    """
    from apps.identity.selectors import apply_scope

    if file.uploaded_by_id == user.pk:
        return True

    from apps.inventory.models import Media

    if apply_scope(
        Media.objects.filter(file=file, deleted_at__isnull=True), user, "media"
    ).exists():
        return True

    from apps.identity.models import Role
    from apps.identity.scoping import scopes_for

    return Role.DataScope.ALL in scopes_for(user)
