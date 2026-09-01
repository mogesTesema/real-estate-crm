"""Public write API for `identity` (architecture.md §1.2).

Company, branches, teams, users, RBAC, portal profiles.

This is the ONLY module another app may import to mutate `identity`-owned rows.
Calling `Model.objects.create/update/delete` on a `identity` model from another
app is a forbidden pattern, as is reacting to `post_save` signals to do it.

Empty by design: this pass builds schema only. See the plan's "out of scope".
"""
