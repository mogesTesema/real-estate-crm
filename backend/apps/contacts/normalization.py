"""Canonical forms for the identifiers de-duplication matches on.

Pure functions, no database, no side effects — which is why they live here rather than in
`services`. `selectors` needs them to normalise a search term before comparing it against the
stored value, and a read module importing the write API is both a smell and, as it turns out,
a transitive violation of the satellite contract in `pyproject.toml`: `platform.selectors`
reads `contacts.selectors`, so anything `contacts.selectors` imports is something `platform`
imports too.

`services` re-exports all three, so `contacts.services.normalize_phone` keeps working for
callers outside this app.
"""
import phonenumbers
from django.conf import settings


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


