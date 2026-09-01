"""HTTP surface for `crm`: views and serializers.

NEVER imported by another app — architecture.md §1.2 lists "importing another
app's api / views / serializers" as a forbidden pattern, and the import-linter
contracts in CI enforce it.

Empty by design: this pass builds schema only.
"""
