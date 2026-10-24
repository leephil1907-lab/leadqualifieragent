# Security boundaries and deployment checklist

## Implemented on the feature branch
- Intake requires a configured bearer token and server-configured tenant ID.
- The client cannot choose its tenant by sending a tenant field.
- Payloads are normalized and bounded before enqueueing; unknown fields are discarded.
- Explicit boolean consent is required before enqueueing.
- Idempotency keys are bounded and scoped to the configured tenant; conflicting payload reuse returns HTTP 409.
- Queue events record lifecycle changes; the worker runs the existing deterministic scorer and performs no outbound calls.
- CI runs tests, bytecode compilation, and pip-audit.

## Required before public production traffic
- Use HTTPS at the reverse proxy and store a high-entropy API token in a secret manager; rotate it.
- Add per-token rate limits, request-size limits at proxy/app, monitoring, and abuse controls.
- Implement per-customer token-to-tenant mapping before using multiple tenants. The current endpoint is intentionally single-tenant per process/configuration.
- Protect browser/session endpoints with the host application's authentication and CSRF strategy; this bearer-token endpoint is for trusted server-to-server callers.
- Validate and retain consent wording, purpose, timestamp, source, and policy version according to the applicable privacy requirements; a caller-supplied timestamp is not independently verified.
- If website fetching/enrichment is added, block loopback, private, link-local, multicast, reserved IPs and cloud metadata; re-check DNS after redirects and cap redirects, bytes, and timeouts to prevent SSRF.
- Use PostgreSQL or a managed queue for multiple app instances or higher throughput. SQLite is a single-host/modest-workload option.
- Keep external messaging, booking, enrichment and email sends behind separate consent/policy checks and human approval where appropriate.
- Never log bearer tokens or raw authorization headers.

A successful dependency audit checks known published package advisories; it does not prove application security or eliminate unknown vulnerabilities.
