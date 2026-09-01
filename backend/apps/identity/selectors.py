"""Public reads / scoped querysets for `identity` (architecture.md §1.2).

Other apps read `identity` rows through this module. They must never import
`apps.identity.api` — that package is the HTTP surface and is private to this app.

Empty by design: this pass builds schema only.
"""
