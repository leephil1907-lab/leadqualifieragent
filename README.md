# Reachmark Lead Qualifier Agent

A reusable lead-qualification foundation extracted from Reachmark Digital. It provides transparent, deterministic fit and intent scoring, lead tiers, next-question selection, SQLite-backed qualification history, conversation history, and optional Telegram notifications.

## Current scope
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

## Important limitations before production
This is a foundation, not yet the full Version 3 specification. Public anonymous capture, rate limiting, consent/policy enforcement, a durable background queue and dead-letter handling, AI fact extraction with evidence/confidence validation, website auditing, provider-based enrichment, Cal.com booking, human-review actions, and multi-channel ingestion remain future work. The current message endpoint uses simple deterministic inference; it is not an AI extractor. The current score is a baseline and should be evaluated against a labeled dataset before production decisions depend on it.

Do not deploy this module as a public standalone service without implementing authentication, tenant isolation, CSRF/webhook protections as appropriate, rate limiting, and an operational job queue.

## Qualification principles
AI may extract and explain supported facts, but a deterministic rules engine owns the score. Uncertain or conflicting cases should be routed to human review. Every material decision and external action should be recorded with enough evidence to explain it later.
