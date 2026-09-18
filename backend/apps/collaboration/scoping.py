"""How `collaboration` rows are scoped (architecture.md §2).

Registered from `CollaborationConfig.ready()`. Grows per aggregate as its endpoints land;
`notification` arrives with the notify engine.
"""
from apps.identity.scoping import owned_by, register


def register_resources():
    from apps.identity.models import Role

    from .models import Notification

    scope = Role.DataScope

    # A notification feed is the one resource where every scope — portal clients included —
    # gets exactly the same rule: your own rows, nothing else. A manager has no business in
    # a report's feed; the feed is a private inbox, not a record.
    register(
        "notification",
        model=Notification,
        entity_type=None,
        scopes={s: owned_by("recipient") for s in scope.values},
    )
