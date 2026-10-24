import unittest

from web.evidence import validate_extractions
from web.policy import evaluate_action_policy


class EvidenceValidationTests(unittest.TestCase):
    def test_accepts_only_source_grounded_high_confidence_values(self):
        source = "Budget is $2,000 and we need more booked jobs."
        result = validate_extractions(source, [
            {"field": "budget", "value": "$2,000", "evidence": "Budget is $2,000", "confidence": 0.99},
            {"field": "timeline", "value": "next week", "evidence": "next week", "confidence": 0.99},
            {"field": "goal", "value": "double revenue", "evidence": "we need more booked jobs", "confidence": 0.45},
        ])
        self.assertEqual(result["accepted"]["budget"]["value"], "$2,000")
        self.assertNotIn("timeline", result["accepted"])
        self.assertTrue(result["requires_human_review"])
        self.assertEqual(len(result["rejected"]), 2)

    def test_conflicting_values_are_withheld_for_review(self):
        result = validate_extractions("Budget is $2,000; elsewhere budget is $5,000.", [
            {"field": "budget", "value": "$2,000", "evidence": "Budget is $2,000", "confidence": 0.9},
            {"field": "budget", "value": "$5,000", "evidence": "budget is $5,000", "confidence": 0.9},
        ])
        self.assertNotIn("budget", result["accepted"])
        self.assertEqual(len(result["conflicts"]), 1)


class ActionPolicyTests(unittest.TestCase):
    def test_external_action_fails_closed_without_consent_review_or_allowlist(self):
        denied = evaluate_action_policy(action="send_email", consent={"granted": True},
            review={"required": True, "status": "pending"}, idempotency_key="request-0001",
            allowed_actions={"send_email"})
        self.assertFalse(denied["allowed"])
        self.assertIn("human_approval_missing", denied["reasons"])
        self.assertFalse(denied["external_action_executed"])

    def test_external_action_can_pass_policy_but_is_not_executed_here(self):
        result = evaluate_action_policy(action="book_call", consent={"granted": True},
            review={"required": True, "status": "approved"}, idempotency_key="request-0001",
            allowed_actions={"book_call"})
        self.assertTrue(result["allowed"])
        self.assertFalse(result["external_action_executed"])


if __name__ == "__main__":
    unittest.main()
