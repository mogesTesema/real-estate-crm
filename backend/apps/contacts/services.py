"""Public write API for `contacts` (architecture.md §1.2).

Buyers, sellers, rental tenants, landlords, vendors, consent.

This is the ONLY module another app may import to mutate `contacts`-owned rows. Calling
`Model.objects.create/update/delete` on a `contacts` model from another app is a forbidden
pattern, as is reacting to `post_save` signals to do it. `crm.services.capture_lead` reaches
this module through `find_or_create_contact`.

**Normalisation happens here, on the way in.** `email` and `phone` are stored canonical —
lower-cased and E.164 respectively — so de-duplication (SRS 3.1.3 / 3.1.10) is an indexed
equality lookup rather than a scan with a per-row transform. Two records entered as
"Sam@Example.COM" and "sam@example.com", or "050 123 4567" and "+971501234567", collapse to
one key at write time; a normaliser applied only at read time would not make them collide.
"""
import csv
import io
import logging

import phonenumbers
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import signals
from .models import Consent, Contact, ContactRelationship, ContactRole

logger = logging.getLogger(__name__)

#: Fields a caller may set through `create_contact` / `update_contact`. Anything else — the
#: audit columns, `deleted_at` — is this module's to manage, not the caller's.
WRITABLE_FIELDS = frozenset(
    {
        "contact_type",
        "first_name",
        "middle_name",
        "last_name",
        "company_name",
        "email",
        "secondary_email",
        "phone",
        "secondary_phone",
        "date_of_birth",
        "national_id",
        "tax_number",
        "preferred_language",
        "preferred_contact_method",
        "address_line_1",
        "address_line_2",
        "city",
        "state",
        "country",
        "postal_code",
        "latitude",
        "longitude",
        "assigned_agent",
        "default_source",
        "notes",
        "custom_data",
        "is_active",
    }
)


# --- Normalisation ------------------------------------------------------------------------


def normalize_email(value):
    """Canonical form of an address: trimmed and lower-cased, or None.

    The whole address, not only the domain. `BaseUserManager.normalize_email` lower-cases the
    domain alone — correct to the letter of the RFC, where the local part is case-sensitive,
    and wrong for de-duplication, where no real mail provider treats "Sam@" and "sam@" as two
    people. `identity` learned this the same way.
    """
    if not value:
        return None
    value = value.strip().lower()
    return value or None


