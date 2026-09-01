"""Public reads / scoped querysets for `inventory` (architecture.md §1.2).

Other apps read `inventory` rows through this module. They must never import
`apps.inventory.api` — that package is the HTTP surface and is private to this app.

Empty by design: this pass builds schema only.
"""
