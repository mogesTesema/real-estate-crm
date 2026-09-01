"""Models for `collaboration` (architecture.md §12/§13/§17/§18).

architecture.md §1.2 makes this package layout normative, not optional:

    models/documents.py
    models/activities.py
    models/communications.py
    models/notifications.py

Every model is re-exported here so `from apps.collaboration.models import X` works
regardless of which submodule defines it.
"""
