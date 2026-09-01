"""Models for `property_ops` (architecture.md §10, §14).

architecture.md §1.2 makes this package layout normative, not optional:

    models/leasing.py
    models/maintenance.py

Every model is re-exported here so `from apps.property_ops.models import X` works regardless
of which submodule defines it.
"""
from .leasing import (
    Application,
    Deposit,
    Inspection,
    Lease,
    LeaseParty,
    Renewal,
    RentSchedule,
)
from .maintenance import MaintenanceRequest, Vendor, WorkOrder

__all__ = [
    "Application",
    "Deposit",
    "Inspection",
    "Lease",
    "LeaseParty",
    "MaintenanceRequest",
    "Renewal",
    "RentSchedule",
    "Vendor",
    "WorkOrder",
]
