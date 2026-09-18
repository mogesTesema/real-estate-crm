# Register & Login — Frontend Integration Guide

Everything you need to build sign-in, the session, user administration, and client-portal
invitations against this API. Every request and response below was captured from a running
server, not written from memory.

**Base URL:** `/api/v1` · **Interactive docs:** `/api/docs` (Swagger) · `/api/redoc`
**OpenAPI schema:** `/api/schema` — generate a typed client from this rather than hand-writing types.

---

## 1. The five-minute version

```ts
// 1. Sign in
const { access, refresh } = await post('/auth/token/', { email, password });

// 2. Every subsequent request
headers: { Authorization: `Bearer ${access}` }

// 3. Who am I, and what may I do?
const me = await get('/auth/me/');
if (me.must_change_password) redirect('/change-password');   // see §4 — this is mandatory
```

Three rules that will save you debugging time:

1. **`must_change_password: true` means the API is closed to them.** Every endpoint except
   `/auth/me/` and `/auth/change-password/` returns **403** until they set a new password.
2. **Access tokens last 15 minutes.** Refresh on 401 — don't assume a long session.
3. **`roles` is an array, not a string.** A user can hold several. Drive your UI off
   `data_scopes` and `grantable_role_codes`, not off `roles[0].code`.

---

## 2. Response shapes

### Success

Single objects are returned bare. Lists are paginated:

```json
{
  "count": 5,
  "next": "http://localhost:8000/api/v1/users/?page=2&page_size=1",
  "previous": null,
  "results": [ /* … */ ]
}
```

Query params on every list: `?page=`, `?page_size=` (max 200), `?search=`, `?ordering=`.

### Errors — one envelope, always

```json
{ "error": { "status": 400, "detail": { "email": ["Enter a valid email address."] } } }
```

`detail` is **either** a field map (validation, 400) **or** `{"detail": "message"}`
(permission and auth errors). Handle both:

```ts
function errorMessages(body: any): string[] {
  const d = body?.error?.detail;
  if (!d) return ['Something went wrong.'];
  if (typeof d.detail === 'string') return [d.detail];
  return Object.entries(d).flatMap(([field, msgs]: any) =>
    (msgs as string[]).map(m => `${field}: ${m}`)
  );
}
```

| Status | Means | What to do |
| :--- | :--- | :--- |
| `400` | Validation failed | Show messages against the named fields |
| `401` | No/expired token, or bad credentials | Refresh once, then sign out |
| `403` | Authenticated but not allowed | Show the message — it explains *why* |
| `404` | Not found **or out of your scope** | Treat as "does not exist" (see §6) |
| `429` | Rate limited | Back off; show "too many attempts, try again shortly" |

---

## 3. Signing in

### `POST /auth/token/`

```json
{ "email": "someone@acme.test", "password": "…" }
```
→ `200 { "access": "eyJ…", "refresh": "eyJ…" }`

Failure is `401` with `{"error":{"status":401,"detail":{"detail":"No active account found with the given credentials"}}}`.
The same message covers a wrong password, an unknown address, a deactivated account and a
soft-deleted one — deliberately, so the endpoint can't be used to discover who has an account.

**Rate limited to 10/min per IP.** A `429` means back off, not retry harder.

A **portal client** whose contract has ended gets `401` with a distinct message —
`"Portal access for this account is not active…"`. Worth surfacing verbatim; it tells them to
contact their agent rather than reset their password.

### `POST /auth/token/refresh/`

```json
{ "refresh": "eyJ…" }
```
→ `200 { "access": "eyJ…", "refresh": "eyJ…" }`

**Refresh tokens rotate.** You get a *new* refresh token each time and the old one stops
working. Always store both from the response, or the user is logged out on the next refresh.

### `POST /auth/logout/` — authenticated

```json
{ "refresh": "eyJ…" }
```
→ `205` (no body)

Revokes the refresh token server-side. **The current access token stays valid until it
expires** — that is inherent to JWTs, which is why the lifetime is only 15 minutes. Clear
both tokens from storage on logout regardless.

### A working interceptor

