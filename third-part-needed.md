# Third-Party Integrations Needed

> Every external service this CRM currently **mocks**. The system is fully functional end to
> end against these mocks; each section tells you exactly what to buy/configure, which
> settings key to flip, and how to verify the cut-over. Nothing in application code changes
> when a real provider is wired — every seam is a settings-selected adapter.

## How the mock seams work (read this first)

| Seam | Settings key | Default (mock) | Where the interface lives |
| :--- | :--- | :--- | :--- |
| Email | `GATEWAY_EMAIL` / Django `EMAIL_*` | Django mail (console backend in dev) | `apps/collaboration/gateways.py::MessageGateway` |
| SMS | `GATEWAY_SMS` | `LoggingSmsGateway` | same |
| WhatsApp | `GATEWAY_WHATSAPP` | `LoggingWhatsAppGateway` | same |
| Push | `GATEWAY_PUSH` | `LoggingPushGateway` | same |
| E-sign provider | envelope `provider` field | `INTERNAL` (built-in click-to-sign) | `apps/collaboration/services/esign.py` |
| Webhook HTTP transport | `WEBHOOK_TRANSPORT` | `urllib` (stdlib POST; tests use `mock` — in-memory outbox) | `apps/platform/webhooks.py` |
| Portal syndication / feeds | `INTEGRATION_ADAPTERS` / `connection.config["adapter"]` | deterministic mocks | `apps/platform/integrations/` |
| AI provider | `AI_PROVIDER` | `mock` (deterministic, rule-based) | `apps/crm/ai.py` |
| Object storage | `AWS_STORAGE_BUCKET_NAME` etc. | **real** — MinIO in compose, any S3 in prod | django-storages (already wired) |

Secrets are never stored in the database: `platform_connection.credentials_ref` is a pointer
into your secrets manager (env var name, vault path); adapters resolve it at call time.

Each section below follows the same six headings:
**SRS requirement · Mocked today · Real provider & plan · Env vars · Webhooks to expose · Test strategy**

---

## 1. Transactional Email (SMTP / SES / SendGrid / Mailgun)
- **SRS**: 3.1.12 (instant acknowledgment), 3.8.1 (drip), 3.18 (notifications), password reset.
- **Mocked today**: not mocked — Django's mail framework is real; dev uses the console
  backend. Production needs real SMTP/API credentials.
- **Real provider & plan**: set `EMAIL_BACKEND` + `EMAIL_HOST/PORT/USER/PASSWORD/USE_TLS`
  (or an anymail backend for SES/SendGrid). No code change.
- **Env vars**: `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`,
  `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL`.
- **Webhooks to expose**: delivery/bounce callbacks → `POST /api/public/webhooks/{id}/`
  feeding `update_message_status`.
- **Test strategy**: send a lead acknowledgment against a sandbox inbox; verify a bounce
  callback flips the message to FAILED.

## 2. SMS (Twilio)
- **SRS**: 3.1.12, 3.8.1, 3.8.4, 3.18.
- **Mocked today**: `LoggingSmsGateway` — logs the send, returns `mock-<uuid>`.
  NOTE: staff SMS notifications resolve the address from `identity_user.phone`.
- **Real provider & plan**: implement `TwilioSmsGateway(MessageGateway)` (~30 lines,
  `twilio` SDK), point `GATEWAY_SMS` at it.
- **Env vars**: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.
- **Webhooks to expose**: status callbacks → inbound webhook → `update_message_status`;
  inbound SMS → `record_inbound_message`.
- **Test strategy**: Twilio test credentials + magic numbers; assert DispatchLog SENT with a
  real SID shape.

## 3. WhatsApp Business (Meta Cloud API / Twilio)
- **SRS**: 3.1.12, 3.8.1, 3.9.2.
- **Mocked today**: `LoggingWhatsAppGateway`.
- **Real provider & plan**: Meta Cloud API app + approved message templates (session
  messages are only allowed inside the 24h window — template management is the real work);
  implement `MetaWhatsAppGateway`.
