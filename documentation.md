# Real Estate CRM Backend: Daily Progress Log

Quick daily notes on what got done on the backend and where things stand. Frontend-only work
is tracked in the frontend repo's own log and omitted here.

**Current state:** the backend is built against `architecture.md` v3.4 — **all nine apps
operational end to end** under the strict import DAG (101 tables, 994 tests, 15/15 import
contracts, OpenAPI clean). On top of Phase 1's lead-to-deal engine: leases activate and
generate rent schedules, invoices collect payments through allocations and PDC cheques,
commissions calculate marginally and split, owner statements assemble on a collection basis,
maintenance flows from a portal tenant's request to a billable work-order expense, documents
version and sign through a built-in token e-sign, tasks recur and remind, messages thread
across channels, offers negotiate to an acceptance that feeds the transaction, drip campaigns
advance leads, the public site has listings/inquiries/landing-pages/chat, portals sync through
mock adapters, webhooks fan out signed events, saved reports schedule themselves, dashboards
snapshot nightly, and the permission matrix / field permissions / custom fields are all
runtime-editable. Every external service is a settings-selected mock adapter documented in
`third-part-needed.md`. Recurring work is idempotent management commands (no Celery — Render
free tier).

Live endpoints: `/api/v1/` for all nine apps, `/api/public/` for the unauthenticated site
surface; Swagger at `/api/docs/`.

Bootstrap with `manage.py bootstrap_e2e` — idempotent; it seeds a company/branch/team, a
super-admin login (printed on stdout) and the property type the live walkthrough
(`backend/scripts/e2e_walkthrough.py`) needs. `createsuperuser` also works and attaches the
`super_admin` role. The old `seed_demo` command went with the flat layout.


---

## Day 16 (continued): Fri, Sep 18, 2026; SRS Phases 2–4 — all nine apps end to end

One long pass, phases A–G, one commit each, 994 tests (up from 705). The plan's constraint
held: **exactly one schema addition** (`crm_campaign_enrollment`, v3.4) — everything else
landed schema-free on the 100 tables that already existed.

**What shipped, per phase**
- **A — foundations**: channel gateways (email real, SMS/WhatsApp/push logging mocks), the
  merge engine (regex over a whitelisted flat dict — no template-engine SSTI surface), the
  `notify()` engine (never raises; suppression is an outcome, quiet hours are tz-aware),
  file storage with gated downloads, the permission-class trio + the behavior-preserving
  permission-matrix seed.
- **B/C — the money spine**: lease state machine over the GiST overlap exclusion, rent
  schedules, deposits, invoices with `_refresh_invoice` as the sole writer of
  amount_paid/balance_due, payments/allocations/reversals at value dates, PDC cheques,
  marginal TIERED commissions with residue-folded splits, installments, collection-basis
  owner statements, reconciliation, maintenance→work-order→billable-expense, and
  `mark_deal_won` — idempotent, degrading, quietly-refusing.
- **D — collaboration**: documents with one access function serving the scoping arm, the
  download gate and the writes (incl. the ALL-scope confidential inversion), in-place
  versions audited as history, built-in e-sign (stateless signed tokens, state-based
  revocation, sequential turns, sha256 fingerprints), recurring activities materialized on
  completion, threads/messages that keep FAILED sends as history, mentions, templates.
- **E — crm completion**: immutable-once-submitted offers whose acceptance rejects every
  rival, checklist-gated transactions, the matching engine (±10% budget tolerance, PostGIS
  dwithin), drip campaigns on the one new table, landing pages, saved-search alerts with an
  always-advancing window, and the public API (honeypots, enumeration-proof 202s).
- **F — platform**: portal-sync engine (hash-skip outbound, signal-inverted inbound leads),
  HMAC-signed webhook fan-out with SyncLog attempts and backoff retries, the safe
  saved-report executor (user JSON never reaches `filter()` raw), schedules that advance
  `next_run_at` before executing, dashboard snapshots sharing `_kpis()` with the live view,
  and the deterministic AI provider (scoring rationale, next-best-action, drafts, chat
  qualification).
