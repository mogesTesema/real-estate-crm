"""Public write API for `finance` (architecture.md §1.2).

Accounts, invoices, payments, commissions, expenses, statements.

This is the ONLY module another app may import to mutate `finance`-owned rows.
Calling `Model.objects.create/update/delete` on a `finance` model from another
app is a forbidden pattern, as is reacting to `post_save` signals to do it.

Empty by design: this pass builds schema only. See the plan's "out of scope".
"""
