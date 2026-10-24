"""Consent- and review-gated outbound adapters for Resend, WhatsApp Cloud API, and Twilio.

Every channel has a separate consent field and records a durable attempt before sending.
Ambiguous provider outcomes are never automatically retried.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import requests
from flask import Blueprint, jsonify, request

from web.job_queue import connection, get_job
from web.policy import evaluate_action_policy
from web.service_auth import resolve_bearer_tenant

_LOG = logging.getLogger(__name__)
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")
_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


def _ensure_attempts(db_path: str | None) -> None:
    with connection(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS qualifier_outbound_attempts (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, job_id TEXT NOT NULL,
            action TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
            status TEXT NOT NULL, provider_id TEXT, response_summary TEXT,
            consent_version TEXT NOT NULL, consent_source TEXT NOT NULL, consent_recorded_at REAL NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(tenant_id,idempotency_key))""")


def _prepare_attempt(*, tenant: str, job_id: str, action: str, key: str,
                     fingerprint: str, version: str, source: str,
                     db_path: str | None) -> tuple[dict | None, bool]:
    """Return existing attempt (duplicate=True) or create an in-progress attempt."""
    _ensure_attempts(db_path)
    stamp = time.time()
    with connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM qualifier_outbound_attempts WHERE tenant_id=? AND idempotency_key=?",
                                (tenant, key)).fetchone()
        if existing:
            conn.commit()
            if existing["request_hash"] != fingerprint or existing["job_id"] != job_id or existing["action"] != action:
                return {"error": "Idempotency-Key was used for a different outbound request."}, False
            return dict(existing), True
        row_id = uuid.uuid4().hex
        conn.execute("""INSERT INTO qualifier_outbound_attempts
            (id,tenant_id,job_id,action,idempotency_key,request_hash,status,consent_version,
             consent_source,consent_recorded_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'processing',?,?,?,?,?)""",
            (row_id, tenant, job_id, action, key, fingerprint, version, source, stamp, stamp, stamp))
        conn.commit()
    return {"id": row_id, "status": "processing"}, False


def _finish_attempt(*, tenant: str, key: str, status: str, provider_id: str | None,
                    summary: dict, db_path: str | None) -> None:
    with connection(db_path) as conn:
        conn.execute("""UPDATE qualifier_outbound_attempts SET status=?,provider_id=?,
            response_summary=?,updated_at=? WHERE tenant_id=? AND idempotency_key=?""",
            (status, provider_id, json.dumps(summary, ensure_ascii=False), time.time(), tenant, key))


