"""Models for `crm` (architecture.md §6/§7/§9).

architecture.md §1.2 makes this package layout normative, not optional:

    models/marketing.py
    models/leads.py
    models/pipeline.py
    models/deals.py

Every model is re-exported here so `from apps.crm.models import X` works
regardless of which submodule defines it.
"""
