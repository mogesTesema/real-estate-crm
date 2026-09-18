"""Public reads / scoped querysets for `contacts` (architecture.md §1.2).

Other apps read `contacts` rows through this module. They must never import
`apps.contacts.api` — that package is the HTTP surface and is private to this app.
"""
from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import F, Q, Value
from django.db.models.functions import Coalesce, Concat

from apps.identity.selectors import apply_scope

from .models import Contact
from .normalization import normalize_email, normalize_national_id, normalize_phone


def live_contacts():
    """Every contact that has not been soft-deleted. Unscoped — callers must scope."""
    return Contact.objects.filter(deleted_at__isnull=True)


def visible_contacts(user):
    """The contacts `user` may see (architecture.md §2)."""
    return apply_scope(live_contacts(), user, "contact")


def search_contacts(user, term):
    """Free-text search over the scoped set (SRS 5.1).

    `icontains` rather than `startswith`: the GIN trigram indexes added in v3.3 serve infix
    ILIKE natively, which is what a CRM search box is actually used for. A prefix-only search
    that misses "Al Maktoum" when the user types "Maktoum" reads as missing data.
    """
    if not term:
        return visible_contacts(user)
    return visible_contacts(user).filter(
        Q(first_name__icontains=term)
        | Q(last_name__icontains=term)
        | Q(company_name__icontains=term)
        | Q(email__icontains=term)
        | Q(phone__icontains=term)
    )


#: What a duplicate match tells the caller. `visible` decides how much of it they get.
def _match(contact, *, matched_on, score, visible_ids):
    """Describe one candidate duplicate, revealing only what the caller is entitled to.

    **The tension this resolves.** De-duplication has to search the whole book — a scoped
    lookup would hide the contact another agent already owns and create the second record that
    SRS 3.1.10/3.1.11 exist to prevent. But returning that contact in full would make
    `GET /contacts/duplicates/` a way to read any record in the company by guessing a phone
    number.

    So the *existence* of a match is always reported, along with who to ask; the record itself
    is only returned when `apply_scope` would have returned it anyway. That is the minimum
    disclosure that still stops duplicate outreach.
    """
    in_scope = contact.pk in visible_ids
    payload = {
        "matched_on": matched_on,
        "score": round(score, 3),
        "in_scope": in_scope,
        "id": str(contact.pk) if in_scope else None,
    }
    if in_scope:
        payload["display_name"] = str(contact)
        payload["email"] = contact.email
        payload["phone"] = contact.phone
    else:
        agent = contact.assigned_agent
        payload["display_name"] = _masked(contact)
        payload["owned_by"] = agent.full_name if agent else None
        payload["hint"] = (
            "This person is already in the CRM but outside your access. "
            "Ask the owning agent, or request access, rather than creating a second record."
        )
    return payload


def _masked(contact):
    """Enough to recognise the person you were about to add, not enough to profile them."""
    if contact.contact_type == Contact.ContactType.COMPANY:
        name = contact.company_name or ""
        return f"{name[:2]}…" if name else "…"
    first = (contact.first_name or "").strip()
    last = (contact.last_name or "").strip()
    return " ".join(filter(None, [first, f"{last[:1]}." if last else ""])) or "…"


def find_duplicates(
    user,
    *,
    email=None,
    phone=None,
    national_id=None,
    first_name=None,
    last_name=None,
    company_name=None,
    exclude_id=None,
    limit=10,
):
    """Candidate duplicates for a contact about to be created (SRS 3.1.3, 3.1.10, 3.2.7).

    Exact matches on the three normalised identifiers first, then trigram-similar names above
    `CONTACT_SIMILARITY_THRESHOLD`. Exact matches sort first and are never crowded out by
    fuzzy ones.
    """
    email = normalize_email(email)
    phone = normalize_phone(phone)
    national_id = normalize_national_id(national_id)

    candidates = live_contacts()
    if exclude_id:
        candidates = candidates.exclude(pk=exclude_id)

    exact_filter = Q(pk__in=[])
    matched_field = {}
    for field, value in (("email", email), ("phone", phone), ("national_id", national_id)):
        if value:
            exact_filter |= Q(**{field: value})
            matched_field[field] = value

    results, seen = [], set()

    if matched_field:
        for contact in candidates.filter(exact_filter).select_related("assigned_agent")[:limit]:
            on = next(
                field
                for field, value in matched_field.items()
                if getattr(contact, field) == value
            )
            results.append((contact, on, 1.0))
            seen.add(contact.pk)

    name = " ".join(filter(None, [first_name, last_name])) or company_name
    if name and len(results) < limit:
        threshold = getattr(settings, "CONTACT_SIMILARITY_THRESHOLD", 0.4)
        similar = (
            candidates.exclude(pk__in=seen)
            .annotate(
                full_name=Concat(
                    Coalesce(F("first_name"), Value("")),
                    Value(" "),
                    Coalesce(F("last_name"), Value("")),
                    Value(" "),
                    Coalesce(F("company_name"), Value("")),
                ),
            )
            .annotate(similarity=TrigramSimilarity("full_name", name))
            .filter(similarity__gte=threshold)
            .select_related("assigned_agent")
            .order_by("-similarity")[: limit - len(results)]
        )
        results.extend((contact, "name", contact.similarity) for contact in similar)

    # One extra query, deliberately: resolving scope for the handful of candidates found is
    # far cheaper than scoping the whole table and then losing the out-of-scope matches that
    # are the entire reason this function is unscoped.
    ids = [contact.pk for contact, _, _ in results]
    visible_ids = set(
        apply_scope(Contact.objects.filter(pk__in=ids), user, "contact").values_list(
            "pk", flat=True
        )
    )
    return [
        _match(contact, matched_on=on, score=score, visible_ids=visible_ids)
        for contact, on, score in results
    ]