- **G — admin hardening**: `HasPermission` on the new surfaces + two all-staff-seeded
  existing ones, runtime grant/revoke with audit, field-level permissions
  (most-permissive-wins; absence = today's behavior), the custom-field registry with
  lenient-by-default validation at every entity write.

**Decisions worth keeping**
- Satellites still never call domain write services: inbound portal leads reach `crm` over
  a request/response signal (`inbound_lead_received`), the same inversion as
  `verify_portal_eligibility`; matrix-edit audits ride `role_permission_changed`.
- E-sign tokens are `django.core.signing` payloads: stateless, expirable, and revoked by
  the envelope status re-check — voiding kills every outstanding URL at once.
- Webhook attempts are SyncLog rows on an auto-managed WEBHOOK connection — the graduation
  path to a dedicated delivery table is documented where the trade-off lives.
- The custom-field admin API lives in `identity`, not `core`: core is the kernel and may
  import nothing, not even the permission classes its own admin surface would need.
- Recurring work stays idempotent management commands (13 of them now) — the no-Celery
  posture for the Render free tier, each proven run-twice-changes-nothing.

---

## Day 16: Fri, Sep 18, 2026; SRS Phase 1 — the operational CRM

Six modules, end to end: `contacts`, `inventory`, the `crm` lead engine, the pipeline and its
Kanban, viewings, GPS field tracking, and the dashboards that read all of it. 662 tests, up
from 381. What follows is the reasoning worth keeping, not a feature list.

**One way in.** `capture_lead` is the only path a lead can take into the system — web form,
portal feed, walk-in, phone, manual entry, CSV. Every guarantee in SRS 3.1 hangs off it:
de-duplication, scoring, routing, the SLA clock, the instant acknowledgment. A second create
path would be a set of requirements that silently applies to some leads and not others. The
order inside it is load-bearing too: the contact is resolved first because scoring reads their
phone, the score is computed before routing because rules match on `min_score`, and the SLA
clock starts only once the lead is with someone who could answer it.

**Three bugs that came out of reading the old code rather than running it.**
- Round-robin filtered users on `user_roles__role__code`, which joins the through-table — an
  agent holding two roles appeared twice, doubling their counted load and permanently
  protecting them from assignment. Resolving the candidate ids first, then annotating over a
  plain `pk__in`, is the fix.
- `merge_contacts` repointed `leads` and nothing else, silently orphaning every other child —
  and would have gone on orphaning each relation added by each new module. It now walks
  `Contact._meta.related_objects`, so a relation added in a later pass is carried
  automatically. It also repoints `identity_record_share`, which points at a bare UUID with no
  foreign key: left behind, the grant silently evaporates for whoever held it.
- Moving a deal *out* of a lost stage left `lost_reason` set, so the deal read as open while
  every report still showed why it was lost.

**The tension in de-duplication, and how it resolves.** The check has to search the whole book
— a scoped lookup hides the contact another agent already owns and creates the exact duplicate
SRS 3.1.10/3.1.11 exist to prevent. But returning that contact would make
`GET /contacts/duplicates/` a way to read any record in the company by guessing a phone number.
So a match outside the caller's scope is reported as its *existence*, a masked name, and who to
ask. That is the minimum disclosure that still stops duplicate outreach. The previous
implementation had no scoping on this path at all.

**Radius search is PostGIS now.** The old code computed a haversine distance in Python over
every row, which cannot use an index and reads the whole table to answer "within 5km".
`geo_point` is `geography(Point,4326)` with its GiST index already built. `distance_km` is also
*returned* — the old serializer computed it and never exposed it, so a map view had no way to
sort by nearest.

**Mandatory means mandatory.** SRS 3.4.4 wants a reason on every deal stage move. It is refused
at the service, refused at the serializer, and refused again by a CHECK constraint on
`crm_deal_stage_history.reason`, so an import or a future endpoint cannot talk its way past it.
The same shape applies to GPS: 3.16.5 refuses a session without GPS enabled, because a session
that opens without it is an untracked visit wearing a tracked visit's name — worse than no
session, since the record implies a trail that does not exist.

**The half of a requirement that is easy to miss.** SRS 3.16.7 says GPS tracks are
"role-restricted **and** audited". Row scoping is the restriction and was the obvious half;
without the audit row a manager reads an employee's whole day and leaves no trace. Every
*supervisory* read now writes a `platform_audit_event` with the number of points disclosed.
Self-reads are not audited — the control exists to make supervisory access visible, and logging
self-reads buries the ones that matter.

**No role branching in the dashboard.** The figures are the same questions for everyone; the
*scope* is what differs, and `apply_scope` already decides that from the caller's `data_scope`.
An agent sees their own numbers and a manager the branch's, from one code path. Branching on
role would put visibility rules in a second place, and the two would drift.

**Things the tooling caught that review would not have.**
- `UNIQUE(deal, property, unit)` did nothing for whole-property links, because `unit` is null
  and Postgres treats nulls as distinct in a unique index. `NULLS NOT DISTINCT` is the fix.
- `lint-imports` rejected `platform.selectors` because it transitively reached
  `contacts.services` — `contacts.selectors` imported the normalisers from the write API. The
  normalisers are pure functions and now live in `contacts.normalization`. A read module
  importing the write API was a smell before it was a contract violation.
- drf-spectacular flagged two identical `AgentSummarySerializer` copies colliding into one
  component, and a `status` enum whose generated name would churn on every regeneration. Both
  are now named once, in `core.serializers` and `ENUM_NAME_OVERRIDES`.
- `?format=csv` 404s: DRF reserves `format` for content negotiation. The parameter is `export`.

**No Celery.** Render's free tier blocks background workers, so the SLA sweep is a management
command an external scheduler calls (`python manage.py sweep_sla`, idempotent), and the lead
acknowledgment sends inline. A queued acknowledgment that never runs is worse than a
synchronous one costing a few hundred milliseconds.

**DoD**
- 662 tests (was 381). 15/15 import contracts kept, including three new cross-app edges:
  `crm → contacts`, `crm → collaboration`, `platform → domain selectors`.
- Zero OpenAPI warnings, no migration drift, ruff clean.
- Walked the whole flow against a running server: register owner → owner registers agent → CSV
  import → property with media → publish → radius search → routing rule → capture (de-duplicated
  onto an imported contact, scored, routed, SLA started) → duplicate flagged not refused →
  respond → qualify → convert (twice, idempotent) → Kanban move refused without a reason and
  accepted with one → viewing on the calendar → GPS session refused without GPS, trail appended,
  supervisory read audited → dashboards and reports move. All green.

---

## Day 15: Fri, Sep 18, 2026; schema gaps, and row scoping for every resource

Groundwork for the Phase-1 CRM. Two things had to be true before any domain endpoint could be
written, and neither was.

**The spec had holes.** Reading the SRS against the schema we actually built turned up seven
requirements with **no column or table to land in** — and it was implementing v3.2 faithfully
that exposed them. The lead follow-up SLA (3.1.9), duplicate flagging (3.1.10/3.1.11) and the
instant acknowledgment (3.1.12) had nowhere to record state. SRS 3.4.4 demands a *mandatory*
reason and next action on every deal stage move; v3.2 defined status-history tables for leads
and for properties and none for deals. SRS 3.4.7 wants properties linked to an opportunity,
and §3's own entity diagram promised `Deal ├── Property/properties` while §9's table carried
no such column — the document disagreed with itself. Co-listing (3.3.5) and owner mandate
terms (3.3.9) were missing outright. And SRS 5.1 asks for search over a million records in one
second, for which v3.2 named `pg_trgm` but specified no index.

Fixed in the schema *and* in `architecture.md`, now v3.3 with a changelog table mapping each
change to the requirement that forced it. Leaving the doc behind the code means the next
module gets built from a document that is quietly wrong — and `tests/test_spec_coverage.py`
parses that document.

**Two bugs the tests found, not the review.**
- `UNIQUE(deal, property, unit)` did nothing for whole-property links. `unit` is null there,
  and Postgres treats nulls as distinct in a unique index, so the same property could be
  attached to the same deal repeatedly. `NULLS NOT DISTINCT` (PG15+) is the fix.
- Three attempts at the trigram indexes generated invalid or useless SQL before landing on
  `GinIndex(fields=[...], opclasses=["gin_trgm_ops"])`. `OpClass(Upper("col"), ...)` produced
  unbalanced parens, and the `UPPER()` wrapper was wrong anyway: `gin_trgm_ops` answers
  `ILIKE` natively, so an expression index on `UPPER(col)` would never be used by the query it
  was built for. Confirmed with `EXPLAIN` that the planner picks the index for infix `ILIKE`.

**Row scoping, which was the actual blocker.** `apply_scope` registered exactly one resource —
`"user"` — and raised on everything else. That is the right failure mode, and it also meant no
domain endpoint could return a row. Replaced the hand-written predicate with a per-resource
table of `data_scope → predicate`, registered by each app from `AppConfig.ready()`, since
`identity` sits near the bottom of the DAG and may not name the models above it.

Three things changed from the shape I had first sketched, each one a real leak avoided:

- **`FINANCE_ALL` and `MARKETING_ALL` are not aliases for `ALL`.** The sketch kept one
  `_AGENCY_WIDE` frozenset lumping the three together — reasoned about only for the people
  directory, where it is defensible. §2 grants those two their own modules agency-wide and
  everything else "per permission/grant". A generic builder consulting that set would have
  handed marketing every lead in the company. Now each resource states, per scope, exactly
  what it grants, and **a scope a resource does not mention grants nothing**.
- **A lead has two anchors, not one.** Routing (SRS 3.1.5) may assign a lead to a *team* for
  round-robin, leaving `assigned_agent_id` null. An `OWN` predicate written only against
  `assigned_agent` hides the entire unclaimed pool from the agents meant to work it.
- **Shares are unioned outside the role loop**, filtered on `expires_at`, through a
  `(shared_with_user, entity_type)` index that did not exist. A share is a grant in its own
  right — it has to reach a user whose roles grant nothing on that resource, which is the
  whole point of the table.

No `.distinct()`. Every to-many anchor renders as `pk IN (SELECT …)` rather than a JOIN, so
the filter cannot multiply rows and pagination's `COUNT(*)` does not pay for a `DISTINCT`. The
invariant is held by tests instead of by a blanket call, because the cost of being wrong —
duplicate rows in a paginated list — reads as missing data, not as a scoping bug.

`ScopedQuerysetMixin` refuses a view that declares no resource, and refuses **at import time**
a view that overrides `get_queryset`. Overriding it is the obvious way to add `select_related`
and the one edit that silently removes scoping, because the subclass method wins the MRO.

**DoD**
- 381 tests (was 298). 73 of them are scoping: every resource against every `data_scope`,
  share expiry, portal isolation in both directions, and the no-duplicate-rows invariant.
- 15/15 import contracts kept — the proof that registering from `ready()` did not smuggle a
  domain import into `identity`.
- Rental-tenant isolation tested directly: a portal tenant sees their own lease and the ones
  they are a co-tenant on, and nothing else; suspending the profile takes the rows back.

---

## Day 14: Fri, Sep 18, 2026; portal clients, password reset, logout, audit

Finished register and login for all eight roles — this time working from
`full-featured-web-based-real-estate-crm-srs.md` and
`Real-Estate-CRM-Complete-User-Role-Definitions.md` directly. Both had been sitting untracked
in the repo root the whole time while being the authority `architecture.md` derives from;
they are now committed.

**Reading the sources changed two decisions.**
- SRS 3.15.2 delegates exactly two registration steps — managers register owners, owners
  register agents. The "admin fallback" that also let owners create property managers,
  marketing and finance staff was mine, invented before I had the documents. Removed;
  those three sit with Super Admin, whose 3.15.1 remit is company-wide user governance.
- Role Definitions §4 puts portal invitation under the Sales/Leasing Agent ("Invite portal
  access only for clients with completed contracts") and §5 gives the Property Manager tenant
  onboarding. That settled who may invite whom far better than my guess would have.

**The portal, and the DAG problem it posed.** A client's login is gated on a completed
contract (SRS 3.11.2), but contracts live in `crm` and `property_ops`, which `identity` may
not import. Solved by inverting the dependency: `identity` defines signals and sends them,
the domain apps answer from receivers connected in `apps.py::ready()`. The import arrow
points the legal way and `lint-imports` staying at 15/15 is the proof. **Silence is
refusal** — an uninstalled app, a missing contract, a contract in the wrong state and a
contact unconnected to it all look identical from `identity`, and all mean no.

The same inversion then solved the audit gap flagged on Day 13, which was blocked by exactly
the same constraint. One mechanism, two problems.

**Eligibility, read off the real enums rather than paraphrased.** A lease counts when ACTIVE,
EXPIRING or RENEWED — deliberately *narrower* than `Lease.OCCUPYING_STATUSES`, which includes
PENDING_SIGNATURE. That constant is right for the double-letting exclusion it was written
for and wrong here: the SRS says "signed", and awaiting signature is not signed. Being party
to the contract is checked too — a co-tenant qualifies (architecture.md §2 says
"tenant/**party**"), a guarantor does not.

**Two test-infrastructure bugs worth recording.**
- Throttling was silently self-throttling the suite. Hundreds of logins share one LocMem
  counter, so whichever test happened to run last failed — a flake with nothing to do with
  the code under test. Disabled in test settings (with the scopes still *present*: a missing
  scope raises rather than meaning unlimited) and covered properly in `test_throttling.py`,
  which patches the class attribute, since DRF binds `THROTTLE_RATES` at import and
  `override_settings` cannot reach it.
- An audit-cleanup fixture tried `DELETE` on `platform_audit_event` and got "permission
  denied". That is the append-only guarantee working exactly as designed; the fixture was
  wrong. Per-test transaction rollback needs no privilege.

Also: a silent `str.replace` in a bulk edit didn't match after an earlier edit shifted its
anchor, so the `user_registered` signal never landed and only one audit test caught it.
Assert on every programmatic replacement.

**DoD**
- 298 tests (was 219). The portal file alone covers the invite matrix, every contract status
  on both sides, fail-closed behaviour, and client isolation in both directions.
- Walked the lifecycle against a running server: PM registered → tenant invited against a
  real ACTIVE lease → client logs in, forced to choose her own password, sees zero staff,
  cannot register anyone → access revoked → login stops → record survives as SUSPENDED. The
  audit table held all twelve events, including the failed logins with no actor.

---

## Day 13: Fri, Sep 18, 2026; registration, auth, and user administration

The first API layer. Ten endpoints under `/api/v1/`, Swagger back at `/api/docs/`, and
`apply_scope` — the mandatory row-visibility layer — landed alongside them, since listing
users is the first thing that needs it.

**Registration authority.** The spec states the chain super_admin → manager → owner → agent
in three places. It reads oddly, because a manager mints an owner who outranks them in data
scope, but it is explicit twice over and the two are different axes: the manager is a
branch's onboarding administrator, the owner is the person with agency-wide visibility. The
three roles the spec never places — property_manager, marketing, finance — went to owner and
super_admin. `portal` is refused outright: a portal profile needs a contact holding a
completed contract, and the DAG forbids identity from reading crm or property_ops to check.

**Four defects found while building, three of them security.** Worth recording because none
were visible from the schema pass:

- **Soft-deleted users could log in.** `UserManager` never overrode `get_by_natural_key`, so
  `authenticate()` matched on email alone — and once an address was reused, an ordinary
  login raised `MultipleObjectsReturned`.
- **`ALL` scope narrowed access instead of widening it.** The predicate returned an empty
  `Q()`, but `Q()` is Django's *identity* element: `Q() | Q(branch=x)` collapses to
  `Q(branch=x)`. A user holding both `owner` and `agent` saw only their branch. A grant of
  everything has to short-circuit, not combine — caught by a test written specifically for
  the multi-role case.
- **A manager could grant themselves `owner`**, jumping BRANCH → ALL in one call: escaping
  the matrix by obeying it, since managers legitimately grant `owner`. The earlier test
  passed vacuously because it used an agent, who can grant nothing. Probed the hole to
  confirm it was real, then barred anyone from changing their own roles.
- **`normalize_email` lowercases only the domain**, so `Alice@` and `alice@` would have
  become two live rows both satisfying the partial unique index, only one able to log in.

**Decisions with long reach.** `apply_scope` OR-s a user's roles rather than ranking them —
`UserRole` has no `is_primary` flag, and ranking would need a total ordering over
`data_scope` that does not exist (FINANCE_ALL is module-wide, BRANCH is org-position-wide).
An unregistered resource raises rather than returning the unfiltered queryset, because a
silent fallback is how a scoping layer quietly stops scoping.

`must_change_password` is a new column and a deviation from §4's field list, taken because
the spec describes no credential-delivery mechanism at all. The gate is a
`DEFAULT_PERMISSION_CLASS`, not opt-in per view, so a forgotten view cannot weaken it.

**Known gap, flagged rather than fixed:** §1.3 wants `platform.services.record_event` on
every sensitive mutation, but the import DAG forbids `identity → platform`. Register, grant,
revoke, deactivate and login — the five most audit-worthy events in the system — currently
write no audit row. Fixing it needs either an inverted dependency (identity emits a signal,
platform receives it in `apps.py::ready()`) or an amendment to the §1.2 matrix. That is a
decision, not an oversight.

**DoD**
- 213 tests (was 82), including all 49 cells of the authority matrix and the scoping layer
  tested directly rather than only through HTTP.
- Walked the whole chain against a running server: root → manager → owner → agent, the
  forced password change gating the API, an agent seeing branch colleagues in summary form
  only, and every refusal.

---

## Day 12: Fri, Sep 18, 2026; the whole schema, phases 3–10

Built the remaining seven apps in DAG order and closed every deferred foreign key. 98 tables,
82 tests, 15 import-linter contracts.

**What landed**
- `contacts` (4), `inventory` (9), `crm` (21), `property_ops` (10), `finance` (16),
  `collaboration` (17), `platform` (8).
- The four append-only tables — `crm_agent_location_point`, `finance_account_entry`,
  `collaboration_signature_event`, `platform_audit_event` — with `UPDATE`/`DELETE` revoked
  from the app role and tests proving the revoke actually bites.

**Decisions worth remembering**
- **Forward FKs needed almost no ceremony.** Django resolves same-project forward references
  through lazy string refs and migration dependencies, so the raw-UUID-then-promote dance the
  old code had begun was unnecessary. The rule used instead: a field pointing at a
  not-yet-built app is declared in the phase that introduces its *target*, and the owning app
  picks up a small follow-up migration. Only `identity` ↔ `collaboration` was genuinely
  circular and actually required deferral.
- **Where the spec offered a choice between a service-layer rule and a CHECK, the CHECK won**
  whenever both operands live on the same row. A service can be bypassed by an import, a
  shell, or a future endpoint that forgets; a CHECK cannot. That covers the mandatory deal
  loss reason, the commission transaction/lease XOR, and "RECONCILED requires difference = 0".
- **Inspecting generated DDL caught two silent index bugs.** `models.Index(opclasses=[...])`
  over JSONB produced a *btree*, which can never serve the containment operator those columns
  are filtered with — it would have built fine and never been used. And `PointField` already
  emits its own GiST index, so the explicit one built the same index twice.
- **`NULLS NOT DISTINCT` on `platform_dashboard_snapshot`.** ORG-scope snapshots have a null
  `scope_id`, and Postgres treats NULLs as distinct in a plain unique index — so the nightly
  job would have inserted a duplicate ORG row on every run.
- **One spec-vs-framework conflict, resolved for the spec.** Django's `auth.E003` demands a
  plainly unique `USERNAME_FIELD`; architecture.md §4 specifies a *partial* unique index on
  `identity_user.email` (`WHERE deleted_at IS NULL`) so soft-deleting a user frees the
  address. Kept the partial index, silenced the check, and documented that every email lookup
  must filter `deleted_at`.

**DoD**
- Verified on a genuinely virgin database (`down -v`, confirmed zero tables): all 23 local
  migrations apply in one pass. That is what proves the deferred-FK ordering is correct
  rather than incrementally lucky.
- `tests/test_spec_coverage.py` parses `architecture.md` and checks the built schema against
  it, so a forgotten deferred FK — which otherwise leaves no trace in the owning app's code —
  fails loudly.

---

## Day 11: Fri, Sep 18, 2026; restructure to architecture.md v3.2

Replaced the flat seven-app layout with the nine DAG-ordered apps the spec requires, and got
the repo back to a state where `migrate` is meaningful.

**Why the rewrite, not an extension**
- Row visibility moved from Postgres RLS (session GUC + `tenant_id` partition on every table)
  to application-layer scoping via `identity.selectors.apply_scope`. The GUC machinery in
  `apps/core/tenancy.py` and the per-app `000N_rls.py` migrations had no place in the new
  design and were deleted rather than ported.
- "Tenant" was redefined to mean a *rental tenant* (lease party), so the SaaS partition column
  is gone from every table. The non-superuser `crm_app` role stays, but its job is now to make
  the four append-only tables genuinely immutable — a superuser ignores the `REVOKE`.

**What got done**
- Tagged the old implementation `phase1-flat-layout` and committed its removal as one
  checkpoint, so the domain logic (lead scoring/routing, contact merge, listing state machine,
  `move_stage`) stays retrievable for the services pass.
- Scaffolded architecture.md §1.2's mandatory public surface — `apps.py`, `services.py`,
  `selectors.py`, `tasks.py`, `api/` — across all nine apps, plus the normative `models/`
  packages for the three fat apps (`crm`, `property_ops`, `collaboration`).
- Wired **import-linter** contracts into CI. The §1.2 matrix is not a pure layering
  (`collaboration`/`platform` are satellites everyone calls, and `crm` ↔ `property_ops` is
  bidirectional by design), so the contracts encode the parts that are absolute: the
  `core → identity → contacts → inventory` spine, the ban on importing another app's `api/`,
  and the ban on satellites calling domain write services.
- Split extension bootstrap by trust level: `pg_trgm` and `btree_gist` are trusted, so a Django
  migration installs them and it works on Neon unattended; PostGIS is not, so it is
  provisioned as infrastructure in `postgres-init.sql`, a CI psql step, and the Neon dashboard.
- Unified the app database role on `crm_app` across compose, CI, settings, and `.env.example`.
  This was a real hazard: `db_policy._app_db_role()` reads the role name from settings at
  migrate time, so the three-way mismatch would have produced `REVOKE`s against a role nobody
  connects as — immutability tests passing vacuously.
- Rewrote `README.md`, `DEPLOY.md`, and this file, all three of which still described the old
  system as live.

---

## Day 10: Tue, Aug 11, 2026; seed depth

- Strengthened demo seeding for role walkthrough reliability in
  `backend/apps/core/management/commands/seed_demo.py`.

---

## Day 9: Mon, Aug 10, 2026; seed coverage for new roles

- Extended `seed_demo` with one user per missing role (super_admin, manager,
  property_manager, marketing, finance, portal) plus an `owner@demo.test` alias.

---

## Day 5: Tue, Aug 5, 2026; property/deal API fields

- Property geo `lat/lng/radius_km` filter; `Listing.co_listing_agent`; opportunity
  `days_in_stage`; activity `related_model` for deal tasks.

---

## Day 4: Mon, Aug 4, 2026; foundation shell, auth/leads APIs

- Forgot/reset password, MFA verify stub, `mfa_enabled` + `lead_routing_strategy` on `/me/`,
  `Notification` list/mark-read; `LeadSource` CRUD, lead assign action, routing strategy used
  in `capture_lead` / `route_lead`.

---

## Day 3 (evening): Mon, Aug 3, 2026; Phase 1 close-out

**Backend**
- Forgot/reset password + MFA verify stub; `mfa_enabled` + `lead_routing_strategy` on `/me/`.
- `LeadSource` CRUD; tenant routing strategy wired into `capture_lead` / `route_lead`.
- Opportunity `days_in_stage`; property geo `lat/lng/radius_km`; `Listing.co_listing_agent`;
  `Notification` list + mark-read.

**DoD**
- Backend `tests/test_phase1_closeout.py` (auth/MFA/sources/routing/geo/notifications) —
  green.

---

## Day 3 (continued): Mon, Aug 3, 2026; Phase 1 Wave A close-out

- `GET /users/`, `POST /leads/{id}/assign/`; custom_fields validator allows free-form when no
  FieldDefinitions (and system keys `image_url` / `media_order` / `cover_media_id`).

---

## Day 1: Thu, Jul 23, 2026; Backend build & infrastructure setup

Built the backend from scratch and got the first working slice of the MVP running locally.

**What got done**
- Set up the Django project with split settings (dev/prod/test), Docker Compose (Postgres,
  Redis, MinIO), Dockerfile, and CI pipeline.
- Built role-based access control and row-level scoping within the company (agents see their
  own records, managers see their branch, owners see company-wide).
- Built the user system with org hierarchy (Company → Branch → Team → User) and JWT auth.
- Built all the domain modules — contacts (with dedup and merge), properties (with listing
  status tracking), leads (capture → route → score → SLA alerts → convert), deals (pipeline
  stages with Kanban API), and an activity timeline.
- Wrote 21 backend tests covering RLS isolation, scoping, dedup, and state machines. All
  passing.

**Key decisions**
- Access control is role- and ownership-based inside one company (not SaaS multi-company
  partitioning).
- Deferred: Elasticsearch, public portal, Twilio/SendGrid, AI features.

---

## Day 2: Fri, Jul 24, 2026; DevOps & deployment

Got the backend deployed and accessible online. Fixed the CI pipeline that was failing due to
an RLS issue.

**CI/CD fix**
- Tests were failing in GitHub Actions because the Postgres Docker container makes its user a
  superuser, which bypasses RLS. Tried demoting the user but Postgres doesn't allow a
  bootstrap user to demote itself.
- Fixed it with a two-role setup: container starts with `postgres` as superuser, then a setup
  step creates a separate `crm` role without superuser privileges. Django connects as `crm`,
  RLS kicks in, tests pass.

**Deployed to production**
- Backend live on Render (`real-estate-crm-rfvc.onrender.com`). Configured env vars for Neon
  DB, CORS, and the frontend origin.
- Neon DB note: the default `neondb_owner` role has `BYPASSRLS`, so we created a dedicated
  `crm_app` role without it. Also rewrote the `-pooler` host to direct connections since
  session variables don't survive the transaction pooler.
- API docs available at `/api/docs/` (Swagger) and `/api/redoc/` (ReDoc).
- Seeded the database with demo data: agents, 22 contacts, 10 properties, 12 leads, 9 deals.

**Where things stand**
- Phase 1 MVP backend is almost done. Core API works. Next: finish remaining end-to-end
  flows, media uploads in production, and refine lead routing/scoring logic.
