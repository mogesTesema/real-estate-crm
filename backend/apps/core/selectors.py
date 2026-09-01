"""Public reads / scoped querysets for `core` (architecture.md §1.2).

Other apps read `core` rows through this module. They must never import
`apps.core.api` — that package is the HTTP surface and is private to this app.

Empty by design: this pass builds schema only.
"""
