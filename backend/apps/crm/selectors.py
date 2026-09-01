"""Public reads / scoped querysets for `crm` (architecture.md §1.2).

Other apps read `crm` rows through this module. They must never import
`apps.crm.api` — that package is the HTTP surface and is private to this app.

Empty by design: this pass builds schema only.
"""
