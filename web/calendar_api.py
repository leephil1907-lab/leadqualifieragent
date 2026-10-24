"""Optional Cal.com v2 availability and consent/review-gated booking endpoints."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Blueprint, jsonify, request

from web.job_queue import connection, get_job
from web.policy import evaluate_action_policy
from web.service_auth import resolve_bearer_tenant


_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


def _config():
    key = os.getenv("CAL_COM_API_KEY", "").strip()
    event_type = os.getenv("CAL_COM_EVENT_TYPE_ID", "").strip()
    version = os.getenv("CAL_COM_API_VERSION", "2026-02-25").strip()
    return key, event_type, version


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("startTime must be an ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("startTime must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("startTime must include a timezone")
    if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise ValueError("Booking time must be in the future")
    return parsed


def create_calendar_blueprint(*, db_path: str | None = None) -> Blueprint:
    blueprint = Blueprint("lead_qualifier_calendar", __name__)

    def tenant():
        _, tenant_id = resolve_bearer_tenant(request.headers.get("Authorization", ""), purpose="booking")
        return tenant_id

    def init_attempts():
        with connection(db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS qualifier_booking_attempts (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, job_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, status TEXT NOT NULL,
                booking_uid TEXT, response_summary TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, UNIQUE(tenant_id,idempotency_key))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS qualifier_booking_consents (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, job_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, purpose TEXT NOT NULL, wording_version TEXT NOT NULL,
                source TEXT NOT NULL, recorded_at REAL NOT NULL, UNIQUE(tenant_id,idempotency_key))""")

    @blueprint.get("/api/v1/calendar/availability")
    def availability():
        tenant_id = tenant()
        if not tenant_id:
            return jsonify(error="Booking integration permission required."), 401
        api_key, event_type, version = _config()
        if not api_key or not event_type:
            return jsonify(error="Cal.com integration is not configured."), 503
        try:
            start = _parse_datetime(request.args.get("startTime", ""))
            end = _parse_datetime(request.args.get("endTime", ""))
            timezone_name = request.args.get("timeZone", "")
            ZoneInfo(timezone_name)
            if end <= start or (end - start).days > 31:
                raise ValueError("endTime must be after startTime and within 31 days")
        except (ValueError, ZoneInfoNotFoundError) as exc:
            return jsonify(error=str(exc) or "Invalid timezone."), 400
        try:
            response = requests.get("https://api.cal.com/v2/slots", params={
                "eventTypeId": event_type, "startTime": start.isoformat(),
                "endTime": end.isoformat(), "timeZone": timezone_name,
            }, headers={"Authorization": f"Bearer {api_key}", "cal-api-version": version,
                        "Accept": "application/json"}, timeout=(3, 12))
            response.raise_for_status()
            data = response.json()
            return jsonify(availability=data.get("data", data)), 200
        except (requests.RequestException, ValueError) as exc:
            logging.getLogger(__name__).warning("Cal.com availability request failed: %s", type(exc).__name__)
            return jsonify(error="Availability could not be retrieved."), 502

    @blueprint.post("/api/v1/calendar/bookings")
    def book():
        tenant_id = tenant()
        if not tenant_id:
            return jsonify(error="Booking integration permission required."), 401
        api_key, event_type, version = _config()
        if not api_key or not event_type:
            return jsonify(error="Cal.com integration is not configured."), 503
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Request body must be a JSON object."), 400
        job_id = str(body.get("job_id", "")).strip()
        key = request.headers.get("Idempotency-Key", "").strip()
        if not job_id or not _IDEMPOTENCY_RE.fullmatch(key):
            return jsonify(error="job_id and Idempotency-Key are required."), 400
        job = get_job(job_id, tenant_id, path=db_path)
        if not job or job.get("status") != "succeeded":
            return jsonify(error="Completed qualification job not found."), 404
        payload = job.get("payload", {})
        result = job.get("result", {})
        if body.get("booking_consent") is not True:
            return jsonify(error="Explicit booking consent is required."), 400
        consent_version = str(body.get("booking_consent_version", "")).strip()[:80]
        consent_source = str(body.get("booking_consent_source", "")).strip()[:100]
        if not consent_version or not consent_source:
            return jsonify(error="Booking consent version and source are required."), 400
        stored_consent = payload.get("consent")
        combined_consent = {
            "granted": isinstance(stored_consent, dict) and stored_consent.get("granted") is True,
            "purpose": "booking",
            "booking_consent_granted": True,
        }
        policy = evaluate_action_policy(action="book_call", consent=combined_consent,
            review=result.get("review"), idempotency_key=key, allowed_actions={"book_call"})
        if not policy["allowed"]:
            return jsonify(error="Booking is blocked by the consent/review policy.", policy=policy), 403
        lead = result.get("lead", {})
        name = str(body.get("name") or lead.get("name") or lead.get("company") or "").strip()[:200]
        email = str(body.get("email") or lead.get("email") or "").strip().lower()[:254]
        timezone_name = str(body.get("timeZone", "")).strip()
        try:
            start = _parse_datetime(body.get("start"))
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            return jsonify(error=str(exc) or "A valid IANA timeZone is required."), 400
        if not name or not email or "@" not in email:
            return jsonify(error="Attendee name and valid email are required."), 400
        booking_payload = {
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "eventTypeId": int(event_type) if event_type.isdigit() else event_type,
            "attendee": {"name": name, "email": email, "timeZone": timezone_name, "language": "en"},
            "metadata": {"source": "lead-qualifier-agent", "jobId": job_id, "idempotencyKey": key},
        }
        fingerprint_payload = {"booking": booking_payload, "consent_version": consent_version,
                              "consent_source": consent_source}
        fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True).encode()).hexdigest()
        init_attempts()
        stamp = time.time()
        with connection(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM qualifier_booking_attempts WHERE tenant_id=? AND idempotency_key=?",
                                    (tenant_id, key)).fetchone()
            if existing:
                conn.commit()
                if existing["request_hash"] != fingerprint or existing["job_id"] != job_id:
                    return jsonify(error="Idempotency-Key was used for a different booking request."), 409
                if existing["status"] == "succeeded":
                    return jsonify(ok=True, duplicate=True, booking_uid=existing["booking_uid"],
                                   booking= json.loads(existing["response_summary"] or "{}")), 200
                return jsonify(error="Booking attempt already exists and requires reconciliation.",
                               status=existing["status"]), 409
            conn.execute("""INSERT INTO qualifier_booking_attempts
                (id,tenant_id,job_id,idempotency_key,request_hash,status,created_at,updated_at)
                VALUES(?,?,?,?,?,'processing',?,?)""",
                (uuid.uuid4().hex, tenant_id, job_id, key, fingerprint, stamp, stamp))
            conn.execute("""INSERT INTO qualifier_booking_consents
                (id,tenant_id,job_id,idempotency_key,purpose,wording_version,source,recorded_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, tenant_id, job_id, key, "booking", consent_version, consent_source, stamp))
            conn.commit()
        try:
            response = requests.post("https://api.cal.com/v2/bookings", json=booking_payload,
                headers={"Authorization": f"Bearer {api_key}", "cal-api-version": version,
                         "Content-Type": "application/json", "Accept": "application/json"},
                timeout=(3, 15))
            response.raise_for_status()
            response_data = response.json()
            data = response_data.get("data", response_data)
            uid = data.get("uid") or data.get("bookingUid") or data.get("id")
            if not uid:
                raise RuntimeError("Cal.com response did not contain a booking identifier")
            summary = {"uid": str(uid), "status": data.get("status"), "start": data.get("start"),
                       "meetingUrl": data.get("meetingUrl") or data.get("location")}
            status = "succeeded"
            http_status = 201
        except requests.HTTPError as exc:
            provider_status = getattr(getattr(exc, "response", None), "status_code", 0)
            if provider_status >= 500 or provider_status == 0:
                status, summary, uid, http_status = "uncertain", {"message": "Provider error; booking outcome may be unknown."}, None, 502
            else:
                status, summary, uid, http_status = "failed", {"error_type": "ProviderRejectedRequest", "status_code": provider_status}, None, 502
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            # Connection loss, malformed success payload, or missing UID may happen after
            # the provider created the booking. Never retry these automatically.
            status, summary, uid, http_status = "uncertain", {"message": "Provider outcome is unknown; reconcile before retrying."}, None, 502
        with connection(db_path) as conn:
            conn.execute("UPDATE qualifier_booking_attempts SET status=?,booking_uid=?,response_summary=?,updated_at=? WHERE tenant_id=? AND idempotency_key=?",
                (status, uid, json.dumps(summary, ensure_ascii=False), time.time(), tenant_id, key))
        if status == "uncertain":
            return jsonify(error="Booking outcome is uncertain; reconcile in Cal.com before retrying.",
                           status=status), http_status
        if status == "failed":
            return jsonify(error="Cal.com booking failed; this request will not be retried automatically.",
                           status=status), http_status
        return jsonify(ok=True, duplicate=False, booking_uid=str(uid), booking=summary), http_status

    return blueprint
