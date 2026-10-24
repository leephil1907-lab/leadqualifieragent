"""Authenticated, tenant-scoped HTTP intake backed by the durable queue.

Register with app.register_blueprint(create_intake_blueprint()).
The API is disabled unless both LEAD_QUALIFIER_API_TOKEN and
LEAD_QUALIFIER_TENANT_ID are configured. Use a high-entropy secret and HTTPS.
"""
from __future__ import annotations

import hmac
import os
import re
from flask import Blueprint, jsonify, request

from web.intake_validation import IntakeValidationError, normalize_lead
from web.job_queue import enqueue, get_job, init_db

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


def create_intake_blueprint(*, db_path: str | None = None) -> Blueprint:
    blueprint = Blueprint("lead_qualifier_intake", __name__)

    def config():
        token = os.getenv("LEAD_QUALIFIER_API_TOKEN", "").strip()
        tenant = os.getenv("LEAD_QUALIFIER_TENANT_ID", "").strip()
        return token, tenant

    @blueprint.post("/api/v1/intake/leads")
    def submit_lead():
        token, tenant = config()
        if not token or not tenant:
            return jsonify(error="Authenticated intake is not configured."), 503
        auth = request.headers.get("Authorization", "")
        scheme, _, supplied = auth.partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, token):
            return jsonify(error="Authentication required."), 401

        key = request.headers.get("Idempotency-Key", "").strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            return jsonify(error="A valid Idempotency-Key header is required."), 400

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Request body must be a JSON object."), 400
        # Consent must be explicit; it is not inferred from submitting the form.
        if body.get("consent") is not True:
            return jsonify(error="Explicit consent is required to process this lead."), 400
        try:
            lead = normalize_lead(body)
        except IntakeValidationError as exc:
            return jsonify(error=str(exc)), 400

        init_db(db_path)
        try:
            job, created = enqueue(
                tenant, "qualify_lead",
                {"lead": lead, "consent": True, "consent_recorded_at": request.headers.get("X-Consent-Recorded-At", "")[:80]},
                key, path=db_path,
            )
        except (ValueError, TypeError):
            return jsonify(error="Invalid intake request."), 400
        if not created:
            existing = get_job(job["id"], tenant, path=db_path)
            if existing and existing["payload"] != {"lead": lead, "consent": True, "consent_recorded_at": request.headers.get("X-Consent-Recorded-At", "")[:80]}:
                return jsonify(error="Idempotency-Key was already used with a different payload."), 409
        return jsonify(ok=True, job_id=job["id"], status=job["status"], duplicate=not created), (202 if created else 200)

    return blueprint
