"""Public write API for `collaboration` (architecture.md §1.2).

Documents, e-signature, activities/calendar, communications, notifications. A package split
by aggregate, re-exporting every public name so `from apps.collaboration import services`
and the import-linter module target `apps.collaboration.services` both resolve unchanged.
"""
from . import files  # noqa: F401 - submodule access (download_target/open_file)
from .activities import (  # noqa: F401
    ACTIVITY_TRANSITIONS,
    change_activity_status,
    close_activity_for_source,
    create_activity,
    create_task_for_lease_expiry,
    create_task_for_stage_move,
    sweep_activity_reminders,
    update_activity,
    upsert_activity_for_source,
)
from .communications import (  # noqa: F401
    assign_thread,
    close_thread,
    create_note,
    create_template,
    log_call,
    record_inbound_message,
    render_template_preview,
    reopen_thread,
    send_message,
    update_message_status,
    update_note,
    update_template,
)
from .documents import (  # noqa: F401
    add_document_version,
    create_document,
    delete_document,
    generate_document_from_template,
    grant_access,
    link_document,
    revoke_access,
    unlink_document,
    update_document,
)
from .esign import (  # noqa: F401
    create_envelope,
    decline,
    record_view,
    remind,
    resolve_token,
    send_envelope,
    sign,
    sign_token,
    void_envelope,
)
from .files import file_sha256, store_file  # noqa: F401
from .notify import (  # noqa: F401
    mark_all_read,
    mark_read,
    notify,
    set_notification_preference,
)
