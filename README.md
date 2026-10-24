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

## Important limitations before production
This is a foundation, not yet the full Version 3 specification. Public anonymous capture, rate limiting, consent/policy enforcement, a durable background queue and dead-letter handling, AI fact extraction with evidence/confidence validation, website auditing, provider-based enrichment, Cal.com booking, human-review actions, and multi-channel ingestion remain future work. The current message endpoint uses simple deterministic inference; it is not an AI extractor. The current score is a baseline and should be evaluated against a labeled dataset before production decisions depend on it.

Do not deploy this module as a public standalone service without implementing authentication, tenant isolation, CSRF/webhook protections as appropriate, rate limiting, and an operational job queue.

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
