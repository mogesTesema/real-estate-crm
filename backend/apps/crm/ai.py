"""AI assistance behind a provider seam (SRS §3.20).

Lives in `crm` (not `platform`) because the features are lead-shaped and platform may not
import `crm.services`. The provider is pluggable: `AI_PROVIDER=mock` (default) is
deterministic — same lead in, same words out — which makes it testable AND honest: it
never hallucinates a fact it did not compute. The Claude API provider is documented in
third-part-needed.md §17; swapping it in is a settings change, not a refactor.
"""
from functools import cache

from django.conf import settings
from django.utils.module_loading import import_string


def get_provider():
    path = getattr(settings, "AI_PROVIDER_CLASS", None)
    if path is None:
        name = getattr(settings, "AI_PROVIDER", "mock")
        path = {"mock": "apps.crm.ai.MockAIProvider"}.get(name, name)
    return _load(path)


@cache
def _load(path):
    return import_string(path)()


class MockAIProvider:
    """Deterministic rule-based provider — the fallback IS the mock.

    Every answer is derived from the rule engine and templates this app already owns, so
    the mock's insights are *true*, just not clever. That is the right floor: a real model
    can only add nuance on top of correct arithmetic.
    """

    # --- lead scoring rationale ---------------------------------------------------------

    def score_lead(self, lead):
        from .services import SOURCE_WEIGHTS, URGENT_TIMEFRAMES, score_lead

        factors = []
        source_type = lead.source.source_type if lead.source_id else None
        if source_type:
            factors.append({
                "factor": "source",
                "value": source_type,
                "weight": SOURCE_WEIGHTS.get(source_type, 0),
            })
        if lead.budget_max:
            factors.append({"factor": "budget_stated", "value": str(lead.budget_max),
                            "weight": 15})
        timeframe = (lead.expected_timeframe or "").lower()
        if any(t in timeframe for t in URGENT_TIMEFRAMES):
            factors.append({"factor": "urgent_timeframe", "value": lead.expected_timeframe,
                            "weight": 20})
        if lead.contact.phone:
            factors.append({"factor": "reachable_by_phone", "value": True, "weight": 10})
        return {
            "score": score_lead(lead),
            "rationale": factors,
            "model": "rules-v1",
        }

    # --- next best action ---------------------------------------------------------------

    def next_best_action(self, lead):
        """First-match decision table. Ordered by urgency, and each row explains itself."""
        from django.utils import timezone

        if lead.sla_breached and lead.first_response_at is None:
            return {
                "action": "CALL_NOW",
                "reason": "The response SLA is breached and nobody has answered yet.",
            }
        if lead.status == "QUALIFIED" and not lead.deleted_at:
            from .selectors import matching_listings_for_lead

            top = matching_listings_for_lead(lead)[:1]
            if top:
                return {
                    "action": "PROPOSE_VIEWING",
                    "reason": "Qualified with a live match on the market.",
                    "listing_id": str(top[0].pk),
                }
        if lead.status == "NEW":
            return {
                "action": "MAKE_CONTACT",
                "reason": "The lead has never been contacted.",
            }
        if lead.updated_at < timezone.now() - timezone.timedelta(days=7):
            return {
                "action": "FOLLOW_UP",
                "reason": "No movement in over a week.",
            }
        return {"action": "NURTURE", "reason": "On track; keep the cadence."}

    # --- message drafting ---------------------------------------------------------------

    _DRAFTS = {
        ("EMAIL", "follow_up"): (
            "Following up on your inquiry",
            "Dear {name},\n\nThank you for your interest{property_clause}. "
            "I would love to help you take the next step — would a short call this week "
            "suit you?\n\nBest regards,\n{agent}",
        ),
        ("EMAIL", "viewing_invite"): (
            "Shall we arrange a viewing?",
            "Dear {name},\n\nI have a property I think fits what you are looking for"
            "{property_clause}. Could I book you in for a viewing?\n\nBest regards,\n{agent}",
        ),
        ("SMS", "follow_up"): (
            None,
            "Hi {name}, {agent} here regarding your property inquiry. "
            "Is there a good time to call you this week?",
        ),
        ("WHATSAPP", "follow_up"): (
            None,
            "Hello {name}! {agent} from the agency — just checking in on your "
            "property search. Happy to answer any questions.",
        ),
    }

    def draft_message(self, *, channel, intent, contact, agent, property_title=None):
        subject, template = self._DRAFTS.get(
            (channel, intent), self._DRAFTS[("EMAIL", "follow_up")]
        )
        clause = f" in {property_title}" if property_title else ""
        context = {
            "name": contact.first_name or "there",
            "agent": agent.full_name if agent else "your agent",
            "property_clause": clause,
        }
        return {
            "subject": subject.format(**context) if subject else None,
            "body": template.format(**context),
            "model": "rules-v1",
        }

    # --- public chat qualification ------------------------------------------------------

    #: The slot-filling script: (slot, question). Stateless — the client round-trips
    #: `state`, so the server holds nothing between messages.
    CHAT_SLOTS = (
        ("intent", "Are you looking to buy or to rent?"),
        ("budget", "What budget do you have in mind?"),
        ("location", "Which area are you interested in?"),
        ("name", "Great — may I have your name?"),
        ("email", "And an email address so our agent can reach you?"),
    )

    def qualify_chat(self, *, state, message):
        state = dict(state or {})
        # Bind the visitor's answer to the slot we last asked about.
        pending = state.pop("_pending", None)
        if pending and message:
            state[pending] = message.strip()

        for slot, question in self.CHAT_SLOTS:
            if not state.get(slot):
                return {
                    "done": False,
                    "reply": question,
                    "state": {**state, "_pending": slot},
                }
        return {
            "done": True,
            "reply": "Thank you! One of our agents will contact you shortly.",
            "state": state,
        }
