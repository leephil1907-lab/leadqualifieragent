"""Tenant-scoped review queue API. Decisions are recorded but cause no external side effects."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from web.service_auth import resolve_bearer_tenant
from web.job_queue import decide_review, list_pending_reviews


def create_review_blueprint(*, db_path: str | None = None) -> Blueprint:
    blueprint = Blueprint("lead_qualifier_review", __name__)

    def tenant():
        _, tenant_id = resolve_bearer_tenant(request.headers.get("Authorization", ""), purpose="review")
        return tenant_id

    @blueprint.get("/api/v1/reviews")
    def pending():
        tenant_id = tenant()
        if not tenant_id:
            return jsonify(error="Authentication required."), 401
        return jsonify(reviews=list_pending_reviews(tenant_id, path=db_path))

    @blueprint.post("/api/v1/reviews/<job_id>")
    def decide(job_id: str):
        tenant_id = tenant()
        if not tenant_id:
            return jsonify(error="Authentication required."), 401
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Request body must be a JSON object."), 400
        reviewer = str(body.get("reviewer", "")).strip()
        reason = str(body.get("reason", "")).strip()
        decision = str(body.get("decision", "")).strip().lower()
        if decision not in {"approved", "rejected"} or not reviewer or not reason:
            return jsonify(error="decision, reviewer, and reason are required."), 400
        try:
            result = decide_review(job_id, tenant_id, decision, reviewer, reason, path=db_path)
        except LookupError:
            return jsonify(error="Review item not found."), 404
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True, review=result, external_actions_triggered=False), 200

    return blueprint
