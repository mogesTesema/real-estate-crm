"""Public reads / scoped querysets for `platform` (architecture.md §1.2).

Other apps read `platform` rows through this module. They must never import
`apps.platform.api` — that package is the HTTP surface and is private to this app.

Empty by design: this pass builds schema only.
"""