```ts
let refreshing: Promise<string> | null = null;

async function authedFetch(path: string, init: RequestInit = {}) {
  let res = await call(path, init, getAccess());

  if (res.status === 401 && getRefresh()) {
    refreshing ??= doRefresh();          // one refresh, however many 401s land at once
    try {
      const fresh = await refreshing;
      res = await call(path, init, fresh);
    } catch {
      signOut();                          // refresh itself failed: the session is over
    } finally {
      refreshing = null;
    }
  }
  return res;
}

async function doRefresh(): Promise<string> {
  const res = await fetch('/api/v1/auth/token/refresh/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh: getRefresh() }),
  });
  if (!res.ok) throw new Error('refresh failed');
  const { access, refresh } = await res.json();
  store(access, refresh);                 // store BOTH — the refresh token has rotated
  return access;
}
```

---

## 4. The session — `GET /auth/me/`

The single source of truth for what the UI should render.

```json
{
  "id": "da3fef10-efa8-43a8-9435-c1a37ac53700",
  "email": "root@acme.test",
  "first_name": "Root",
  "last_name": "Admin",
  "full_name": "Root Admin",
  "phone": null,
  "employee_number": null,
  "job_title": null,
  "branch": null,
  "team": null,
  "roles": [
    {
      "id": "3f950a70-…",
      "code": "super_admin",
      "name": "Super Admin",
      "description": "Highest authority: agency-wide data plus system governance…",
      "data_scope": "ALL",
      "is_system_role": true
    }
  ],
  "is_active": true,
  "must_change_password": false,
  "last_login": null,
  "created_at": "2026-09-17T23:54:42.382903Z",
  "company": null,
  "data_scopes": ["ALL"],
  "grantable_role_codes": ["agent", "finance", "manager", "marketing", "owner", "property_manager", "super_admin"],
  "is_superuser": true
}
```

**Drive your UI from these three fields:**

| Field | Use it for |
| :--- | :--- |
| `data_scopes` | How much data to expect — `["ALL"]` means agency-wide, `["OWN"]` means their own records |
| `grantable_role_codes` | **Which roles to offer in the "add user" form.** Empty ⇒ hide the button entirely |
| `must_change_password` | Force the password-change screen before anything else |

`branch`, `team` and `company` are `null` for an unplaced user (a bootstrap super admin, for
example). Don't assume they exist.

`PATCH /auth/me/` edits `first_name`, `last_name`, `phone`, `employee_number`, `job_title` —
and nothing else. Email, roles and activation have their own endpoints, deliberately.

### The forced password change

New users — staff and clients alike — get a password chosen by whoever registered them, so
they are confined until they replace it:

```
POST /auth/change-password/
{ "current_password": "…", "new_password": "…" }
→ 200 { "detail": "Password changed." }
```

The new password must differ from the current one and pass Django's validators (minimum
length, not all-numeric, not a common password, not similar to their name or email). Failures
come back as `400` on `new_password`.

**Gate your router on this**, or every screen will 403 confusingly:

```ts
if (me.must_change_password && route !== '/change-password') {
  return <Navigate to="/change-password" />;
}
```

---

## 5. Forgotten passwords

```
POST /auth/forgot-password/   { "email": "…" }
→ 200 { "detail": "If that email matches an account, reset instructions have been sent." }
```

**Always 200**, whether or not the account exists — so never render "no such user". Show the
message as-is. Rate limited to 5/min.

The email contains a link to `{FRONTEND_ORIGIN}/reset-password?uid=…&token=…`, so **you need a
route at `/reset-password`** that reads both query params and posts them:

```
POST /auth/reset-password/
{ "uid": "…", "token": "…", "new_password": "…" }
→ 200 { "detail": "Password set. You can now sign in." }
```

Tokens are **single-use** and invalidate when the password changes or the user signs in. An
expired, used or tampered token gives `400` on `token` — offer to request a fresh link rather
than showing a raw error.

> **Note for local development:** no mail server is configured, so reset emails print to the
> backend container's log. `docker compose logs backend | grep -A5 "Reset your password"`.

---

## 6. Registering staff

### `POST /users/`

```json
{
  "email": "new.agent@acme.test",
  "first_name": "New",
  "last_name": "Agent",
  "password": "an-initial-password",
  "role_code": "agent",
  "branch": "<uuid>",
  "team": "<uuid>",
  "phone": "+971500000000",
  "employee_number": "E-1001",
  "job_title": "Sales Agent"
}
```

