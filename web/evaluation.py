"""Offline evaluation helpers for labeled lead-qualification examples.

This module never changes production scoring. It measures the existing scorer against
human-labeled records and intentionally refuses to claim a benchmark without labels.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from web.lead_qualifier import _score


def evaluate_labeled_cases(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return tier accuracy and confusion counts for cases with expected_tier labels."""
    rows = list(cases)
    if not rows:
        raise ValueError("At least one labeled case is required")
    confusion: dict[str, dict[str, int]] = {}
    correct = 0
    predictions = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("lead"), dict):
            raise ValueError(f"Case {index} must contain a lead object")
        expected = str(row.get("expected_tier", "")).strip().upper()
        if expected not in {"A", "B", "C", "D"}:
            raise ValueError(f"Case {index} has an invalid expected_tier")
        actual = _score(row["lead"]).get("tier")
        confusion.setdefault(expected, {})
        confusion[expected][actual] = confusion[expected].get(actual, 0) + 1
        correct += int(actual == expected)
        predictions.append({"case_id": str(row.get("id", index)), "expected_tier": expected,
                            "predicted_tier": actual, "correct": actual == expected})
    return {
        "cases": len(rows),
        "correct": correct,
        "tier_accuracy": round(correct / len(rows), 4),
        "confusion_matrix": confusion,
        "predictions": predictions,
        "scoring_version": "baseline-v1",
        "interpretation": "Use representative, human-labeled examples. This metric is only as reliable as the supplied labels.",
    }
