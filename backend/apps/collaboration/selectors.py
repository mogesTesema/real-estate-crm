"""Public reads / scoped querysets for `collaboration` (architecture.md §1.2).

Other apps read `collaboration` rows through this module. They must never import
`apps.collaboration.api` — that package is the HTTP surface and is private to this app.

Empty by design: this pass builds schema only.
"""