`email`, `first_name`, `last_name`, `password`, `role_code` are required; the rest optional.
Returns `201` with the full user object.

**Who may register whom** — take this from `me.grantable_role_codes`, don't hardcode it:

| Registrar | May create |
| :--- | :--- |
| `super_admin` | any staff role |
| `manager` | `owner` — **in their own branch only** |
| `owner` | `agent` |
| everyone else | nobody |

Two rules worth encoding in the form:

- **`branch` is required** for `manager` (and any branch- or team-scoped role). Omitting it
  gives `400` on `branch`. It is optional for `owner`, `agent` and the rest.
- A `manager` may omit `branch` — theirs is used automatically. If they pass a *different*
  branch they get `403`.

`role_code: "portal"` is rejected with `400` — client logins go through §7 instead, and the
error message says so.

### The rest of user administration

| | |
| :--- | :--- |
| `GET /users/` | Scoped directory. Filters: `?branch=`, `?team=`, `?is_active=true`, `?search=`, `?ordering=` |
| `GET /users/{id}/` | One user |
| `PATCH /users/{id}/` | Profile fields only |
| `DELETE /users/{id}/` | **Deactivates** (`is_active=false`) — never deletes. `204` |
| `POST /users/{id}/reactivate/` | Undoes it |
| `POST /users/{id}/roles/` | `{ "role_code": "finance" }` → the updated user |
| `DELETE /users/{id}/roles/` | Same body, **or** `?role_code=finance` → `204` |
| `GET /roles/` | The eight role definitions, for labels and descriptions |

**Two behaviours that will look like bugs if you don't expect them:**

- **`GET /users/` returns different fields depending on who is asking.** A manager or above
  gets the full record. An agent gets a *summary* — `id`, `full_name`, `job_title`, `branch`,
  `team`, `is_active` and nothing more. Type this as a union and don't assume `email` is there.
- **Out-of-scope records are `404`, not `403`.** A manager fetching a user from another branch
  gets "not found" — that is intentional, so the endpoint can't be used to probe which UUIDs
  exist. Render it as "not found".

Deactivated users **stay in the list** (with `is_active: false`) so they can be reactivated.
Pass `?is_active=true` for an assignee picker.

Revoking a role accepts `role_code` in the request body **or** as a query parameter —
`DELETE /users/{id}/roles/?role_code=finance`. Use the query form if your HTTP client is
awkward about bodies on DELETE (several are).

Refusals to expect: nobody may change their own roles or deactivate themselves, and the last
remaining administrator cannot be removed. All `403` with an explanatory message.

---

## 7. Inviting portal clients

A portal client is an external party — buyer, seller, rental tenant, landlord. They get a
login **only** once they hold a completed contract. There is no public signup; staff invite
them.

### `POST /portal-users/`

```json
{
  "contact_id": "<contact uuid>",
  "portal_type": "TENANT",
  "contract_ref_type": "LEASE",
  "contract_ref_id": "<lease uuid>",
  "password": "an-initial-password"
}
```
→ `201`

```json
{
  "id": "<portal profile uuid>",
  "email": "tina.tenant@example.test",
  "full_name": "Tina Tenant",
  "contact": "<contact uuid>",
  "portal_type": "TENANT",
  "eligibility_status": "ACTIVE",
  "completed_contract_ref_type": "LEASE",
  "completed_contract_ref_id": "<lease uuid>",
  "is_verified": true,
  "last_access_at": null,
  "is_active": true,
  "must_change_password": true
}
```

Note there is **no name or email in the request** — both are read from the contact record.
You pass *who* and *which contract*; the backend derives the rest.

**Pair the type with the right contract**, or you get `400`:

| `portal_type` | `contract_ref_type` | Who may invite |
| :--- | :--- | :--- |
| `BUYER`, `SELLER` | `TRANSACTION` | `agent`, `manager`, `owner`, `super_admin` |
| `TENANT`, `LANDLORD` | `LEASE` | `property_manager`, `manager`, `owner`, `super_admin` |

`marketing` and `finance` cannot invite anyone.

