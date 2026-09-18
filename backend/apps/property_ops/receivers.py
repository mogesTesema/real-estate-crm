"""`property_ops` answers `identity`'s portal-eligibility question for tenants and landlords.

Same inversion as `apps/crm/receivers.py`: `identity` owns the portal profile but may not
import this app, so it asks and this module answers.

Connected in `apps/property_ops/apps.py::ready()`.
"""
from apps.identity.signals import verify_portal_eligibility

from .models import Lease, LeaseParty

#: Lease states that entitle a party to a portal login.
#:
#: SRS 3.11.2 says "active/signed lease". `Lease.OCCUPYING_STATUSES` also includes
#: PENDING_SIGNATURE — correct for the double-letting exclusion constraint it was written for,
#: but wrong here: a lease awaiting signature is precisely not signed. DRAFT is likewise not a
#: contract, and TERMINATED/EXPIRED are contracts that have ended — access should stop with
#: them, which is what `revoke_portal_access` is for.
ELIGIBLE_LEASE_STATUSES = frozenset(
    {
        Lease.Status.ACTIVE,
        Lease.Status.EXPIRING,
        Lease.Status.RENEWED,
    }
)

#: architecture.md §2 grants the tenant portal to "leases where contact is tenant/**party**",
#: so a co-tenant living under the lease qualifies. A GUARANTOR does not: they are liable for
#: the lease, not a resident of it, and the portal shows a home's maintenance and rent history.
TENANT_PARTY_TYPES = frozenset(
    {LeaseParty.PartyType.TENANT, LeaseParty.PartyType.CO_TENANT}
)


def _contact_snapshot(contact) -> dict:
    return {
        "eligible": True,
        "email": contact.email,
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or contact.company_name or "",
    }


def verify_lease_eligibility(
    sender, *, contact_id, portal_type, contract_ref_type, contract_ref_id, **kwargs
):
    """Answer for TENANT and LANDLORD, which are anchored on a `property_ops.Lease`."""
    if contract_ref_type != "LEASE":
        return None
    if portal_type not in ("TENANT", "LANDLORD"):
        return None

    lease = (
        Lease.objects.filter(pk=contract_ref_id)
        .select_related("tenant", "landlord")
        .first()
    )
    if lease is None or lease.status not in ELIGIBLE_LEASE_STATUSES:
        return None

    contact = None
    if portal_type == "LANDLORD":
        if str(lease.landlord_id) == str(contact_id):
            contact = lease.landlord
    else:  # TENANT
        if str(lease.tenant_id) == str(contact_id):
            contact = lease.tenant
        else:
            party = (
                LeaseParty.objects.filter(
                    lease=lease,
                    contact_id=contact_id,
                    party_type__in=TENANT_PARTY_TYPES,
                )
                .select_related("contact")
                .first()
            )
            if party is not None:
                contact = party.contact

    if contact is None:
        return None
    return _contact_snapshot(contact)


def connect():
    verify_portal_eligibility.connect(
        verify_lease_eligibility,
        dispatch_uid="property_ops.verify_lease_eligibility",
    )
