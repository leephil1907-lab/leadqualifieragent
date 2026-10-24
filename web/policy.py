"""Deterministic policy gates for any future outbound action."""
from __future__ import annotations

from typing import Any


def evaluate_action_policy(*, action: str, consent: Any, review: Any,
                           idempotency_key: str, allowed_actions: set[str] | None = None) -> dict[str, Any]:
    """Fail closed unless consent, explicit human approval, and action allowlist pass."""
    action_name = str(action or "").strip().lower()
    allowed = allowed_actions or set()
    consent_ok = isinstance(consent, dict) and consent.get("granted") is True
    review_ok = isinstance(review, dict) and review.get("required") is True and review.get("status") == "approved"
    key_ok = isinstance(idempotency_key, str) and 8 <= len(idempotency_key.strip()) <= 200
    reasons = []
    if not consent_ok:
        reasons.append("explicit_consent_missing")
    if not review_ok:
        reasons.append("human_approval_missing")
    if action_name not in allowed:
        reasons.append("action_not_allowlisted")
    if not key_ok:
        reasons.append("idempotency_key_missing_or_invalid")
    return {
        "allowed": not reasons,
        "action": action_name,
        "reasons": reasons,
        "idempotency_key_valid": key_ok,
        "external_action_executed": False,
    }
