# Reachmark Lead Qualifier Agent

A reusable lead-qualification foundation extracted from Reachmark Digital. It provides transparent, deterministic fit and intent scoring, lead tiers, next-question selection, SQLite-backed qualification history, conversation history, and optional Telegram notifications.

## Current scope

- Side-effect-free intake normalization with field limits, email checks, and safe HTTP/HTTPS URL normalization.
- Durable SQLite job-queue primitive with tenant-scoped idempotency, atomic claims, exponential retries, dead-letter state, and an append-only event trail.
- Tests for queue lifecycle, retry exhaustion, duplicate submissions, and tenant isolation.
- Deterministic fit score, intent score, weighted total, and A/B/C/D tiers.
- Reasons and missing-field evidence for each score.
- A single next qualification question.
- Flask routes designed to register with Reachmark's existing app and database connection.
- SQLite qualification, conversation, and event history.
- Optional Telegram notifications for captured leads.
- Unit tests for scoring boundaries and representative leads.

## Integration model
The initial implementation is a Flask integration module, not a complete standalone SaaS. It expects the host application to provide its database context manager, lead-creation function, authentication/session handling, and application logger. In Reachmark, register it with register_lead_qualifier(app, db, log, add_lead).

Routes exposed by the module:
- POST /api/qualifier/leads — authenticated lead capture.
- GET /api/qualifier/leads — list qualified leads for the current owner.
- GET /api/qualifier/leads/<lead_id> — lead, conversation, and event details.
- POST /api/qualifier/leads/<lead_id>/message — record a reply and recalculate the baseline score.
- GET /api/qualifier/summary — tier and stage counts.

## Install and test
Create a virtual environment, run pip install -r requirements.txt, then run python -m unittest discover -s tests -v.

## Environment
Telegram alerts are optional and require TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID. Do not commit secrets to this repository.

## Phase 1 reliability work

The `web.job_queue` module is a storage primitive for a separate worker to use. It supports `queued`, `processing`, `succeeded`, and `dead` states; atomic claiming; bounded retries; tenant-scoped idempotency; and audit events. It does not start a worker process or automatically execute jobs. The current implementation is SQLite-backed for development and modest single-host workloads.

The `web.intake_validation` module bounds recognized fields, discards unknown keys, normalizes email and website values, and rejects malformed input before persistence. It does not fetch websites or guarantee protection against SSRF in any future URL-fetching component.

CI runs unit tests, Python compilation, and a dependency vulnerability audit. A clean dependency audit only covers known published package vulnerabilities and does not replace application security review.

## Remaining integration gates
This repository remains a Flask integration module rather than a standalone SaaS application. The authenticated queue, worker, tenant-scoped review queue, and signed channel webhook adapter are implemented on the feature branch. Live provider enrichment, website crawling/auditing, an LLM extractor, Cal.com booking, production dashboard UI, Telegram delivery from the worker, and provider-specific email/WhatsApp/phone adapters are not represented as complete: they require approved provider choices, credentials, host-app wiring, and additional validation. No provider calls are fabricated.

The deterministic `baseline-v1` scoring engine is unchanged. Evaluate it against a representative, labeled dataset before using it for consequential routing. Do not expose the service publicly until the deployment checklist in SECURITY.md is satisfied.

## Qualification principles
AI may extract and explain supported facts, but a deterministic rules engine owns the score. Uncertain or conflicting cases should be routed to human review. Every material decision and external action should be recorded with enough evidence to explain it later.


## Authenticated intake and worker (feature branch)

The optional server-to-server endpoint is `POST /api/v1/intake/leads`. Register it with:

```python
from web.intake_api import create_intake_blueprint
app.register_blueprint(create_intake_blueprint())
```

Configure `LEAD_QUALIFIER_API_TOKEN` and `LEAD_QUALIFIER_TENANT_ID`; without both values the endpoint fails closed with HTTP 503. Send a Bearer token, an `Idempotency-Key` header, JSON lead fields, and explicit `"consent": true`. A new submission returns HTTP 202 and a job ID; a repeated identical key/payload returns the original job, while a changed payload with the same key returns HTTP 409. The configured tenant is server-side, not accepted from the body.

Run the worker on the same host/database with `python -m web.worker`. It processes `qualify_lead` jobs with the existing `baseline-v1` deterministic scorer, retries failures, and writes results/audit events. It does not call Telegram, email, an LLM, enrichment services, or booking APIs. Use a process supervisor in deployment; keep the SQLite database on persistent local storage. Do not use this SQLite queue for multi-instance production workloads.

Run checks with `python -m pytest tests -q`, `python -m compileall -q web tests`, and `pip-audit -r requirements.txt`.


## Authenticated multi-tenant intake and review

The intake blueprint exposes:
- `POST /api/v1/intake/leads`: bearer-authenticated lead submission, explicit consent, normalized fields, bounded JSON body, per-token/tenant rate limiting, idempotency, and a queued job ID.
- `GET /api/v1/intake/jobs/<job_id>`: tenant-scoped job status and lifecycle events, without returning the raw intake payload.
- `POST /api/v1/webhooks/<channel>`: HMAC-SHA256 signed webhooks for `website_form`, `email`, `whatsapp`, `telegram`, `phone`, `crm`, and `calcom`. Sign `timestamp + "." + raw_request_body`, send `X-Webhook-Timestamp`, `X-Webhook-Signature: sha256=<hex>`, and `Idempotency-Key`. Timestamps older than five minutes are rejected. These are generic adapters for trusted gateways, not turnkey integrations with each vendor's unique webhook format.
- `GET /api/v1/reviews` and `POST /api/v1/reviews/<job_id>`: tenant-scoped review queue and one-time approve/reject decisions with required reasons and audit events. Recording approval does not execute an external action.

Register both blueprints with the host Flask app:

```python
from web.intake_api import create_intake_blueprint
from web.review_api import create_review_blueprint

app.register_blueprint(create_intake_blueprint())
app.register_blueprint(create_review_blueprint())
```

For one tenant, configure `LEAD_QUALIFIER_API_TOKEN` and `LEAD_QUALIFIER_TENANT_ID`. For multiple tenants, configure `LEAD_QUALIFIER_API_TOKENS` as a JSON array of objects with `token`, `tenant_id`, and optional `scopes`. Keep intake tokens scoped to `intake` and `status`; review uses a separate `LEAD_QUALIFIER_REVIEW_TOKENS` JSON array with `token`, `tenant_id`, and `scopes: ["review"]`. Use long random secrets and keep them out of source control.

Configure signed webhook secrets with `LEAD_QUALIFIER_WEBHOOK_SECRETS`, a JSON array of `{"channel":"whatsapp","secret":"...","tenant_id":"..."}` entries. Store all secrets in deployment secret settings, not in Git.

Run the worker with `python -m web.worker` on the same host and persistent SQLite file. Every processed qualification result is marked for human review, and external actions remain disabled by default. SQLite is not suitable for multi-instance production use; migrate to a shared transactional queue/database before horizontal scaling.
