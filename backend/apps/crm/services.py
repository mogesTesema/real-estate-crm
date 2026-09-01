"""Public write API for `crm` (architecture.md §1.2).

Marketing, leads, pipelines, deals, viewings, offers, transactions.

This is the ONLY module another app may import to mutate `crm`-owned rows.
Calling `Model.objects.create/update/delete` on a `crm` model from another
app is a forbidden pattern, as is reacting to `post_save` signals to do it.

Empty by design: this pass builds schema only. See the plan's "out of scope".
"""