def normalize_phone(value, region=None):
    """E.164 form of a number, or a best-effort fallback.

    Deliberately **never raises**. A CSV of ten years of legacy contacts will contain numbers
    that no parser accepts, and refusing the row loses the contact to keep the format tidy.
    An unparseable number is stored stripped of separators so at least identical strings still
    collide; `+` is preserved because its presence is the one signal that a number is already
    international.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    region = region or getattr(settings, "DEFAULT_PHONE_REGION", "AE")
    try:
        parsed = phonenumbers.parse(raw, region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except phonenumbers.NumberParseException:
        pass
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    return f"+{digits}" if raw.lstrip().startswith("+") else digits


def normalize_national_id(value):
    """Identity documents are quoted with and without separators; the digits are the key."""
    if not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isalnum()).upper()
    return cleaned or None


def _normalized(fields):
    """Apply the three normalisers to whichever of their fields are present."""
    out = dict(fields)
    if "email" in out:
        out["email"] = normalize_email(out["email"])
    if "secondary_email" in out:
        out["secondary_email"] = normalize_email(out["secondary_email"])
    if "phone" in out:
        out["phone"] = normalize_phone(out["phone"])
    if "secondary_phone" in out:
        out["secondary_phone"] = normalize_phone(out["secondary_phone"])
    if "national_id" in out:
        out["national_id"] = normalize_national_id(out["national_id"])
    return out


# --- Contacts -----------------------------------------------------------------------------


def _reject_unknown(fields):
    unknown = set(fields) - WRITABLE_FIELDS
    if unknown:
        raise ValidationError(
            {name: "This field cannot be set through the contacts service." for name in unknown}
        )


def _validate_identity(contact_type, fields, *, existing=None):
    """A person needs a name; a company needs a company name (SRS 3.2.1).

    Checked here rather than as a CHECK constraint because the rule spans two columns whose
    emptiness is a blank string in some imports and NULL in others, and because the message a
    user sees matters more than the guarantee is worth at this level.
    """

    def value(name):
        if name in fields:
            return fields[name]
        return getattr(existing, name, None)

    if contact_type == Contact.ContactType.COMPANY:
        if not (value("company_name") or "").strip():
            raise ValidationError({"company_name": "A company contact needs a company name."})
    else:
        first, last = (value("first_name") or "").strip(), (value("last_name") or "").strip()
        if not (first or last):
            raise ValidationError(
                {"last_name": "A person needs at least a first or last name."}
            )


@transaction.atomic
def create_contact(*, actor, roles=(), **fields):
    """Create a contact. `roles` is the set of hats they wear (SRS 3.2.2)."""
    _reject_unknown(fields)
    fields = _normalized(fields)
    contact_type = fields.get("contact_type") or Contact.ContactType.PERSON
    fields["contact_type"] = contact_type
    _validate_identity(contact_type, fields)

    contact = Contact.objects.create(created_by=actor, updated_by=actor, **fields)
    if roles:
        set_roles(contact, roles, actor=actor)
    return contact


@transaction.atomic
def update_contact(contact, *, actor, roles=None, **fields):
    """Patch a contact. Only the fields passed are touched."""
    _reject_unknown(fields)
    fields = _normalized(fields)
    contact_type = fields.get("contact_type", contact.contact_type)
    _validate_identity(contact_type, fields, existing=contact)

    for name, value in fields.items():
        setattr(contact, name, value)
    contact.updated_by = actor
    contact.save(update_fields=[*fields, "updated_by", "updated_at"] if fields else None)

    if roles is not None:
        set_roles(contact, roles, actor=actor)
    return contact


@transaction.atomic
def delete_contact(contact, *, actor):
    """Soft-delete. Never a row deletion: leads, deals and leases PROTECT this row, and the
    history they carry is the point of keeping it."""
    if contact.deleted_at:
        return contact
    contact.deleted_at = timezone.now()
    contact.is_active = False
    contact.updated_by = actor
    contact.save(update_fields=["deleted_at", "is_active", "updated_by", "updated_at"])
    signals.contact_deleted.send_robust(sender=None, contact=contact, actor=actor)
    return contact


def set_roles(contact, roles, *, actor=None):
    """Replace a contact's role set (SRS 3.2.2).

    A *set*, not a column: the same person is a buyer today and a past seller at the same
    time, and collapsing that into one `role` field is the modelling mistake this table exists
    to avoid.
    """
    valid = set(ContactRole.Role.values)
    roles = {r.strip().upper() for r in roles if r and r.strip()}
    unknown = roles - valid
    if unknown:
        raise ValidationError({"roles": f"Unknown contact roles: {sorted(unknown)}"})

    existing = set(contact.roles.values_list("role", flat=True))
    contact.roles.filter(role__in=existing - roles).delete()
    ContactRole.objects.bulk_create(
        [ContactRole(contact=contact, role=role) for role in roles - existing]
    )
    return roles


def add_relationship(*, from_contact, to_contact, relationship_type, actor=None, notes=None):
    """Link two contacts — household, company representative, referral source (SRS 3.2.5)."""
    if from_contact.pk == to_contact.pk:
        raise ValidationError("A contact cannot be related to themselves.")
    if relationship_type not in set(ContactRelationship.RelationshipType.values):
        raise ValidationError({"relationship_type": "Unknown relationship type."})
    return ContactRelationship.objects.create(
        from_contact=from_contact,
        to_contact=to_contact,
        relationship_type=relationship_type,
        notes=notes,
    )


def record_consent(*, contact, channel, status, source, actor=None, evidence=None):
    """Record or supersede a per-channel consent decision (SRS 5.5, 3.9).

    Withdrawal is a new row with `withdrawn_at` set, not an edit of the grant: the evidence
    that consent *was* given has to survive its withdrawal, or there is nothing to show a
    regulator asking why the contact was ever mailed.
    """
    if channel not in set(Consent.Channel.values):
        raise ValidationError({"channel": "Unknown consent channel."})
    if status not in set(Consent.Status.values):
        raise ValidationError({"status": "Unknown consent status."})

    now = timezone.now()
    return Consent.objects.create(
        contact=contact,
        channel=channel,
        status=status,
        source=source,
        evidence=evidence,
        consented_at=now if status == Consent.Status.OPTED_IN else None,
        withdrawn_at=now if status == Consent.Status.OPTED_OUT else None,
    )


# --- Merge (SRS 3.2.7) --------------------------------------------------------------------

#: Fields the survivor inherits from the duplicate when the survivor's own value is blank.
#: `custom_data` is merged key-wise instead; `assigned_agent` is deliberately absent, because
#: a merge must not silently reassign a contact away from the agent working them.
_INHERITABLE = tuple(
    sorted(
        WRITABLE_FIELDS
        - {"custom_data", "assigned_agent", "contact_type", "is_active"}
    )
)


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _repoint(relation, duplicate, survivor):
    """Move one relation's rows from `duplicate` to `survivor`, returning how many moved.

    Two things make this more than an `.update()`:

    * **Unique constraints.** `contacts_contact_role` is UNIQUE(contact, role); if both
      records are marked BUYER, repointing the duplicate's row collides. The colliding row is
      redundant by definition — the survivor already holds that fact — so it is dropped. The
      bulk update is tried first and the per-row fallback only runs when it fails, so the
      common case stays one statement.
    * **Savepoints.** The caller is inside `transaction.atomic()`, so catching an IntegrityError
      without a savepoint would leave Postgres in an aborted transaction and the caller's own
      COMMIT would fail. Each attempt gets its own.
    """
    field = relation.field
    model = relation.related_model
    rows = model._default_manager.filter(**{field.name: duplicate})

    try:
        with transaction.atomic():
            return rows.update(**{field.name: survivor})
    except IntegrityError:
        pass

    moved = 0
    for row in list(rows):
        setattr(row, field.name, survivor)
        try:
            with transaction.atomic():
                row.save(update_fields=[field.attname])
                moved += 1
        except IntegrityError:
            with transaction.atomic():
                row.delete()
    return moved


def _repoint_record_shares(duplicate, survivor):
    """Explicit shares point at a bare UUID with no foreign key, so nothing moves them.

    Left behind, a share would name a soft-deleted contact — the grant silently evaporates for
    whoever was given it. `contacts -> identity` is a legal import direction, so this is a
    direct call rather than another signal.
    """
    from apps.core.choices import ScopedEntityType
    from apps.identity.models import RecordShare

    moved = 0
    for share in RecordShare.objects.filter(
        entity_type=ScopedEntityType.CONTACT, entity_id=duplicate.pk
    ):
        share.entity_id = survivor.pk
        try:
            with transaction.atomic():
                share.save(update_fields=["entity_id"])
                moved += 1
        except IntegrityError:
            # That user already holds a share on the survivor. Redundant, so drop it.
            with transaction.atomic():
                share.delete()
    return moved


@transaction.atomic
def merge_contacts(*, survivor, duplicate, actor):
    """Fold `duplicate` into `survivor` (SRS 3.2.7).

    Walks `Contact._meta.related_objects` rather than naming relations. The previous
    implementation repointed `leads` and nothing else, which silently orphaned every other
    child — and, worse, would have gone on silently orphaning each new relation added by each
    new module. Reflection means a relation added in a later pass is carried automatically.

    The duplicate is soft-deleted, not removed: it is the row a PROTECT foreign key may still
    point at from an append-only table, and `custom_data["merged_into"]` leaves a trail from
    the dead id to the live one for anyone holding a stale reference.
    """
    if survivor.pk == duplicate.pk:
        raise ValidationError("A contact cannot be merged into itself.")
    if duplicate.deleted_at:
        raise ValidationError("That contact has already been merged or deleted.")
    if survivor.deleted_at:
        raise ValidationError("Cannot merge into a deleted contact.")

    # Lock both rows in a deterministic order. Two merges running in opposite directions over
    # the same pair would otherwise deadlock.
    ordered = sorted([survivor.pk, duplicate.pk], key=str)
    list(Contact.objects.select_for_update().filter(pk__in=ordered).order_by("id"))

    moved = {}
    for relation in Contact._meta.related_objects:
        field = relation.field
        if relation.one_to_one:
            # A one-to-one child cannot be held twice. The portal profile is the case that
            # matters: two logins for one person is exactly what the merge is undoing, and
            # silently discarding one of them would revoke somebody's access without a word.
            if (
                field.model._default_manager.filter(**{field.name: survivor}).exists()
                and field.model._default_manager.filter(**{field.name: duplicate}).exists()
            ):
                raise ValidationError(
                    f"Both contacts have a {field.model._meta.verbose_name}. "
                    f"Resolve it before merging."
                )
        count = _repoint(relation, duplicate, survivor)
        if count:
            moved[f"{relation.related_model._meta.label}.{field.name}"] = count

    shares = _repoint_record_shares(duplicate, survivor)
    if shares:
        moved["identity.RecordShare.entity_id"] = shares

    # A relationship may now point at the survivor from both ends. Self-edges are meaningless.
    ContactRelationship.objects.filter(
        from_contact=survivor, to_contact=survivor
    ).delete()

    # Fill the survivor's gaps from the duplicate. Never overwrite: the survivor is the record
    # the operator chose to keep, so their values win wherever they have one.
    filled = []
    for name in _INHERITABLE:
        if _blank(getattr(survivor, name, None)):
            value = getattr(duplicate, name, None)
            if not _blank(value):
                setattr(survivor, name, value)
                filled.append(name)
    merged_custom = {**(duplicate.custom_data or {}), **(survivor.custom_data or {})}
    if merged_custom != survivor.custom_data:
        survivor.custom_data = merged_custom
        filled.append("custom_data")
    if filled:
        survivor.updated_by = actor
        survivor.save(update_fields=[*filled, "updated_by", "updated_at"])

    duplicate.deleted_at = timezone.now()
    duplicate.is_active = False
    duplicate.updated_by = actor
    duplicate.custom_data = {
        **(duplicate.custom_data or {}),
        "merged_into": str(survivor.pk),
        "merged_at": timezone.now().isoformat(),
    }
    duplicate.save(
        update_fields=["deleted_at", "is_active", "custom_data", "updated_by", "updated_at"]
    )

    signals.contacts_merged.send_robust(
        sender=None, survivor=survivor, duplicate=duplicate, actor=actor, moved=moved
    )
    survivor.refresh_from_db()
    return survivor


# --- Capture path, shared with crm ---------------------------------------------------------


def find_or_create_contact(*, actor, roles=(), **fields):
    """Return an existing contact matching on a normalised identifier, or create one.

    The entry point `crm.services.capture_lead` uses, which is why it lives here and not in
    `crm`: §1.2 makes this module the only way another app may create a contacts row.

    **Deliberately unscoped.** The point of de-duplication is to stop two agents independently
    working the same prospect (SRS 3.1.10 / 3.1.11); a scoped lookup would hide the other
    agent's contact and create the second record it exists to prevent. The *caller's* view of
    that contact is still scoped — `apply_scope` decides what the endpoint returns, and
    `selectors.find_duplicates` masks a match the caller may not see.
    """
    fields = _normalized(fields)
    match = None
    for key in ("email", "phone", "national_id"):
        value = fields.get(key)
        if value:
            match = (
                Contact.objects.filter(deleted_at__isnull=True, **{key: value})
                .order_by("created_at")
                .first()
            )
            if match:
                break
    if match:
        if roles:
            existing = set(match.roles.values_list("role", flat=True))
            set_roles(match, existing | {r.upper() for r in roles}, actor=actor)
        return match, False
    return create_contact(actor=actor, roles=roles, **fields), True


# --- CSV import / export (SRS 3.2.6, 3.13.4) ----------------------------------------------

#: Column keys an import may map onto, in the order the export writes them.
IMPORTABLE_FIELDS = (
    "contact_type",
    "first_name",
    "middle_name",
    "last_name",
    "company_name",
    "email",
    "secondary_email",
    "phone",
    "secondary_phone",
    "national_id",
    "tax_number",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "country",
    "postal_code",
    "preferred_language",
    "preferred_contact_method",
    "notes",
)


def read_csv(uploaded_file):
    """Decode an uploaded CSV into (headers, rows-as-dicts).

    Tries UTF-8 with a BOM first: a file saved from Excel starts with one, and reading it as
    plain UTF-8 puts an invisible \\ufeff on the first header, so the mapping for that column
    silently never matches.
    """
    raw = uploaded_file.read()
    if isinstance(raw, str):
        raw = raw.encode()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 decodes any byte string
        raise ValidationError({"file": "Could not decode the file as text."})

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValidationError({"file": "The file has no header row."})
    headers = [name.strip() for name in reader.fieldnames]
    rows = [
        {(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader
    ]
    return headers, rows


def suggest_mapping(headers):
    """Guess which CSV column feeds which field, so the operator confirms rather than types."""
    suggestion = {}
    normalized = {h.lower().replace(" ", "_").replace("-", "_"): h for h in headers}
    aliases = {
        "first_name": ("first_name", "firstname", "given_name", "first"),
        "last_name": ("last_name", "lastname", "surname", "family_name", "last"),
        "company_name": ("company_name", "company", "organisation", "organization"),
        "email": ("email", "email_address", "e_mail"),
        "phone": ("phone", "mobile", "phone_number", "telephone", "contact_number"),
        "secondary_phone": ("secondary_phone", "alt_phone", "phone_2", "other_phone"),
        "national_id": ("national_id", "emirates_id", "id_number", "passport"),
        "city": ("city", "town"),
        "country": ("country",),
        "notes": ("notes", "note", "comments", "remarks"),
    }
    for field in IMPORTABLE_FIELDS:
        for candidate in aliases.get(field, (field,)):
            if candidate in normalized:
                suggestion[field] = normalized[candidate]
                break
    return suggestion


def import_contacts(rows, *, mapping, actor, roles=(), commit=False, filename=""):
    """Classify every row as new, duplicate or invalid — and only then, optionally, write.

    Returns the report whether or not it commits, so the operator sees exactly what a commit
    would do before it does it (SRS 3.2.6's "dry-run report"). `commit=False` runs the identical
    code path; the difference is one transaction that gets rolled back, not a second
    implementation that might disagree with the first.

    Matching reuses the same normalised identifiers as live capture, so a CSV cannot introduce
    a duplicate that the API would have caught.
    """
    max_rows = getattr(settings, "CONTACT_IMPORT_MAX_ROWS", 5000)
    if len(rows) > max_rows:
        raise ValidationError(
            {"file": f"{len(rows)} rows exceeds the {max_rows}-row import limit."}
        )
    unknown = set(mapping) - set(IMPORTABLE_FIELDS)
    if unknown:
        raise ValidationError({"mapping": f"Unknown target fields: {sorted(unknown)}"})

    report = {"total": len(rows), "created": [], "duplicates": [], "invalid": []}
    # Identifiers seen earlier in this same file. Without it, a CSV containing the same person
    # twice creates them twice — the database has not been written yet in dry-run mode, so
    # nothing else would catch it.
    seen = {"email": {}, "phone": {}, "national_id": {}}

    with transaction.atomic():
        for index, row in enumerate(rows, start=2):  # start=2: row 1 is the header
            fields = {
                field: row.get(column, "")
                for field, column in mapping.items()
                if row.get(column, "") != ""
            }
            if not fields:
                report["invalid"].append({"row": index, "errors": {"row": "Empty row."}})
                continue

            fields.setdefault("contact_type", Contact.ContactType.PERSON)
            normalized = _normalized(fields)

            clash = next(
                (
                    {"key": key, "row": seen[key][normalized[key]]}
                    for key in seen
                    if normalized.get(key) and normalized[key] in seen[key]
                ),
                None,
            )
            if clash:
                report["duplicates"].append(
                    {
                        "row": index,
                        "matched": f"row {clash['row']} of this file",
                        "matched_on": clash["key"],
                    }
                )
                continue

            existing = next(
                (
                    found
                    for key in ("email", "phone", "national_id")
                    if normalized.get(key)
                    for found in [
                        Contact.objects.filter(
                            deleted_at__isnull=True, **{key: normalized[key]}
                        ).first()
                    ]
                    if found
                ),
                None,
            )
            if existing:
                report["duplicates"].append(
                    {"row": index, "matched": str(existing.pk), "matched_on": "database"}
                )
                continue

            try:
                with transaction.atomic():
                    contact = create_contact(actor=actor, roles=roles, **normalized)
            except ValidationError as exc:
                report["invalid"].append(
                    {
                        "row": index,
                        "errors": exc.message_dict
                        if hasattr(exc, "message_dict")
                        else {"row": exc.messages},
                    }
                )
                continue

            for key in seen:
                if normalized.get(key):
                    seen[key][normalized[key]] = index
            report["created"].append({"row": index, "id": str(contact.pk)})

        if not commit:
            transaction.set_rollback(True)

    report["summary"] = {
        "total": report["total"],
        "created": len(report["created"]),
        "duplicates": len(report["duplicates"]),
        "invalid": len(report["invalid"]),
        "committed": bool(commit),
    }
    if commit:
        signals.contacts_imported.send_robust(
            sender=None,
            actor=actor,
            created=len(report["created"]),
            skipped=len(report["duplicates"]),
            invalid=len(report["invalid"]),
            filename=filename,
        )
    return report


def export_rows(queryset, *, actor, fields=IMPORTABLE_FIELDS):
    """Stream a scoped queryset as CSV rows, and record that it happened.

    A generator, so a large export does not build the whole file in memory — and the audit
    signal fires from inside it, after the last row, so the recorded count is what was actually
    sent rather than what was requested.
    """

    def rows():
        yield list(fields)
        count = 0
        for contact in queryset.iterator(chunk_size=500):
            yield [str(getattr(contact, name, "") or "") for name in fields]
            count += 1
        # SRS 5.3 names data export a sensitive action. Recorded after the fact, with the real
        # row count: an export that the client abandoned halfway did not leak the whole table.
        signals.contacts_exported.send_robust(
            sender=None, actor=actor, row_count=count, fields=list(fields)
        )

    return rows()
