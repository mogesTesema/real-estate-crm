"""Public reads / scoped querysets for `property_ops` (architecture.md §1.2).

Other apps read `property_ops` rows through this module. They must never import
`apps.property_ops.api` — that package is the HTTP surface and is private to this app.

Empty by design: this pass builds schema only.
"""
