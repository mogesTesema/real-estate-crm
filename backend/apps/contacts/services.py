"""
Contact de-duplication and merge (SRS §3.1.3, §3.1.10, §3.2.7).

Duplicate detection matches on normalized phone, normalized email, or an exact
name match — configurable emphasis can be layered on later. Merge reassigns the
duplicate's roles to the survivor and soft-deletes the duplicate, keeping an audit
trail (the survivor and duplicate ids are logged via AuditLog on save/delete).
"""
from django.db import transaction
from django.db.models import Q

from .models import Contact, ContactRole, normalize_email, normalize_phone


def find_duplicates(*, email: str = "", phone: str = "", name: str = "", exclude_id=None):
    """Return contacts in the current tenant that look like duplicates."""
    email_n = normalize_email(email)
    phone_n = normalize_phone(phone)
    q = Q()
    if email_n:
        q |= Q(email=email_n)
    if phone_n:
        q |= Q(phone=phone_n)
    if name:
        q |= Q(full_name__iexact=name.strip())
    if not q:
        return Contact.objects.none()
    qs = Contact.objects.filter(q)
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return qs


@transaction.atomic
def merge_contacts(*, survivor: Contact, duplicate: Contact) -> Contact:
    """Fold `duplicate` into `survivor`. Both must belong to the same tenant."""
    if survivor.tenant_id != duplicate.tenant_id:
        raise ValueError("Cannot merge contacts across tenants.")
    if survivor.pk == duplicate.pk:
        raise ValueError("Cannot merge a contact into itself.")

    # Move roles the survivor doesn't already have.
    existing = set(survivor.contact_roles.values_list("role", flat=True))
    for role in duplicate.contact_roles.all():
        if role.role not in existing:
            ContactRole.objects.create(
                tenant_id=survivor.tenant_id,
                contact=survivor,
                role=role.role,
                is_active=role.is_active,
            )

    # Absorb contact points we don't already carry.
    merged_emails = set(survivor.extra_emails) | set(duplicate.extra_emails)
    if duplicate.email and duplicate.email != survivor.email:
        merged_emails.add(duplicate.email)
    survivor.extra_emails = sorted(e for e in merged_emails if e)

    merged_phones = set(survivor.extra_phones) | set(duplicate.extra_phones)
    if duplicate.phone and duplicate.phone != survivor.phone:
        merged_phones.add(duplicate.phone)
    survivor.extra_phones = sorted(p for p in merged_phones if p)
    survivor.save()

    # Re-point owned records (leads) at the survivor.
    duplicate.leads.update(contact=survivor)

    duplicate.soft_delete()
    return survivor
