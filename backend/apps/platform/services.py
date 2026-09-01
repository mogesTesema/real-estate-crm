"""Public write API for `platform` (architecture.md §1.2).

Audit, integrations, reports and analytics.

This is the ONLY module another app may import to mutate `platform`-owned rows.
Calling `Model.objects.create/update/delete` on a `platform` model from another
app is a forbidden pattern, as is reacting to `post_save` signals to do it.

Empty by design: this pass builds schema only. See the plan's "out of scope".
"""
