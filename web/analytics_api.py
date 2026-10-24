"""Tenant-scoped pipeline analytics and outcome feedback endpoints."""
from __future__ import annotations

import json
import re
import uuid
from flask import Blueprint, jsonify, request

from web.service_auth import resolve_bearer_tenant
from web.job_queue import connection


_ALLOWED_OUTCOMES = {"meeting_booked", "converted", "lost", "not_interested", "unqualified"}
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


def create_analytics_blueprint(*, db_path: str | None = None) -> Blueprint:
    blueprint = Blueprint("lead_qualifier_analytics", __name__)

    def tenant():
        _, tenant_id = resolve_bearer_tenant(request.headers.get("Authorization", ""))
        return tenant_id

    def ensure_outcomes() -> None:
        with connection(db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS qualifier_outcomes (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, job_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, outcome TEXT NOT NULL,
                revenue REAL, reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                UNIQUE(tenant_id, idempotency_key))""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_qualifier_outcomes_tenant_created ON qualifier_outcomes(tenant_id,created_at)")

    @blueprint.get("/api/v1/analytics/summary")
    def summary():
        tenant_id = tenant()
        if not tenant_id:
            return jsonify(error="Authentication required."), 401
        ensure_outcomes()
        with connection(db_path) as conn:
            rows = conn.execute("""SELECT status,COUNT(*) AS count FROM qualifier_jobs
                WHERE tenant_id=? GROUP BY status""", (tenant_id,)).fetchall()
            jobs = {row["status"]: row["count"] for row in rows}
            completed = conn.execute("""SELECT result FROM qualifier_jobs
                WHERE tenant_id=? AND status='succeeded'""", (tenant_id,)).fetchall()
            tiers: dict[str, int] = {}
            reviews: dict[str, int] = {}
            score_count = 0
            score_total = 0
            for row in completed:
                result = json.loads(row["result"] or "{}")
                qualification = result.get("qualification", {})
                if isinstance(qualification, dict) and qualification.get("tier") in {"A", "B", "C", "D"}:
                    tier = qualification["tier"]
                    tiers[tier] = tiers.get(tier, 0) + 1
                    score_total += int(qualification.get("total_score", 0))
                    score_count += 1
                review = result.get("review", {})
                if isinstance(review, dict) and review.get("required"):
                    state = str(review.get("status", "pending"))
                    reviews[state] = reviews.get(state, 0) + 1
            outcome_rows = conn.execute("""SELECT outcome,COUNT(*) AS count,
                COALESCE(SUM(revenue),0) AS revenue FROM qualifier_outcomes
                WHERE tenant_id=? GROUP BY outcome""", (tenant_id,)).fetchall()
            outcomes = {row["outcome"]: {"count": row["count"], "revenue": round(row["revenue"], 2)}
                        for row in outcome_rows}
        return jsonify(jobs=jobs, tiers=tiers, reviews=reviews, outcomes=outcomes,
                       average_score=round(score_total / score_count, 2) if score_count else None,
                       scoring_version="baseline-v1")

    @blueprint.post("/api/v1/outcomes")
    def record_outcome():
        tenant_id = tenant()
        if not tenant_id:
            return jsonify(error="Authentication required."), 401
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Request body must be a JSON object."), 400
        job_id = str(body.get("job_id", "")).strip()
        outcome = str(body.get("outcome", "")).strip().lower()
        key = request.headers.get("Idempotency-Key", "").strip()
        reason = str(body.get("reason", "")).strip()[:500]
        revenue_raw = body.get("revenue")
        if not job_id or outcome not in _ALLOWED_OUTCOMES or not _IDEMPOTENCY_RE.fullmatch(key):
            return jsonify(error="job_id, a supported outcome, and a valid Idempotency-Key are required."), 400
        if isinstance(revenue_raw, bool):
            return jsonify(error="revenue must be a non-negative number."), 400
        try:
            revenue = None if revenue_raw in (None, "") else float(revenue_raw)
        except (TypeError, ValueError):
            return jsonify(error="revenue must be a non-negative number."), 400
        if revenue is not None and (revenue < 0 or revenue > 1_000_000_000):
            return jsonify(error="revenue must be a non-negative number within the supported range."), 400
        ensure_outcomes()
        with connection(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT id FROM qualifier_jobs WHERE id=? AND tenant_id=? AND status='succeeded'",
                               (job_id, tenant_id)).fetchone()
            if not job:
                conn.rollback()
                return jsonify(error="Completed job not found."), 404
            existing = conn.execute("SELECT * FROM qualifier_outcomes WHERE tenant_id=? AND idempotency_key=?",
                                    (tenant_id, key)).fetchone()
            if existing:
                same_payload = (existing["job_id"] == job_id and existing["outcome"] == outcome
                                and existing["revenue"] == revenue and existing["reason"] == reason)
                conn.commit()
                if not same_payload:
                    return jsonify(error="Idempotency-Key was already used for a different outcome payload."), 409
                return jsonify(ok=True, outcome=existing["outcome"], duplicate=True), 200
            conn.execute("""INSERT INTO qualifier_outcomes
                (id,tenant_id,job_id,idempotency_key,outcome,revenue,reason,created_at)
                VALUES(?,?,?,?,?,?,?,strftime('%s','now'))""",
                (uuid.uuid4().hex, tenant_id, job_id, key, outcome, revenue, reason))
            conn.commit()
        return jsonify(ok=True, job_id=job_id, outcome=outcome, duplicate=False), 201

    return blueprint
