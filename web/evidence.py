"""Evidence validation for future model or channel extractors.

Model output is untrusted. Every extracted value must cite an exact source substring;
unsupported or conflicting claims are withheld and flagged for human review.
"""
from __future__ import annotations

from typing import Any


def validate_extractions(source_text: str, candidates: Any, *, confidence_threshold: float = 0.75) -> dict[str, Any]:
    if not isinstance(source_text, str):
        raise ValueError("source_text must be a string")
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    threshold = max(0.0, min(float(confidence_threshold), 1.0))
    accepted: dict[str, dict[str, Any]] = {}
    rejected = []
    conflicts = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            rejected.append({"index": index, "reason": "candidate_not_object"})
            continue
        field = item.get("field")
        value = item.get("value")
        evidence = item.get("evidence")
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = -1
        if not isinstance(field, str) or not field.isidentifier() or not isinstance(value, str) or not value.strip():
            rejected.append({"index": index, "reason": "invalid_field_or_value"})
            continue
        if not isinstance(evidence, str) or not evidence.strip() or evidence not in source_text:
            rejected.append({"index": index, "field": field, "reason": "evidence_not_in_source"})
            continue
        if not 0 <= confidence <= 1 or confidence < threshold:
            rejected.append({"index": index, "field": field, "reason": "confidence_below_threshold"})
            continue
        normalized = value.strip()
        existing = accepted.get(field)
        if existing and existing["value"].casefold() != normalized.casefold():
            conflicts.append({"field": field, "values": [existing["value"], normalized]})
            accepted.pop(field, None)
            continue
        if field not in {conflict["field"] for conflict in conflicts}:
            accepted[field] = {"value": normalized, "evidence": evidence, "confidence": confidence}
    return {
        "accepted": accepted,
        "rejected": rejected,
        "conflicts": conflicts,
        "requires_human_review": bool(rejected or conflicts),
        "source_length": len(source_text),
    }
