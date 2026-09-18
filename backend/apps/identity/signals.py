"""Signals `identity` sends, and other apps answer.

**Why signals rather than direct calls.** The import DAG (architecture.md §1.2, enforced by
import-linter) forbids `identity` from importing `contacts`, `crm`, `property_ops`, `finance`
or `platform`. All of those may import `identity`. That is a deliberate layering, and it
leaves `identity` with two jobs it cannot do alone:

* **Portal eligibility.** SRS 3.11.2 grants portal login only to a client with a completed
  contract — but contracts live in `crm` and `property_ops`, which `identity` cannot read.
* **Audit.** SRS 5.3 requires an audit trail for login and permission changes — but the audit
  table lives in `platform`, which `identity` cannot write to.

Both are solved by inverting the dependency: `identity` *sends*, the other apps *receive*.
The import arrow points the legal way (crm/platform → identity), and `lint-imports` staying
green in CI is the proof that it does.

Receivers are connected in each app's ``apps.py::ready()``:

    apps/crm/receivers.py           -> portal eligibility for BUYER / SELLER
    apps/property_ops/receivers.py  -> portal eligibility for TENANT / LANDLORD
    apps/platform/receivers.py      -> audit rows for every event below
"""
from django.dispatch import Signal

# --- Query signal -------------------------------------------------------------------------

#: "Does this contact hold a completed contract that entitles them to a portal login?"
#:
#: Sent by `identity.services.grant_portal_access`. Receivers that own the referenced contract
#: type answer; every other receiver returns None.
#:
#: kwargs:  contact_id, portal_type, contract_ref_type, contract_ref_id
#: returns: None (not mine / not eligible), or a dict:
#:          {"eligible": True, "email": str, "first_name": str, "last_name": str}
#:
#: The response carries the contact's identity details because the answering app can already
#: see the contact — which is what lets `identity` create the login without importing
#: `contacts`. **Absence of a positive answer means refuse**: an app that is not installed,
#: or a contract that does not exist, can vouch for nobody.
verify_portal_eligibility = Signal()

# --- Event signals (audit; SRS 5.3) -------------------------------------------------------
#
# Fire-and-forget. A receiver that raises must not break the mutation that triggered it — the
# sender wraps dispatch accordingly — but a *missing* audit row is still a defect worth
# noticing, so receivers log rather than swallow silently.

#: kwargs: user, actor, role_code
user_registered = Signal()

#: kwargs: user, actor, role_code
role_assigned = Signal()
role_revoked = Signal()

#: kwargs: user, actor
user_deactivated = Signal()
user_reactivated = Signal()

#: kwargs: user, actor, portal_profile
portal_access_granted = Signal()
#: kwargs: user, actor, portal_profile, reason
portal_access_revoked = Signal()

#: kwargs: user, ip_address, user_agent
user_logged_in = Signal()
#: kwargs: email, ip_address, user_agent  — no user: the whole point is that none resolved
user_login_failed = Signal()

#: kwargs: user, actor  (actor is None for a self-service reset)
password_changed = Signal()
