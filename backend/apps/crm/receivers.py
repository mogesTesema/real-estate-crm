"""`crm` answers `identity`'s portal-eligibility question for buyers and sellers.

`identity` owns the portal profile but may not import `crm` (architecture.md §1.2), so it
asks and this module answers. The dependency points the legal way — `crm` imports `identity`,
never the reverse — which is what keeps `lint-imports` green.

Connected in `apps/crm/apps.py::ready()`.
"""
import logging

from apps.identity.signals import verify_portal_eligibility

from .models import Transaction

logger = logging.getLogger(__name__)

#: A transaction that entitles its client to a portal login.
#:
#: SRS 3.11.2 grants access on "a completed contract (closed sale, active/signed lease, or
#: other closed lead/transaction type)—not to open/unconverted leads". CONTRACTED is the
#: signature moment; PARTIALLY_PAID and COMPLETED are strictly later states of the same
#: contract, so excluding them would lock out a buyer who is mid-instalment — exactly the
#: person who most needs to watch their closing status.
#:
#: PENDING is the open case the SRS names, and CANCELLED undoes the contract. Both refuse.
ELIGIBLE_TRANSACTION_STATUSES = frozenset(
    {
        Transaction.Status.CONTRACTED,
        Transaction.Status.PARTIALLY_PAID,
        Transaction.Status.COMPLETED,
    }
)


def _contact_snapshot(contact) -> dict:
    """The identity details `identity` needs to build the login.

    `identity` cannot import `contacts`, but `crm` can — so the app that verified the contract
    also vouches for who the client is, in the same round trip.
    """
    return {
        "eligible": True,
        "email": contact.email,
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or contact.company_name or "",
    }


def verify_transaction_eligibility(
    sender, *, contact_id, portal_type, contract_ref_type, contract_ref_id, **kwargs
):
    """Answer for BUYER and SELLER, which are anchored on a `crm.Transaction`.

    Returns None for anything this app does not own — silence is how a receiver declines,
    and `identity` treats the absence of a positive answer as a refusal.
    """
    if contract_ref_type != "TRANSACTION":
        return None
    if portal_type not in ("BUYER", "SELLER"):
        return None

    transaction = (
        Transaction.objects.filter(pk=contract_ref_id)
        .select_related("deal", "deal__primary_contact", "property")
        .first()
    )
    if transaction is None or transaction.status not in ELIGIBLE_TRANSACTION_STATUSES:
        return None

    # The contact must be party to *this* contract, not merely exist. A Transaction has no
    # direct contact FK, so there are two possible paths:
    #
    #   the deal's primary contact — `crm.Deal` holds one contact, and which side of the sale
    #     they are on depends on whether it is a buy-side or a sell-side mandate. So it is a
    #     valid path for BUYER and SELLER alike. `Transaction.deal` is nullable, and a
    #     transaction recorded without one can confirm nobody.
    #
    #   the property's owner — reaches a seller who is not the deal's client at all, which is
    #     the ordinary shape of a buy-side deal. Only meaningful for SELLER: owning the
    #     property is not evidence of buying it.
    contact = _contact_on_deal(transaction, contact_id)

    if contact is None and portal_type == "SELLER":
        owner_link = (
            transaction.property.owners.filter(contact_id=contact_id)
            .select_related("contact")
            .first()
        )
        if owner_link is not None:
            contact = owner_link.contact

    if contact is None:
        return None
    return _contact_snapshot(contact)


def _contact_on_deal(transaction, contact_id):
    if transaction.deal is None:
        logger.info(
            "Portal eligibility: transaction %s has no deal, so it can confirm no contact "
            "by that path.",
            transaction.pk,
        )
        return None
    if str(transaction.deal.primary_contact_id) == str(contact_id):
        return transaction.deal.primary_contact
    return None


def connect():
    verify_portal_eligibility.connect(
        verify_transaction_eligibility,
        dispatch_uid="crm.verify_transaction_eligibility",
    )
