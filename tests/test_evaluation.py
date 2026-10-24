import unittest

from web.evaluation import evaluate_labeled_cases


class QualificationEvaluationTests(unittest.TestCase):
    def test_evaluation_reports_accuracy_and_confusion_counts(self):
        cases = [
            {"id": "high-intent", "lead": {
                "name": "Example Plumbing", "company": "Example Plumbing", "email": "owner@example.com",
                "website": "https://example.com", "industry": "Plumbing",
                "pain_point": "No enquiries", "goal": "More booked jobs", "budget": "$2000",
                "timeline": "30 days", "authority": "decision_maker", "preferred_next_step": "book a call"
            }, "expected_tier": "A"},
            {"id": "empty", "lead": {}, "expected_tier": "D"},
        ]
        report = evaluate_labeled_cases(cases)
        self.assertEqual(report["cases"], 2)
        self.assertEqual(report["correct"], 2)
        self.assertEqual(report["tier_accuracy"], 1.0)
        self.assertEqual(report["scoring_version"], "baseline-v1")

    def test_evaluation_rejects_empty_or_unlabeled_cases(self):
        with self.assertRaises(ValueError):
            evaluate_labeled_cases([])
        with self.assertRaises(ValueError):
            evaluate_labeled_cases([{"lead": {}}])


if __name__ == "__main__":
    unittest.main()
