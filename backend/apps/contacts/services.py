"""Public write API for `contacts` (architecture.md §1.2).

Buyers, sellers, rental tenants, landlords, vendors, consent.

This is the ONLY module another app may import to mutate `contacts`-owned rows.
Calling `Model.objects.create/update/delete` on a `contacts` model from another
app is a forbidden pattern, as is reacting to `post_save` signals to do it.

Empty by design: this pass builds schema only. See the plan's "out of scope".
"""
