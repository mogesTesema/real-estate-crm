"""Public write API for `collaboration` (architecture.md §1.2).

Documents, e-sign, activities, communications, notifications.

This is the ONLY module another app may import to mutate `collaboration`-owned rows.
Calling `Model.objects.create/update/delete` on a `collaboration` model from another
app is a forbidden pattern, as is reacting to `post_save` signals to do it.

Empty by design: this pass builds schema only. See the plan's "out of scope".
"""