def create_outbound_blueprint(*, db_path: str | None = None) -> Blueprint:
    blueprint = Blueprint("lead_qualifier_outbound", __name__)

    def execute(action: str, consent_field: str):
        _, tenant = resolve_bearer_tenant(request.headers.get("Authorization", ""), purpose="outreach")
        if not tenant:
            return jsonify(error="Outbound-action permission required."), 401
        if request.content_length is not None and request.content_length > 32 * 1024:
            return jsonify(error="Request body is too large."), 413
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Request body must be a JSON object."), 400
        key = request.headers.get("Idempotency-Key", "").strip()
        if not _KEY_RE.fullmatch(key):
            return jsonify(error="A valid Idempotency-Key header is required."), 400
        job_id = str(body.get("job_id", "")).strip()
        if not job_id:
            return jsonify(error="job_id is required."), 400
        job = get_job(job_id, tenant, path=db_path)
        if not job or job.get("status") != "succeeded":
            return jsonify(error="Completed qualification job not found."), 404
        result = job.get("result", {})
        lead = result.get("lead", {})
        consent = body.get(consent_field) is True
        version = str(body.get(consent_field + "_version", "")).strip()[:80]
        source = str(body.get(consent_field + "_source", "")).strip()[:100]
        if not consent or not version or not source:
            return jsonify(error=f"Explicit {action} consent, version, and source are required."), 400
        policy = evaluate_action_policy(action=action, consent={"granted": consent},
            review=result.get("review"), idempotency_key=key, allowed_actions={action})
        if not policy["allowed"]:
            return jsonify(error="Outbound action is blocked by consent/review policy.", policy=policy), 403

        provider_payload: dict
        endpoint: str
        headers: dict[str, str]
        auth = None
        if action == "send_email":
            api_key = os.getenv("RESEND_API_KEY", "").strip()
            sender = os.getenv("RESEND_FROM_EMAIL", "").strip()
            recipient = str(lead.get("email") or "").strip().lower()
            subject = str(body.get("subject", "")).strip()[:200]
            text_body = str(body.get("text", "")).strip()[:20000]
            html_body = str(body.get("html", "")).strip()[:20000]
            if not api_key or not sender:
                return jsonify(error="Resend email delivery is not configured."), 503
            if not _EMAIL_RE.fullmatch(recipient) or not subject or not (text_body or html_body):
                return jsonify(error="Lead email, subject, and text or HTML body are required."), 400
            endpoint = "https://api.resend.com/emails"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                       "Idempotency-Key": key}
            provider_payload = {"from": sender, "to": [recipient], "subject": subject}
            if text_body:
                provider_payload["text"] = text_body
            if html_body:
                provider_payload["html"] = html_body
        elif action == "send_whatsapp_template":
            api_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
            phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
            version_api = os.getenv("WHATSAPP_API_VERSION", "").strip()
            recipient = str(lead.get("phone") or "").strip()
            template_name = str(body.get("template_name", "")).strip()
            language = str(body.get("language_code", "en")).strip()
            components = body.get("components", [])
            if not api_token or not phone_id or not re.fullmatch(r"v[0-9]+\.[0-9]+", version_api):
                return jsonify(error="WhatsApp Cloud API configuration is incomplete."), 503
            if not _PHONE_RE.fullmatch(recipient) or not re.fullmatch(r"[a-z0-9_]{1,512}", template_name):
                return jsonify(error="A valid lead phone number and approved template name are required."), 400
            if not isinstance(components, list) or len(json.dumps(components)) > 10000:
                return jsonify(error="Template components are invalid or too large."), 400
            endpoint = f"https://graph.facebook.com/{version_api}/{phone_id}/messages"
            headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json",
                       "Idempotency-Key": key}
            provider_payload = {"messaging_product": "whatsapp", "to": recipient, "type": "template",
                                "template": {"name": template_name, "language": {"code": language},
                                             "components": components}}
        elif action == "place_call":
            sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
            token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
            from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
            voice_url = os.getenv("TWILIO_VOICE_WEBHOOK_URL", "").strip()
            recipient = str(lead.get("phone") or "").strip()
            parsed = urlsplit(voice_url)
            if not sid or not token or not from_number or parsed.scheme != "https" or not parsed.hostname:
                return jsonify(error="Twilio call configuration is incomplete."), 503
            if not _PHONE_RE.fullmatch(recipient) or not _PHONE_RE.fullmatch(from_number):
                return jsonify(error="Lead and Twilio phone numbers must use E.164 format."), 400
            endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
            headers = {"Idempotency-Key": key}
            auth = (sid, token)
            provider_payload = {"To": recipient, "From": from_number, "Url": voice_url, "Method": "POST"}
        else:
            return jsonify(error="Unsupported outbound action."), 400

        fingerprint = hashlib.sha256(json.dumps({
            "action": action, "provider_payload": provider_payload, "consent_version": version,
            "consent_source": source, "job_id": job_id,
        }, sort_keys=True).encode("utf-8")).hexdigest()
        attempt, duplicate = _prepare_attempt(tenant=tenant, job_id=job_id, action=action, key=key,
            fingerprint=fingerprint, version=version, source=source, db_path=db_path)
        if duplicate:
            if attempt.get("error"):
                return jsonify(error=attempt["error"]), 409
            if attempt.get("status") == "succeeded":
                return jsonify(ok=True, duplicate=True, provider_id=attempt.get("provider_id"),
                               result=json.loads(attempt.get("response_summary") or "{}")), 200
            return jsonify(error="Outbound attempt already exists; reconcile before retrying.",
                           status=attempt.get("status")), 409
        try:
            response = requests.post(endpoint, json=provider_payload if auth is None else None,
                data=provider_payload if auth is not None else None, headers=headers, auth=auth,
                timeout=(3, 15))
            response.raise_for_status()
            data = response.json()
            if action == "send_email":
                provider_id = data.get("id")
            elif action == "send_whatsapp_template":
                messages = data.get("messages") or []
                provider_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
            else:
                provider_id = data.get("sid")
            if not provider_id:
                raise RuntimeError("Provider response did not contain an action identifier")
            summary = {"provider": action, "accepted": True}
            status = "succeeded"
        except requests.HTTPError as exc:
            code = getattr(getattr(exc, "response", None), "status_code", 0)
            status = "uncertain" if code >= 500 or code == 0 else "failed"
            summary = {"provider_error": "http_error", "status_code": code}
            provider_id = None
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            status = "uncertain"
            summary = {"provider_error": type(exc).__name__, "message": "Provider outcome unknown; reconcile before retrying."}
            provider_id = None
        _finish_attempt(tenant=tenant, key=key, status=status, provider_id=provider_id,
                        summary=summary, db_path=db_path)
        if status == "uncertain":
            return jsonify(error="Provider outcome is uncertain; reconcile before retrying.", status=status), 502
        if status == "failed":
            return jsonify(error="Provider rejected the outbound request.", status=status), 502
        return jsonify(ok=True, duplicate=False, action=action, provider_id=provider_id), 201

    @blueprint.post("/api/v1/outbound/email")
    def email():
        return execute("send_email", "email_consent")

    @blueprint.post("/api/v1/outbound/whatsapp-template")
    def whatsapp():
        return execute("send_whatsapp_template", "whatsapp_consent")

    @blueprint.post("/api/v1/outbound/call")
    def call():
        return execute("place_call", "phone_consent")

    return blueprint