- **Env vars**: `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`.
- **Webhooks to expose**: message status + inbound messages (Meta webhook subscription) →
  `/api/public/webhooks/{id}/`.
- **Test strategy**: Meta test number; round-trip an inbound message into a Thread.

## 4. Push Notifications (FCM / APNs)
- **SRS**: 3.16.2, 3.18.1.
- **Mocked today**: `LoggingPushGateway`; there is no device-token storage yet — a real
  integration needs a device-registration endpoint and token table (schema amendment to
  design at integration time).
- **Real provider & plan**: FCM (covers both platforms) via `firebase-admin`.
- **Env vars**: `FCM_CREDENTIALS_JSON` (service-account path/ref).
- **Webhooks to expose**: none (delivery receipts come through the SDK response).
- **Test strategy**: FCM dry-run flag against a test device token.

## 5. E-Signature (DocuSign / Dropbox Sign)
- **SRS**: 3.7.3, 3.7.5.
- **Mocked today**: not mocked — a real **built-in** flow (`provider=INTERNAL`): envelope →
  signed token links → click-to-sign → append-only `SignatureEvent`s with document sha256.
  Legally lighter than a qualified provider; fine for internal sign-offs, disclose before
  using for contracts requiring qualified e-signatures in your jurisdiction.
- **Real provider & plan**: implement the DocuSign adapter (create envelope via API, store
  `external_envelope_id`, translate Connect webhook events into the same
  `record_view/sign/decline` service calls so the event trail stays identical).
- **Env vars**: `DOCUSIGN_INTEGRATION_KEY`, `DOCUSIGN_USER_ID`, `DOCUSIGN_ACCOUNT_ID`,
  `DOCUSIGN_PRIVATE_KEY_REF`.
- **Webhooks to expose**: DocuSign Connect → `/api/public/webhooks/{id}/`.
- **Test strategy**: DocuSign developer sandbox; assert the SignatureEvent sequence matches
  the internal provider's for the same flow.

## 6–8. Portal Syndication & MLS/IDX (PropertyFinder, Bayut/Dubizzle, MLS)
- **SRS**: 3.10.1–3.10.4, 3.1.2.
- **Mocked today**: deterministic adapters — push accepts everything and returns
  derived external ids; inbound leads come from `connection.config["fixture_leads"]`.
- **Real provider & plan**: per-portal feed credentials; implement one adapter per portal
  honouring the same `push_listing`/`fetch_statuses`/`pull_leads` protocol; XML/JSON feed
  formats per portal spec.
- **Env vars**: per connection via `credentials_ref` (e.g. `PF_API_KEY`).
- **Webhooks to expose**: portals that push leads → `/api/public/webhooks/{id}/`.
- **Test strategy**: each portal's sandbox feed; assert ExternalMapping idempotency on
  double-run.

## 9. Lead Ads (Facebook/Instagram, Google Lead Forms)
- **SRS**: 3.1.2, 3.8.5. Same adapter seam as 6–8 (`LeadFeedAdapter`); Meta Graph API
  webhook → inbound endpoint → `capture_lead`.

## 10. Ad Platform ROI (Meta/Google Ads)
- **SRS**: 3.8.5. Pull spend into `crm_campaign_metric.cost` via the metrics upsert;
  scheduled command at integration time.

## 11. Payment Gateway (Stripe / local acquirer)
- **SRS**: 3.6.3, 3.6.8. Today all payments are recorded manually through
  `finance.services.record_payment` (bank transfer/cheque reality in this market). A
  gateway integration maps webhook `payment_intent.succeeded` → `record_payment` +
  allocation. Design the checkout surface at integration time.

