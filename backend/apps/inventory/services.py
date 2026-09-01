"""Public write API for `inventory` (architecture.md §1.2).

Projects, buildings, properties, units, listings, media.

This is the ONLY module another app may import to mutate `inventory`-owned rows.
Calling `Model.objects.create/update/delete` on a `inventory` model from another
app is a forbidden pattern, as is reacting to `post_save` signals to do it.

Empty by design: this pass builds schema only. See the plan's "out of scope".
"""