**Eligibility is verified against the real contract**, so expect `400` on `contract_ref_id`
whenever:

- the transaction is `PENDING` or `CANCELLED` (only `CONTRACTED`, `PARTIALLY_PAID` and
  `COMPLETED` qualify);
- the lease is `DRAFT`, `PENDING_SIGNATURE`, `TERMINATED` or `EXPIRED` (only `ACTIVE`,
  `EXPIRING` and `RENEWED` qualify — a lease awaiting signature is not signed);
- the contact is not actually party to that contract;
- the contact has no email address, so there is nothing to log in with;
- they already have portal access.

The message explains which, so surface it rather than a generic failure.

### Managing them

| | |
| :--- | :--- |
| `GET /portal-users/` | Filters: `?portal_type=`, `?eligibility_status=` |
| `GET /portal-users/{id}/` | |
| `DELETE /portal-users/{id}/` | **Suspends** access (`eligibility_status → SUSPENDED`). `204` |

Suspension is the right action when a lease ends: the record and its contract history survive,
and the client simply cannot sign in. Staff see only the client types they may invite — an
agent will not see rental tenants in this list.

### What the client sees

A signed-in portal client gets `/auth/me/` with `roles: [{code: "portal"}]`,
`data_scopes: ["PORTAL_OWN"]` and `grantable_role_codes: []`. They can reach `/portal-users/`
(themselves only) and `/auth/*`. Everything else is `403` or an empty list.

Client isolation is a hard requirement — one client can never see another's records, and
clients never appear in the staff directory.

---

## 8. Reference

### The eight roles

| `code` | `data_scope` | Sees |
| :--- | :--- | :--- |
| `super_admin` | `ALL` | Everything, plus user governance |
| `owner` | `ALL` | Agency-wide |
| `manager` | `BRANCH` | Their branch |
| `agent` | `OWN` | Their own records; branch colleagues in summary form |
| `property_manager` | `MANAGED_PROPERTIES` | Properties they manage and their children |
| `marketing` | `MARKETING_ALL` | Campaigns, sources, landing pages |
| `finance` | `FINANCE_ALL` | Finance objects agency-wide |
| `portal` | `PORTAL_OWN` | Their own data only |

Fetch these from `GET /roles/` for labels and descriptions rather than hardcoding them —
custom roles are supported.

### All endpoints

```
POST   /api/v1/auth/token/                 sign in                     (public, 10/min)
POST   /api/v1/auth/token/refresh/         rotate tokens               (public)
POST   /api/v1/auth/logout/                revoke refresh token
GET    /api/v1/auth/me/                    session
PATCH  /api/v1/auth/me/                    edit own profile
POST   /api/v1/auth/change-password/       change own password
POST   /api/v1/auth/forgot-password/       request reset               (public, 5/min)
POST   /api/v1/auth/reset-password/        complete reset              (public, 5/min)

POST   /api/v1/users/                      register staff
GET    /api/v1/users/                      scoped directory
GET    /api/v1/users/{id}/
PATCH  /api/v1/users/{id}/
DELETE /api/v1/users/{id}/                 deactivate
POST   /api/v1/users/{id}/reactivate/
POST   /api/v1/users/{id}/roles/           grant a role
DELETE /api/v1/users/{id}/roles/           revoke a role

POST   /api/v1/portal-users/               invite a client
GET    /api/v1/portal-users/
GET    /api/v1/portal-users/{id}/
DELETE /api/v1/portal-users/{id}/          suspend access

GET    /api/v1/roles/                      role catalogue
GET    /healthz/                           liveness                    (public)
```

### Local setup

```bash
docker compose up -d
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py createsuperuser   # your first admin
```

The API is at `http://localhost:8000`. Set `FRONTEND_ORIGIN` in the backend environment to
your dev server's URL (defaults to `http://localhost:5173`) — it drives both CORS and the
password-reset link.

Create a Company and a Branch in Django admin (`/admin/`) before registering a `manager`;
there is no org-structure API yet.

### Not built yet

So you can plan around it: portal *data* endpoints (a tenant's leases, rent history,
maintenance requests), the Company/Branch/Team API, MFA and SSO. The endpoints listed above
are the complete surface today.
