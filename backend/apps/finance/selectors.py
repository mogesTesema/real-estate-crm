"""Public reads / scoped querysets for `finance` (architecture.md §1.2).

Other apps read `finance` rows through this module. They must never import
`apps.finance.api` — that package is the HTTP surface and is private to this app.

Empty by design: this pass builds schema only.
"""
