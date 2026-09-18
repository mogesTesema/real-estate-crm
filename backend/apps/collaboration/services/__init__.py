"""Public write API for `collaboration` (architecture.md §1.2).

Documents, e-signature, activities/calendar, communications, notifications.

This is the ONLY module another app may import to mutate `collaboration`-owned rows. It is a
package split by aggregate, but the import surface is unchanged: every public function is
re-exported here by name, so `from apps.collaboration import services;
services.upsert_activity_for_source(...)` — the form `crm` already uses — keeps working, and
import-linter's module target `apps.collaboration.services` still resolves.
"""
from . import files  # noqa: F401 - submodule access for download_target/open_file
from .activities import (  # noqa: F401
    close_activity_for_source,
    create_task_for_lease_expiry,
    create_task_for_stage_move,
    upsert_activity_for_source,
)
from .files import file_sha256, store_file  # noqa: F401
from .notify import (  # noqa: F401
    mark_all_read,
    mark_read,
    notify,
    set_notification_preference,
)
