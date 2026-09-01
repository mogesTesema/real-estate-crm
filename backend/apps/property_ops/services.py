"""Public write API for `property_ops` (architecture.md §1.2).

Leases, rent schedules, screening, maintenance, vendors.

This is the ONLY module another app may import to mutate `property_ops`-owned rows.
Calling `Model.objects.create/update/delete` on a `property_ops` model from another
app is a forbidden pattern, as is reacting to `post_save` signals to do it.

Empty by design: this pass builds schema only. See the plan's "out of scope".
"""