## 12. Accounting / ERP Export (QuickBooks / Xero)
- **SRS**: 3.6.9. Today: every money object is exportable CSV via the reports surface. Real
  integration: an outbound sync adapter posting invoices/payments through their APIs, keyed
  by `ExternalMapping` for idempotency.

## 13. Calendar Sync (Google / Outlook)
- **SRS**: 3.12.1. Today: `collaboration_calendar_link` table exists, unused; the in-app
  calendar is authoritative. Real integration: OAuth per user, two-way sync worker mapping
  `Activity` ↔ external events via CalendarLink.

## 14. Maps & Geocoding (Google / Mapbox / Nominatim)
- **SRS**: 3.3.7. Today: coordinates are entered manually; PostGIS search is fully real.
  Real integration: geocode `address → geo_point` on property save (adapter + cache).

## 15. SSO (SAML / OAuth2 — Azure AD, Google Workspace)
- **SRS**: 3.17.5. Today: JWT + password auth. Plan: `django-allauth` or `python3-saml`
  in front of the existing identity model; JWT issuance unchanged.

## 16. Zapier / Make Connector
- **SRS**: 3.19.3. Built on the phase-F outbound webhooks (HMAC-signed) + this documented
  event catalogue; a Zapier app is configuration, not code.

## 17. AI / LLM Provider (Anthropic Claude API)
- **SRS**: 3.20.1–3.20.4.
- **Mocked today**: `MockAIProvider` — deterministic; scoring = the rule engine's
  weights verbalized; next-best-action = decision table; chat = scripted slot-filling.
- **Real provider & plan**: `ClaudeAIProvider` using the `anthropic` SDK
  (model: latest Claude), one method per protocol function, structured outputs;
  `AI_PROVIDER=claude`.
- **Env vars**: `ANTHROPIC_API_KEY`.
- **Test strategy**: golden-prompt tests with recorded responses; the mock remains the CI
  provider.

## 18. Website Live Chat / Chatbot Widget
- **SRS**: 3.20.4, 3.9.5. Backend endpoint (`/api/public/chat/qualify/`) is real;
  the widget itself is a frontend deliverable consuming it.

## 19. Object Storage & CDN
- **Already real**: S3-compatible via django-storages (MinIO in compose). Production: any
  S3 bucket + optional CDN in front of signed URLs. Env: `AWS_STORAGE_BUCKET_NAME`,
  `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_S3_REGION_NAME`.

## 20. Report Export Formats
- **SRS**: 3.13.4. CSV is real. XLSX needs `openpyxl`; PDF needs `weasyprint`. Both are
  one-dependency additions; the schedule/report services refuse those formats with a clear
  message until then.

---

## Appendix A — Outbound webhook event catalogue
`lead.captured`, `lead.converted`, `listing.status_changed`, `deal.stage_moved`,
`offer.accepted`, `transaction.status_changed`, `document.signed`.
Signature: `X-Webhook-Signature: sha256=HMAC_SHA256(webhook.secret, raw_body)`;
delivery id in `X-Webhook-Delivery` (dedupe key for consumers).

## Appendix B — .env checklist (mock-mode = everything optional)
```env
# email (real)
EMAIL_HOST= EMAIL_PORT= EMAIL_HOST_USER= EMAIL_HOST_PASSWORD= EMAIL_USE_TLS= DEFAULT_FROM_EMAIL=
# channel gateways (defaults are mocks)
GATEWAY_SMS= GATEWAY_WHATSAPP= GATEWAY_PUSH=
# storage (real; compose provides MinIO)
AWS_STORAGE_BUCKET_NAME= AWS_S3_ENDPOINT_URL= AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY=
# platform seams (urllib = real stdlib delivery; mock = in-memory outbox for tests)
WEBHOOK_TRANSPORT=urllib AI_PROVIDER=mock
# e-sign
ESIGN_PUBLIC_URL_TEMPLATE=/public/esign/{token}/ ESIGN_TOKEN_MAX_AGE_DAYS=30
```
