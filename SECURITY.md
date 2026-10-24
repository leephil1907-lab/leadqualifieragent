# Security Policy

## Reporting a vulnerability

Do not publish credentials, lead personal data, or a working exploit in a public issue. Contact the repository owner privately through GitHub's security reporting feature where available.

## Current safeguards

- Lead payloads are bounded and normalized by `web.intake_validation`.
- Queue records and event lookups are scoped by tenant ID.
- Queue idempotency keys are unique per tenant.
- Retry exhaustion is recorded as a dead-letter status with an audit event.
- CI uses read-only repository permissions and does not require production secrets.

## Important deployment boundaries

This project is not yet a complete public SaaS. The current queue is a SQLite implementation intended for development and modest single-host workloads; SQLite is not a distributed queue. Production multi-instance deployments need a PostgreSQL-backed queue/transaction design and a dedicated worker process.

Do not expose intake routes publicly until authentication or explicit public-form policy, durable rate limiting, consent retention, CSRF/webhook protections, tenant resolution, and abuse monitoring are implemented. Never accept a tenant ID from an untrusted request as proof of tenant membership. The trusted authentication layer must establish the tenant.

The intake validator does not fetch supplied URLs and therefore is not a website-audit or SSRF-protection layer. Any future URL fetcher must block loopback, private, link-local, and reserved IP ranges after DNS resolution, re-check redirects, and enforce response size and time limits.

No external email, booking, billing, or CRM write should execute automatically without the appropriate policy gate and audit record.
