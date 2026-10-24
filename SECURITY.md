# Security boundaries and deployment checklist

## Implemented on the feature branch
- Constant-time bearer-token comparison and server-side tenant resolution; client payloads cannot choose the tenant.
- Optional JSON token maps for multiple tenants; review endpoints use a separate review-token map.
- Bounded recognized input fields, email/website normalization, a 32 KiB request-body limit, explicit consent, and bounded idempotency keys.
- SQLite-backed fixed-window rate limits keyed by tenant and one-way token digest.
- Signed channel webhook adapter using HMAC-SHA256 over `timestamp + "." + raw body`, five-minute replay window, and an allowlist of channel names.
- Durable queue with atomic claims, retries, lease recovery, dead-letter handling, tenant-scoped events, and connection lifecycle management.
- Review decisions require a reason, are one-time and tenant-scoped, and do not execute external actions.
- Worker uses the existing deterministic `baseline-v1` scorer; external actions are disabled by default.
- CI runs unit tests, Python compilation, and `pip-audit`.

## Required before public production traffic
- Terminate HTTPS at a trusted reverse proxy; store high-entropy secrets in a secret manager, rotate them, and never log bearer tokens or webhook secrets.
- The current review API trusts a tenant-scoped review token and a reviewer identifier in the request. Wire it to the host application's authenticated user/session and role-based permissions before allowing human decisions in a customer-facing UI.
- Configure request limits at the proxy too; Flask/Wsgi deployments may buffer request bodies before the route-level check.
- Configure monitoring, alerting, backups, restore tests, retention/deletion policy, and incident response. The fixed-window rate limiter is a basic control, not a full abuse-prevention system.
- Validate and retain consent wording, purpose, timestamp, source, and policy version to meet applicable privacy obligations; supplied timestamps are not independently verified.
- The signed webhook format is a generic adapter contract, not a complete vendor integration. Verify each vendor's signature format, delivery retry behavior, event ID, and consent semantics before connecting it.
- Do not enable website fetching until a dedicated outbound-fetch layer enforces public-IP allowlists, blocks loopback/private/link-local/multicast/reserved/cloud-metadata addresses, revalidates DNS across redirects, pins connections safely, caps redirects/bytes/timeouts, and has SSRF regression tests.
- Provider enrichment, email/WhatsApp/phone outreach, calendar booking, and Telegram delivery must be separately authorized, consent-aware, idempotent, and logged. Human approval must gate consequential external actions.
- Use PostgreSQL or a managed queue for multiple application/worker instances. SQLite is intended only for a single host and modest workloads.
- The host app must use secure cookies, CSRF protections for session routes, authentication, and tenant-scoped database queries.

A clean dependency audit only checks known published package advisories. It does not prove application security, validate deployment configuration, or eliminate unknown vulnerabilities.
