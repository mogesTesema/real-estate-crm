"""Template merge — `{{ contact.first_name }}` over a flat, whitelisted context.

Deliberately NOT the Django template engine. Template bodies are authored by users, and
rendering user-authored text through a real template engine is server-side template
injection waiting for a payload — tags, filters and attribute traversal are exactly the
features an attacker wants and a merge field does not need. A regex substitution over an
explicit dict has no reachable surface beyond the keys we put in it.

Unknown variables fail loud (`ValidationError`) at send/generate time rather than rendering
blank: a blank in a contract or an offer letter is worse than an error the sender sees.
"""
import re

from django.core.exceptions import ValidationError
from django.utils import formats, timezone

#: The `{{ dotted.name }}` shape. Whitespace-tolerant; names are dotted identifiers only.
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def build_merge_context(*, contact=None, lead=None, deal=None, property=None, agent=None):
    """The full vocabulary of merge fields, as a flat dict.

    Missing entities simply contribute no keys — the renderer then reports the *specific*
    variable that could not be resolved, which tells the sender what context their template
    needs rather than silently degrading.
    """
    context = {"today": formats.date_format(timezone.localdate())}

    company = _company()
    if company is not None:
        context["company.name"] = company.name

    if contact is not None:
        context.update(
            {
                "contact.first_name": contact.first_name or "",
                "contact.last_name": contact.last_name or "",
                "contact.full_name": str(contact),
                "contact.email": contact.email or "",
                "contact.phone": contact.phone or "",
            }
        )
    if agent is not None:
        context.update(
            {
                "agent.name": agent.full_name,
                "agent.email": agent.email or "",
                "agent.phone": agent.phone or "",
            }
        )
    if lead is not None:
        context["lead.title"] = lead.title
    if deal is not None:
        context.update(
            {"deal.reference": deal.reference_code, "deal.title": deal.title}
        )
    if property is not None:
        context.update(
            {
                "property.title": property.title,
                "property.city": property.city,
            }
        )
    return context


def _company():
    from apps.identity.models import Company

    return Company.objects.first()


def render(body: str, context: dict) -> str:
    """Substitute every placeholder, refusing unknowns."""

    def _sub(match):
        name = match.group(1)
        try:
            return str(context[name])
        except KeyError:
            raise ValidationError(
                {"body": f"Unknown merge variable '{name}'."}
            ) from None

    return _PLACEHOLDER.sub(_sub, body or "")


def declared_variables(body: str) -> set[str]:
    """Every placeholder a template body mentions — powers validation and preview UX."""
    return set(_PLACEHOLDER.findall(body or ""))
