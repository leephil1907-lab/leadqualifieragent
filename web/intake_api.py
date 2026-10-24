"""Authenticated, tenant-scoped intake backed by the durable queue."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from flask import Blueprint, jsonify, request

from web.service_auth import resolve_bearer_tenant
from web.intake_validation import IntakeValidationError, normalize_lead
from web.job_queue import IdempotencyConflict, LeadIdentityConflict, connection, enqueue, get_job, init_db, list_events

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")
_MAX_BODY = 32 * 1024


def _rate_limit(tenant: str, token: str, *, db_path: str | None, now: int | None = None) -> bool:
    """Fixed-window per-token limit; only a one-way token digest is persisted."""
    current = int(time.time() if now is None else now)
    window = current // 60
    limit = max(1, min(int(os.getenv("LEAD_QUALIFIER_RATE_LIMIT_PER_MINUTE", "60")), 600))
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connection(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS qualifier_rate_limits (
            tenant_id TEXT NOT NULL, token_hash TEXT NOT NULL, window INTEGER NOT NULL,
            request_count INTEGER NOT NULL, PRIMARY KEY(tenant_id,token_hash,window))""")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO qualifier_rate_limits(tenant_id,token_hash,window,request_count)
            VALUES(?,?,?,1) ON CONFLICT(tenant_id,token_hash,window)
            DO UPDATE SET request_count=request_count+1""", (tenant, token_hash, window))
        count = conn.execute("""SELECT request_count FROM qualifier_rate_limits
            WHERE tenant_id=? AND token_hash=? AND window=?""", (tenant, token_hash, window)).fetchone()[0]
        # Keep the limiter table bounded.
        conn.execute("DELETE FROM qualifier_rate_limits WHERE window < ?", (window - 2,))
        conn.commit()
    return count <= limit


def create_intake_blueprint(*, db_path: str | None = None) -> Blueprint:
    blueprint = Blueprint("lead_qualifier_intake", __name__)

    @blueprint.post("/api/v1/intake/leads")
    def submit_lead():
        if request.content_length is not None and request.content_length > _MAX_BODY:
            return jsonify(error="Request body is too large."), 413
        token, tenant = resolve_bearer_tenant(request.headers.get("Authorization", ""))
        if not token or not tenant:
            if not (os.getenv("LEAD_QUALIFIER_API_TOKENS", "").strip() or
                    (os.getenv("LEAD_QUALIFIER_API_TOKEN", "").strip() and os.getenv("LEAD_QUALIFIER_TENANT_ID", "").strip())):
                return jsonify(error="Authenticated intake is not configured."), 503
            return jsonify(error="Authentication required."), 401
        init_db(db_path)
        if not _rate_limit(tenant, token, db_path=db_path):
            return jsonify(error="Rate limit exceeded. Retry shortly."), 429

        key = request.headers.get("Idempotency-Key", "").strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            return jsonify(error="A valid Idempotency-Key header is required."), 400
        raw_body = request.get_data(cache=True)
        if len(raw_body) > _MAX_BODY:
            return jsonify(error="Request body is too large."), 413
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return jsonify(error="Request body must be valid JSON."), 400
        if not isinstance(body, dict):
            return jsonify(error="Request body must be a JSON object."), 400
        if body.get("consent") is not True:
            return jsonify(error="Explicit consent is required to process this lead."), 400
        try:
            lead = normalize_lead(body)
        except IntakeValidationError as exc:
            return jsonify(error=str(exc)), 400

        consent = {
            "granted": True,
            "purpose": str(body.get("consent_purpose", "lead_qualification"))[:100],
            "wording_version": str(body.get("consent_version", ""))[:80],
            "source": lead.get("source") or "website_form",
            "recorded_at": str(body.get("consent_recorded_at", ""))[:80],
        }
        payload = {"lead": lead, "consent": consent}
        try:
            job, created = enqueue(tenant, "qualify_lead", payload, key, path=db_path)
        except (IdempotencyConflict, LeadIdentityConflict) as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc) if isinstance(exc, (IdempotencyConflict, LeadIdentityConflict)) else "Invalid intake request."), (409 if isinstance(exc, (IdempotencyConflict, LeadIdentityConflict)) else 400)
        except TypeError:
            return jsonify(error="Invalid intake request."), 400
        if not created:
            identity_duplicate = job.pop("_duplicate_identity", None)
            if identity_duplicate:
                return jsonify(ok=True, job_id=job["id"], status=job["status"], duplicate=True,
                               duplicate_reason="matching_" + identity_duplicate), 200
            existing = get_job(job["id"], tenant, path=db_path)
            if existing and existing["payload"] != payload:
                return jsonify(error="Idempotency-Key was already used with a different payload."), 409
        return jsonify(ok=True, job_id=job["id"], status=job["status"], duplicate=not created), (202 if created else 200)



    @blueprint.post("/api/v1/audits/html-snapshot")
    def submit_html_audit():
        """Queue analysis of supplied HTML; this endpoint never fetches a URL."""
        token, tenant = resolve_bearer_tenant(request.headers.get("Authorization", ""))
        if not token or not tenant:
            return jsonify(error="Authentication required."), 401
        max_audit_body = 1_100_000
        if request.content_length is not None and request.content_length > max_audit_body:
            return jsonify(error="Audit snapshot is too large."), 413
        raw_body = request.get_data(cache=True)
        if len(raw_body) > max_audit_body:
            return jsonify(error="Audit snapshot is too large."), 413
        if not _rate_limit(tenant, token, db_path=db_path):
            return jsonify(error="Rate limit exceeded. Retry shortly."), 429
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return jsonify(error="Request body must be valid JSON."), 400
        if not isinstance(body, dict) or body.get("authorized_to_audit") is not True:
            return jsonify(error="Explicit authorization to audit this HTML snapshot is required."), 400
        html = body.get("html")
        if not isinstance(html, str) or not html.strip():
            return jsonify(error="An HTML snapshot is required."), 400
        if len(html.encode("utf-8")) > 1_000_000:
            return jsonify(error="HTML snapshot exceeds the 1 MB limit."), 413
        key = request.headers.get("Idempotency-Key", "").strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            return jsonify(error="A valid Idempotency-Key header is required."), 400
        init_db(db_path)
        job, created = enqueue(tenant, "audit_html_snapshot",
                               {"html": html, "authorized_to_audit": True},
                               key, path=db_path)
        if not created:
            existing = get_job(job["id"], tenant, path=db_path)
            if existing and existing["payload"] != {"html": html, "authorized_to_audit": True}:
                return jsonify(error="Idempotency-Key was already used with a different payload."), 409
        return jsonify(ok=True, job_id=job["id"], status=job["status"], duplicate=not created), (202 if created else 200)

    @blueprint.post("/api/v1/webhooks/<channel>")
    def signed_channel_webhook(channel: str):
        """Generic signed adapter for trusted channel gateways; no provider secrets in code."""
        allowed = {"website_form", "email", "whatsapp", "telegram", "phone", "crm", "calcom"}
        if channel not in allowed:
            return jsonify(error="Unsupported intake channel."), 404
        raw_secrets = os.getenv("LEAD_QUALIFIER_WEBHOOK_SECRETS", "").strip()
        try:
            entries = json.loads(raw_secrets) if raw_secrets else []
        except ValueError:
            entries = []
        if not isinstance(entries, list):
            entries = []
        timestamp = request.headers.get("X-Webhook-Timestamp", "")
        signature = request.headers.get("X-Webhook-Signature", "")
        try:
            sent_at = int(timestamp)
        except (TypeError, ValueError):
            return jsonify(error="Invalid webhook timestamp."), 401
        if abs(int(time.time()) - sent_at) > 300:
            return jsonify(error="Webhook timestamp expired."), 401
        if request.content_length is not None and request.content_length > _MAX_BODY:
            return jsonify(error="Request body is too large."), 413
        raw_body = request.get_data(cache=True)
        if len(raw_body) > _MAX_BODY:
            return jsonify(error="Request body is too large."), 413
        matched_tenant = None
        matched_secret = None
        signed = (timestamp + ".").encode("utf-8") + raw_body
        import hmac
        import hashlib as _hashlib
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("channel") != channel:
                continue
            secret, tenant_id = entry.get("secret"), entry.get("tenant_id")
            if not isinstance(secret, str) or not secret or not isinstance(tenant_id, str) or not tenant_id:
                continue
            # Signature format: sha256=<hex HMAC(timestamp + "." + raw_body)>.
            if hmac.compare_digest(signature.removeprefix("sha256="), hmac.new(secret.encode("utf-8"), signed, _hashlib.sha256).hexdigest()):
                matched_tenant = tenant_id
                matched_secret = secret
                break
        if not matched_tenant:
            return jsonify(error="Webhook signature is invalid."), 401
        if not _rate_limit(matched_tenant, matched_secret or "", db_path=db_path):
            return jsonify(error="Rate limit exceeded. Retry shortly."), 429
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return jsonify(error="Webhook body must be valid JSON."), 400
        if not isinstance(body, dict) or body.get("consent") is not True:
            return jsonify(error="A JSON object and explicit consent are required."), 400
        try:
            lead = normalize_lead({**body, "source": channel})
        except IntakeValidationError as exc:
            return jsonify(error=str(exc)), 400
        key = request.headers.get("Idempotency-Key", "").strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            return jsonify(error="A valid Idempotency-Key header is required."), 400
        init_db(db_path)
        consent = {
            "granted": True, "purpose": str(body.get("consent_purpose", "lead_qualification"))[:100],
            "wording_version": str(body.get("consent_version", ""))[:80],
            "source": channel, "recorded_at": str(body.get("consent_recorded_at", ""))[:80],
        }
        payload = {"lead": lead, "consent": consent}
        try:
            job, created = enqueue(matched_tenant, "qualify_lead", payload, key, path=db_path)
        except (IdempotencyConflict, LeadIdentityConflict) as exc:
            return jsonify(error=str(exc)), 409
        if not created:
            identity_duplicate = job.pop("_duplicate_identity", None)
            if identity_duplicate:
                return jsonify(ok=True, job_id=job["id"], status=job["status"], channel=channel,
                               duplicate=True, duplicate_reason="matching_" + identity_duplicate), 200
            existing = get_job(job["id"], matched_tenant, path=db_path)
            if existing and existing["payload"] != payload:
                return jsonify(error="Idempotency-Key was already used with a different payload."), 409
        return jsonify(ok=True, job_id=job["id"], status=job["status"], channel=channel,
                       duplicate=not created), (202 if created else 200)

    @blueprint.get("/api/v1/intake/jobs/<job_id>")
    def job_status(job_id: str):
        _, tenant = resolve_bearer_tenant(request.headers.get("Authorization", ""))
        if not tenant:
            return jsonify(error="Authentication required."), 401
        job = get_job(job_id, tenant, path=db_path)
        if not job:
            return jsonify(error="Job not found."), 404
        # Do not expose raw lead payload or exception details to status callers.
        return jsonify(job={"id": job["id"], "status": job["status"], "attempts": job["attempts"],
                            "created_at": job["created_at"], "updated_at": job["updated_at"],
                            "result": job["result"] if job["status"] == "succeeded" else None,
                            "events": list_events(job_id, tenant, path=db_path)})

    return blueprint
